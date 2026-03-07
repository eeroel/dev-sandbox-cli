#!/usr/bin/env python3
"""
sandbox.py — Dev sandbox container manager (podman/docker, docker-compose-free)

Network infrastructure is created if not present — idempotent.
Each sandbox gets its own squid proxy, started and stopped with the container.
Default sandbox name is derived from the repo directory name.

TODO:
  - Add `--pull` flag to `up` to refresh the base image before rebuilding (for OS/base updates)
  - Consider a `sandbox.py prune` command to automatically remove orphaned sandbox dirs under ~/.sandbox/
  - On `up`, add cli prompt to set git username and email. Default to values from repo, but allow changing them.
  - Warn or error on `up` if the same --name is reused across different repos
    (currently only catches this if meta already exists)
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

_REPO_DEFAULT = "__repo__"  # sentinel: derive name from repo dir

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SANDBOX_HOME = Path.home() / ".sandbox"

NETWORK_NAME = "sandbox-net"          # internal only — sandbox containers cannot reach internet directly
EXTERNAL_NETWORK_NAME = "sandbox-external"  # squid gets this too, for actual internet access
HOST_NETWORK_NAME = "sandbox-host"    # non-internal, for host service access
SQUID_IMAGE = "ubuntu/squid:latest"
SQUID_PORT = 3128


@dataclass
class Config:
    docker: str  # path to podman or docker binary

    @property
    def is_podman(self) -> bool:
        return Path(self.docker).name == "podman"

    @property
    def host_gateway_hostname(self) -> str:
        return "host.containers.internal" if self.is_podman else "host.docker.internal"

    @property
    def host_gateway_add_host(self) -> str | None:
        """Returns --add-host argument for reaching host services, or None if injected automatically."""
        if self.is_podman:
            return f"{self.host_gateway_hostname}:host-gateway"
        if sys.platform == "darwin":
            return None  # Docker Desktop injects host.docker.internal automatically
        # TODO: Linux + Docker — host-gateway requires Docker 20.10+
        return f"{self.host_gateway_hostname}:host-gateway"

    @classmethod
    def detect(cls, override: str | None = None) -> "Config":
        docker = override or shutil.which("podman") or shutil.which("docker")
        if not docker:
            die("Neither podman nor docker found in PATH. Install one or use --docker.")
        return cls(docker=docker)


# module-level config, set in main() after arg parsing
cfg: Config = None  # type: ignore



def load_template(template_dir: Path) -> dict:
    """Load template from a directory.

    A template directory must contain:
      image/Dockerfile  — required, Docker build context
      config.json       — required, keys: mounts (object), inject (array, optional),
                          allowlist (array, optional), denylist (array, optional),
                          host_ports (array of ints, optional)
    And may also contain:
      image/*           — any other files the Dockerfile COPYs

    inject: list of file paths relative to the repo root that are copied into image/
    at provision time (and on `replace`), so the Dockerfile can COPY them into the image.
    These files are seeded once — edits to the repo originals have no effect until `replace`.
    """
    template = {"inject": []}

    template_dir = template_dir.resolve()
    if not template_dir.exists():
        die(f"Template directory not found: {template_dir}")
    if not template_dir.is_dir():
        die(f"Template must be a directory: {template_dir}")

    image_dir = template_dir / "image"
    if not image_dir.exists():
        die(f"Template is missing image/ directory: {template_dir}")
    if not (image_dir / "Dockerfile").exists():
        die(f"Template image/ directory has no Dockerfile: {image_dir}")
    template["image_files"] = {
        str(p.relative_to(image_dir)): p.read_text()
        for p in image_dir.rglob("*") if p.is_file()
    }

    config_path = template_dir / "config.json"
    if not config_path.exists():
        die(f"Template is missing config.json: {template_dir}")
    try:
        cfg_json = json.loads(config_path.read_text())
    except json.JSONDecodeError as e:
        die(f"Invalid JSON in {config_path}: {e}")
    if "mounts" not in cfg_json or not isinstance(cfg_json["mounts"], dict):
        die(f"{config_path}: 'mounts' must be a JSON object")
    template["mounts"] = cfg_json["mounts"]
    template["inject"] = cfg_json.get("inject", [])
    if not isinstance(template["inject"], list):
        die(f"{config_path}: 'inject' must be a JSON array")
    for key in ("allowlist", "denylist", "host_ports"):
        if key in cfg_json:
            template[key] = cfg_json[key]

    return template


# ---------------------------------------------------------------------------
# Sandbox dataclass
# ---------------------------------------------------------------------------

@dataclass
class Sandbox:
    name: str
    repo: Path | None  # None = no-git mode
    # template is only used during first-time provisioning (_provision) and replace.
    # After that, seeded files on disk are the source of truth.
    template: dict | None = None

    # --- Paths ---------------------------------------------------

    @property
    def sandbox_dir(self) -> Path:
        return SANDBOX_HOME / self.name

    @property
    def meta_dir(self) -> Path:
        return self.sandbox_dir / "config"

    @property
    def workspace_dir(self) -> Path:
        return self.sandbox_dir / "workspace"

    @property
    def state_dir(self) -> Path:
        return self.sandbox_dir / "volumes"

    @property
    def squid_container_name(self) -> str:
        return f"sandbox-squid-{self.name}"

    @property
    def container_name(self) -> str:
        return f"sandbox-{self.name}"

    @property
    def image_tag(self) -> str:
        return f"sandbox-{self.name}:latest"

    @property
    def image_dir(self) -> Path:
        return self.meta_dir / "image"

    @property
    def dockerfile_path(self) -> Path:
        return self.image_dir / "Dockerfile"

    @property
    def config_file(self) -> Path:
        return self.meta_dir / "config.json"

    # --- Meta persistence --------------------------------------------------

    @staticmethod
    def _read_meta(name: str) -> dict | None:
        p = SANDBOX_HOME / name / "config" / "meta.json"
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            return None

    @classmethod
    def from_meta(cls, name: str, meta: dict) -> "Sandbox":
        repo_str = meta.get("repo")
        return cls(name=name, repo=Path(repo_str) if repo_str else None)

    @classmethod
    def load(cls, name: str) -> "Sandbox":
        meta = cls._read_meta(name)
        if meta is None:
            die(f"No meta found for sandbox '{name}'. Has it been created with `up`?")
        return cls.from_meta(name, meta)

    @classmethod
    def iter_all(cls) -> "Iterator[Sandbox]":
        if not SANDBOX_HOME.exists():
            return
        for d in sorted(SANDBOX_HOME.iterdir()):
            if not d.is_dir():
                continue
            meta = cls._read_meta(d.name)
            if meta is not None:
                yield cls.from_meta(d.name, meta)

    def save_meta(self, meta: dict):
        p = self.meta_dir / "meta.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(meta, indent=2))


# ---------------------------------------------------------------------------
# Per-sandbox file helpers (operate on seeded files in meta_dir)
# ---------------------------------------------------------------------------

def load_config(sb: Sandbox) -> dict:
    """Load and validate config.json. Dies on missing file or invalid JSON."""
    if not sb.config_file.exists():
        die(f"config.json not found at {sb.config_file}. Instance may be corrupted — re-run `up`.")
    try:
        return json.loads(sb.config_file.read_text())
    except json.JSONDecodeError as e:
        die(f"Malformed config.json at {sb.config_file}: {e}. Instance may be corrupted — re-run `up`.")


def load_mounts(sb: Sandbox) -> dict:
    cfg = load_config(sb)
    if "mounts" not in cfg:
        die(f"config.json at {sb.config_file} is missing 'mounts' key. Instance may be corrupted — re-run `up`.")
    return cfg["mounts"]


# ---------------------------------------------------------------------------
# CLI name/repo resolution
# ---------------------------------------------------------------------------

def find_sandboxes_for_repo(repo: Path) -> list[str]:
    """Return sandbox names whose repo matches the given path."""
    return [sb.name for sb in Sandbox.iter_all()
            if sb.repo and sb.repo.resolve() == repo.resolve()]


def next_available_name(base: str) -> str:
    """Return base if unused, otherwise base-2, base-3, etc."""
    if not (SANDBOX_HOME / base).exists():
        return base
    i = 2
    while (SANDBOX_HOME / f"{base}-{i}").exists():
        i += 1
    return f"{base}-{i}"


def resolve_sandbox(args) -> "Sandbox":
    name_arg = getattr(args, "name", _REPO_DEFAULT)
    repo_arg = getattr(args, "repo", None)

    # Explicit --repo: use it directly, derive name if not given
    if repo_arg:
        repo = Path(repo_arg).resolve()
        name = next_available_name(repo.name) if name_arg == _REPO_DEFAULT else name_arg
        return Sandbox(name=name, repo=repo)

    # Explicit --name: load existing meta if present, otherwise create with that name
    if name_arg != _REPO_DEFAULT:
        meta_path = SANDBOX_HOME / name_arg / "config" / "meta.json"
        if meta_path.exists():
            return Sandbox.load(name_arg)
        # Not yet created — repo optional (no-git mode if not in a repo)
        repo = repo_root()
        return Sandbox(name=name_arg, repo=repo)

    # No --name, no --repo: invert lookup via repo
    repo = repo_root()
    if repo is None:
        die("Not inside a git repository. Use --repo or --name to specify the sandbox.")

    matches = find_sandboxes_for_repo(repo)
    if len(matches) > 1:
        die(f"Multiple sandboxes exist for this repo: {', '.join(sorted(matches))}. "
            "Use --name to specify which one.")
    if len(matches) == 1:
        return Sandbox.load(matches[0])

    # No existing sandbox for this repo — create new with auto-suffixed name
    name = next_available_name(repo.name)
    if name != repo.name:
        print(f"[name] '{repo.name}' already taken by a different repo — using '{name}'")
    return Sandbox(name=name, repo=repo)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run(cmd: list, capture=False, check=True, **kwargs):
    if not capture:
        print(f"  $ {' '.join(str(c) for c in cmd)}")
    result = subprocess.run(
        cmd,
        capture_output=capture,
        text=True,
        check=check,
        **kwargs,
    )
    return result


def _docker_exists(*docker_args: str) -> bool:
    return run([cfg.docker, *docker_args], capture=True, check=False).returncode == 0


def container_exists(name: str) -> bool:
    return _docker_exists("inspect", name)


def container_running(name: str) -> bool:
    r = run(
        [cfg.docker, "inspect", "--format", "{{.State.Running}}", name],
        capture=True,
        check=False,
    )
    return r.returncode == 0 and r.stdout.strip() == "true"


def network_exists(name: str) -> bool:
    return _docker_exists("network", "inspect", name)


def image_exists(name: str) -> bool:
    return _docker_exists("image", "inspect", name)


def repo_root() -> Path | None:
    r = run(["git", "rev-parse", "--show-toplevel"], capture=True, check=False)
    return Path(r.stdout.strip()) if r.returncode == 0 else None


def die(msg: str):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)



def edit_file(path: Path) -> bool:
    mtime_before = path.stat().st_mtime
    subprocess.run([os.environ.get("EDITOR", "vi"), str(path)])
    return path.stat().st_mtime > mtime_before


# ---------------------------------------------------------------------------
# Squid config generation
# ---------------------------------------------------------------------------

def write_squid_conf(sb: Sandbox, allowlist: list[str] | None, denylist: list[str] | None) -> Path:
    conf_dir = sb.meta_dir / "squid"
    conf_dir.mkdir(parents=True, exist_ok=True)

    if allowlist is not None:
        # Allowlist mode: key present in config — only listed domains allowed, everything else denied
        acl_lines = "\n".join(f"acl allowed_domains dstdomain .{d}" for d in allowlist)
        access_rules = (f"{acl_lines}\nhttp_access allow allowed_domains\n" if acl_lines else "") + "http_access deny all"
        mode_comment = "# Mode: allowlist"
    elif denylist is not None and denylist:
        # Denylist mode: listed domains blocked, everything else allowed
        acl_lines = "\n".join(f"acl denied_domains dstdomain .{d}" for d in denylist)
        access_rules = f"{acl_lines}\nhttp_access deny denied_domains\nhttp_access allow all"
        mode_comment = "# Mode: denylist"
    else:
        # Unrestricted: neither key present
        access_rules = "http_access allow all"
        mode_comment = "# Mode: unrestricted"

    conf = f"""\
# Generated by sandbox.py
{mode_comment}
http_port {SQUID_PORT}

# Allow localhost (health checks)
acl localhost src 127.0.0.1/32
http_access allow localhost

{access_rules}

# Reduce log noise
access_log /dev/stdout
cache_log /dev/stderr
cache_store_log none
"""
    conf_path = conf_dir / "squid.conf"
    conf_path.write_text(conf)
    return conf_dir


# ---------------------------------------------------------------------------
# Infrastructure: network + squid
# ---------------------------------------------------------------------------

def ensure_network(host_ports: list[int] | None = None):
    if not network_exists(NETWORK_NAME):
        print(f"[network] Creating {NETWORK_NAME} (internal) ...")
        run([cfg.docker, "network", "create", "--internal", NETWORK_NAME])
    else:
        print(f"[network] {NETWORK_NAME} already exists.")

    if not network_exists(EXTERNAL_NETWORK_NAME):
        print(f"[network] Creating {EXTERNAL_NETWORK_NAME} ...")
        run([cfg.docker, "network", "create", EXTERNAL_NETWORK_NAME])
    else:
        print(f"[network] {EXTERNAL_NETWORK_NAME} already exists.")

    if host_ports:
        if not network_exists(HOST_NETWORK_NAME):
            print(f"[network] Creating {HOST_NETWORK_NAME} (host access) ...")
            run([cfg.docker, "network", "create", HOST_NETWORK_NAME])
        else:
            print(f"[network] {HOST_NETWORK_NAME} already exists.")


def load_sandbox_allowlist(sb: Sandbox) -> list[str] | None:
    """Returns list if 'allowlist' key exists in config (even if empty), None if absent."""
    cfg_data = load_config(sb)
    return cfg_data.get("allowlist", None)


def load_sandbox_denylist(sb: Sandbox) -> list[str] | None:
    """Returns list if 'denylist' key exists in config (even if empty), None if absent."""
    cfg_data = load_config(sb)
    return cfg_data.get("denylist", None)


def ensure_squid(sb: Sandbox):
    allowlist = load_sandbox_allowlist(sb)
    denylist = load_sandbox_denylist(sb)

    if allowlist is not None and denylist is not None and denylist:
        print("[squid] WARNING: both allowlist and denylist set — denylist ignored.")

    # Always recreate the squid container so list changes are picked up on every `up`.
    if container_running(sb.squid_container_name):
        print(f"[squid] Recreating {sb.squid_container_name} to apply latest config ...")
        run([cfg.docker, "stop", "-t", "0", sb.squid_container_name], check=False, capture=True)
        run([cfg.docker, "rm", sb.squid_container_name], check=False, capture=True)
    elif container_exists(sb.squid_container_name):
        print(f"[squid] Removing stopped squid container {sb.squid_container_name} ...")
        run([cfg.docker, "rm", sb.squid_container_name])

    conf_dir = write_squid_conf(sb, allowlist, denylist)
    print(f"[squid] Starting {sb.squid_container_name} ...")
    run([
        cfg.docker, "run", "-d",
        "--name", sb.squid_container_name,
        "--network", NETWORK_NAME,
        "--restart", "unless-stopped",
        "-v", f"{conf_dir}/squid.conf:/etc/squid/squid.conf:ro",
        SQUID_IMAGE,
    ])
    # Also connect squid to the external network so it can reach the internet.
    run([cfg.docker, "network", "connect", EXTERNAL_NETWORK_NAME, sb.squid_container_name])


# ---------------------------------------------------------------------------
# Image build
# ---------------------------------------------------------------------------

def build_image(sb: Sandbox, no_cache=False) -> None:
    """Unconditionally build the image. Relies on Docker's layer cache.

    We always run `docker build` rather than trying to short-circuit it ourselves.
    Docker's layer cache handles this correctly: if a COPYed file hasn't changed,
    the cached layer is reused; if it has, Docker invalidates from that layer forward.

    The build context is image/ (the seeded directory), not the repo root. This means
    the Dockerfile can only access files that were explicitly seeded or injected —
    live changes to the repo never bleed into the image. Use `inject` in config.json
    to pull specific files from the repo into the build context at provision/replace time.
    """
    if not sb.dockerfile_path.exists():
        die(f"Dockerfile not found at {sb.dockerfile_path}. Instance may be corrupted — re-run `up`.")

    print(f"[image] Building {sb.image_tag}{' [no-cache]' if no_cache else ''} ...")
    print(f"[image] Dockerfile: {sb.dockerfile_path}")
    cmd = [cfg.docker, "build", "-t", sb.image_tag, "-f", str(sb.dockerfile_path)]
    if no_cache:
        cmd.append("--no-cache")
    # Build context is image/ — only seeded and injected files are visible to the Dockerfile.
    cmd.append(str(sb.image_dir))
    run(cmd)


# ---------------------------------------------------------------------------
# Git: two-way remote setup
# ---------------------------------------------------------------------------

def setup_git_remotes(sb: Sandbox):
    remote_name = f"agent-{sb.name}"

    r = run(["git", "-C", str(sb.repo), "remote"], capture=True, check=False)
    existing = r.stdout.splitlines()

    if remote_name in existing:
        print(f"[git] Remote '{remote_name}' already exists on host repo.")
        return

    agent_git = sb.workspace_dir / ".git"
    if not agent_git.exists():
        print("[git] Agent clone not yet initialised — remote will be added after first run.")
        print(f"      Run: git -C {sb.repo} remote add {remote_name} {sb.workspace_dir}")
        return

    run(["git", "-C", str(sb.repo), "remote", "add", remote_name, str(sb.workspace_dir)])
    print(f"[git] Added host remote '{remote_name}' → {sb.workspace_dir}")
    print(f"      Fetch with: git fetch {remote_name}")


# ---------------------------------------------------------------------------
# Sandbox container
# ---------------------------------------------------------------------------

def wait_for_clone(sb: Sandbox, timeout=60):
    print(f"[git] Waiting for initial clone in {sb.name}...")
    # We poll workspace_dir on the host, which is the same directory bind-mounted
    # into the container as /llm-workspace. The .git directory appearing there
    # means the entrypoint's `git clone` has completed successfully.
    start_time = time.time()
    while time.time() - start_time < timeout:
        if (sb.workspace_dir / ".git").exists():
            return True
        if not container_running(sb.container_name):
            print("[git] Container stopped unexpectedly during clone.")
            print(f"      Check logs with: {cfg.docker} logs {sb.container_name}")
            return False
        time.sleep(0.5)
    print(f"[git] Timeout: Clone did not complete within {timeout}s.")
    print(f"      Check logs with: {cfg.docker} logs {sb.container_name}")
    return False


def resolve_template_dir(template_arg: str | None) -> Path | None:
    """Resolve template directory from --template arg or .sandbox-template in cwd. Returns None if not found."""
    if template_arg:
        return Path(template_arg)
    candidate = Path.cwd() / ".sandbox-template"
    if candidate.is_dir():
        print(f"[template] Using .sandbox-template from {Path.cwd()}")
        return candidate
    return None


def up(sb: Sandbox, template_dir: Path | None = None, no_cache=False):
    """Provision (first time) and start the sandbox. No-op if already running."""
    print(f"\n=== sandbox up: {sb.name} ===\n")

    if template_dir:
        sb.template = load_template(template_dir)

    if not sb.meta_dir.exists() and sb.template is None:
        die("No existing sandbox found and no template provided. "
            "Run from a directory containing .sandbox-template, or use --template.")

    is_new = not sb.meta_dir.exists()

    host_ports = (sb.template or {}).get("host_ports") or (
        load_config(sb).get("host_ports") if sb.meta_dir.exists() else []
    )
    ensure_network(host_ports)
    try:
        _provision(sb)

        if container_running(sb.container_name):
            print(f"[container] {sb.container_name} is already running.")
            setup_git_remotes(sb)
            return

        # Always build — relies on Docker’s layer cache for efficiency.
        build_image(sb, no_cache=no_cache)

        if container_exists(sb.container_name):
            print(f"[container] Removing stopped container {sb.container_name} ...")
            run([cfg.docker, "rm", sb.container_name], check=False, capture=True)

        _start(sb)
    except BaseException:
        if is_new:
            print(f"\n[up] First-time setup failed — rolling back '{sb.name}' ...")
            _unprovision(sb, force=True)
        raise


def restart(sb: Sandbox, no_cache=False):
    """Stop, rebuild image from seeded files, and restart. Does not re-seed from template."""
    if not sb.meta_dir.exists():
        die(f"Sandbox '{sb.name}' has not been provisioned. Run `sandbox up` first.")
    print(f"\n=== sandbox restart: {sb.name} ===\n")
    down(sb)
    build_image(sb, no_cache=no_cache)
    _start(sb)


def replace(sb: Sandbox, template_dir: Path | None = None, no_cache=False):
    """Wipe meta, re-seed from template, rebuild image, restart. Volumes and workspace are preserved."""
    if not sb.meta_dir.exists():
        die(f"Sandbox '{sb.name}' has not been provisioned. Run `sandbox up` first.")
    if template_dir:
        sb.template = load_template(template_dir)
    if sb.template is None:
        die("No template provided. Run from a directory containing .sandbox-template, or use --template.")
    print(f"\n=== sandbox replace: {sb.name} ===\n")
    print(f"[replace] Stopping and wiping meta for '{sb.name}' ...")
    down(sb)
    _unprovision(sb, force=False)  # keeps workspace and volumes
    _provision(sb)
    build_image(sb, no_cache=no_cache)
    _start(sb)


def _copy_inject_files(sb: Sandbox, template: dict):
    """Copy inject files from the repo into image/ so the Dockerfile can COPY them."""
    if sb.repo is None:
        if template.get("inject"):
            print("[inject] WARNING: inject files specified but no repo configured — skipping.")
        return
    for rel_path in template.get("inject", []):
        src = sb.repo / rel_path
        if not src.exists():
            print(f"[inject] WARNING: {src} not found — skipping.")
            continue
        if not src.is_file():
            print(f"[inject] WARNING: {src} is not a file — skipping.")
            continue
        dest = sb.image_dir / Path(rel_path).name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        print(f"[inject] Copied {src} → {dest}")


def _provision(sb: Sandbox):
    """Seed instance files on first creation only. Idempotent: no-ops if already provisioned."""
    if sb.meta_dir.exists():
        return

    print(f"[provision] First-time setup for '{sb.name}' ...")
    template = sb.template

    sb.meta_dir.mkdir(parents=True, exist_ok=True)
    sb.workspace_dir.mkdir(parents=True, exist_ok=True)
    sb.state_dir.mkdir(parents=True, exist_ok=True)

    config_data = {"mounts": template["mounts"], "inject": template["inject"]}
    if "allowlist" in template:
        config_data["allowlist"] = template["allowlist"]
    if "denylist" in template:
        config_data["denylist"] = template["denylist"]
    if "host_ports" in template:
        config_data["host_ports"] = template["host_ports"]
    sb.config_file.write_text(json.dumps(config_data, indent=2))
    print(f"[provision] Wrote {sb.config_file}")

    sb.image_dir.mkdir(parents=True, exist_ok=True)
    for filename, file_content in template["image_files"].items():
        dest = sb.image_dir / filename
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(file_content)
        print(f"[provision] Wrote {dest}")

    _copy_inject_files(sb, template)

    sb.save_meta({
        "sandbox_name": sb.name,
        "repo": str(sb.repo) if sb.repo else None,
    })
    print(f"[provision] Instance '{sb.name}' provisioned.")


def _start(sb: Sandbox):
    """Start the sandbox container. Assumes _provision() has already run."""
    cfg_data = load_config(sb)
    host_ports = cfg_data.get("host_ports", [])

    ensure_squid(sb)

    proxy_url = f"http://{sb.squid_container_name}:{SQUID_PORT}"

    docker_cmd = [
        cfg.docker, "run", "-d", "-i",
        "--name", sb.container_name,
        "--network", NETWORK_NAME,
        "-e", f"http_proxy={proxy_url}",
        "-e", f"https_proxy={proxy_url}",
        "-e", f"HTTP_PROXY={proxy_url}",
        "-e", f"HTTPS_PROXY={proxy_url}",
        "-e", "no_proxy=localhost,127.0.0.1",
        "-v", f"{sb.workspace_dir}:/llm-workspace:rw",
    ]

    if sb.repo is not None:
        git_dir = sb.repo / ".git"
        docker_cmd.extend(["-v", f"{git_dir}:/repo-git:ro"])

    for rel_path, container_path in load_mounts(sb).items():
        host_path = (sb.state_dir / rel_path).resolve()
        if not host_path.is_relative_to(sb.state_dir.resolve()):
            print(f"WARNING: Skipping unsafe mount path '{rel_path}'")
            continue
        host_path.mkdir(parents=True, exist_ok=True)
        docker_cmd.extend(["-v", f"{host_path}:{container_path}:rw"])

    if host_ports:
        add_host = cfg.host_gateway_add_host
        if add_host:
            docker_cmd.extend(["--add-host", add_host])
        docker_cmd.extend([
            "-e", f"no_proxy=localhost,127.0.0.1,{cfg.host_gateway_hostname}",
        ])

    docker_cmd.extend([sb.image_tag, "bash"])

    print(f"[container] Starting {sb.container_name} ...")
    run(docker_cmd)

    if host_ports:
        run([cfg.docker, "network", "connect", HOST_NETWORK_NAME, sb.container_name])
        print(f"[container] Host services reachable at {cfg.host_gateway_hostname}: {host_ports}")

    print(f"\n[container] {sb.container_name} is up.")
    print(f"  Workspace : {sb.workspace_dir}")
    print(f"  Volumes   : {sb.state_dir}")
    print(f"  Proxy     : {proxy_url}")

    if sb.repo is not None:
        if wait_for_clone(sb):
            setup_git_remotes(sb)

    print(f"\n  Attach with: {Path(sys.argv[0]).name} exec --name {sb.name}")
    print(f"  Or directly: {cfg.docker} exec -it -w /llm-workspace {sb.container_name} bash\n")


def down(sb: Sandbox):
    if container_running(sb.container_name):
        print(f"[container] Stopping {sb.container_name} (volumes preserved) ...")
        run([cfg.docker, "stop", "-t", "0", sb.container_name], check=False)
        run([cfg.docker, "rm", sb.container_name], check=False, capture=True)
    elif container_exists(sb.container_name):
        run([cfg.docker, "rm", sb.container_name], check=False, capture=True)
    else:
        print(f"[container] {sb.container_name} not found.")

    if container_running(sb.squid_container_name):
        print(f"[squid] Stopping {sb.squid_container_name} ...")
        run([cfg.docker, "stop", "-t", "0", sb.squid_container_name], check=False, capture=True)
    if container_exists(sb.squid_container_name):
        run([cfg.docker, "rm", "-f", sb.squid_container_name], check=False, capture=True)


def _unprovision(sb: Sandbox, force: bool = False):
    """Tear down all per-sandbox runtime resources and seeded files.
    Safe to call at any point — all steps are best-effort.
    Does NOT remove the image (expensive, may be reused on retry).
    If force=True, removes workspace and volumes even if they have content.
    """
    # Stop/remove main container
    if container_running(sb.container_name):
        run([cfg.docker, "stop", "-t", "0", sb.container_name], check=False, capture=True)
    if container_exists(sb.container_name):
        run([cfg.docker, "rm", "-f", sb.container_name], check=False, capture=True)

    # Stop/remove squid container
    if container_running(sb.squid_container_name):
        run([cfg.docker, "stop", "-t", "0", sb.squid_container_name], check=False, capture=True)
    if container_exists(sb.squid_container_name):
        run([cfg.docker, "rm", "-f", sb.squid_container_name], check=False, capture=True)

    # Remove git remote on host (may or may not have been added)
    if sb.repo is not None:
        remote_name = f"agent-{sb.name}"
        r = run(["git", "-C", str(sb.repo), "remote"], capture=True, check=False)
        if remote_name in r.stdout.splitlines():
            print(f"[git] Removing host remote '{remote_name}' ...")
            run(["git", "-C", str(sb.repo), "remote", "remove", remote_name], check=False)

    # Remove config dir — workspace and volumes are left intact unless force=True
    if force:
        if sb.sandbox_dir.exists():
            shutil.rmtree(sb.sandbox_dir)
    else:
        if sb.meta_dir.exists():
            shutil.rmtree(sb.meta_dir)
        # Clean up sandbox_dir itself if now empty
        if sb.sandbox_dir.exists() and not any(sb.sandbox_dir.iterdir()):
            sb.sandbox_dir.rmdir()


def destroy(sb: Sandbox):
    """Remove container, image, and config. Workspace and volumes are left intact."""
    if not sb.meta_dir.exists() and not image_exists(sb.image_tag):
        die(f"Sandbox '{sb.name}' not found — nothing to destroy.")
    _unprovision(sb)

    if image_exists(sb.image_tag):
        print(f"[image] Removing {sb.image_tag} ...")
        run([cfg.docker, "rmi", sb.image_tag], check=False)

    print(f"[destroy] {sb.name} destroyed.")

    lingering = [d for d in (sb.workspace_dir, sb.state_dir) if d.exists()]
    if lingering:
        print(f"  Data remaining — remove manually if no longer needed:")
        for d in lingering:
            print(f"    rm -rf {d}")


def infra_down():
    for net in (NETWORK_NAME, EXTERNAL_NETWORK_NAME):
        if network_exists(net):
            print(f"[infra] Removing network {net} ...")
            r = run([cfg.docker, "network", "rm", net], check=False, capture=True)
            if r.returncode != 0:
                die(f"ERROR: Could not remove network {net}.\n"
                    "       It is likely still in use by running sandboxes.")
            else:
                print(f"[infra] {net} removed.")
        else:
            print(f"[infra] Network {net} not found.")


def status():
    r = run([cfg.docker, "ps", "-a", "--format", "{{.Names}} {{.State}}"], capture=True, check=False)
    c_states = {}
    for line in r.stdout.strip().splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2:
            c_states[parts[0]] = parts[1]

    print("\n=== Infrastructure ===")
    net_ok = "✓" if network_exists(NETWORK_NAME) else "✗"
    print(f"  Network (internal): {net_ok} {NETWORK_NAME}")
    ext_ok = "✓" if network_exists(EXTERNAL_NETWORK_NAME) else "✗"
    print(f"  Network (external): {ext_ok} {EXTERNAL_NETWORK_NAME}")

    print("\n=== Sandboxes ===")
    sandboxes = list(Sandbox.iter_all())
    if not sandboxes:
        print("  No sandboxes found.")
    else:
        for sb in sandboxes:
            state = c_states.get(sb.container_name, "no container")
            squid_state = c_states.get(sb.squid_container_name, "no container")
            df_info = str(sb.image_dir) if sb.image_dir.exists() else "None found"
            print(f"  Sandbox: {sb.name}")
            print(f"    State     : {state}")
            print(f"    Squid     : {squid_state}")
            print(f"    Repo      : {sb.repo or 'None (no-git mode)'}")
            print(f"    Volumes   : {sb.state_dir}")
            print(f"    Image dir : {df_info}")
            print()

    if SANDBOX_HOME.exists():
        orphans = [d for d in SANDBOX_HOME.iterdir()
                   if d.is_dir() and not (d / "config" / "meta.json").exists()]
        if orphans:
            print("=== Orphans (dirs without config/meta.json) ===")
            for o in orphans:
                print(f"  {o}")
            print("  (Safe to delete manually or use prune when implemented)\n")


def edit_dockerfile(sb: Sandbox):
    if not sb.dockerfile_path.exists():
        die(f"Dockerfile not found at {sb.dockerfile_path}. Instance may be corrupted — re-run `up`.")

    if edit_file(sb.dockerfile_path):
        print("[ed] Dockerfile changed, rebuilding ...")
        was_running = container_running(sb.container_name)
        try:
            build_image(sb)
        except Exception as e:
            print(f"[ed] Build failed: {e}")
            print(f"[ed] Container left {'running' if was_running else 'stopped'} — no restart.")
            return
        if was_running:
            print(f"[ed] Restarting {sb.container_name} ...")
            down(sb)
            up(sb)
    else:
        print("[ed] No changes.")


def edit_mounts(sb: Sandbox):
    if not sb.config_file.exists():
        die(f"config.json not found at {sb.config_file}. Instance may be corrupted — re-run `up`.")

    if not edit_file(sb.config_file):
        print("[mounts] No changes.")
        return

    load_mounts(sb)  # validates JSON after editing

    print("[mounts] Config updated.")
    if container_running(sb.container_name):
        resp = input(f"Restart {sb.container_name} now to apply changes? [y/N] ").lower()
        if resp == "y":
            down(sb)
            up(sb)


def cmd_init(template_name: str):
    dest = Path.cwd() / ".sandbox-template"
    if dest.exists():
        die(f".sandbox-template already exists in {Path.cwd()}. Remove it first to reinitialise.")

    templates_dir = Path(__file__).parent / "templates"
    if not templates_dir.exists():
        die(f"Templates directory not found at {templates_dir}. Is the package installed correctly?")

    template_dir = templates_dir / template_name
    if not template_dir.exists():
        available = [d.name for d in templates_dir.iterdir() if d.is_dir()]
        die(f"Template '{template_name}' not found. Available: {', '.join(sorted(available))}")

    shutil.copytree(template_dir, dest)
    print(f"[init] Initialised .sandbox-template from '{template_name}' in {Path.cwd()}")
    print(f"  Edit {dest} to customise, then run: sandbox up")


def exec_cmd(sb: Sandbox, cmd: list[str]):
    if not container_running(sb.container_name):
        die(f"{sb.container_name} is not running. Run `sandbox.py up --name {sb.name}` first.")
    cmd = cmd or ["bash"]
    os.execvp(cfg.docker, [cfg.docker, "exec", "-it", "-w", "/llm-workspace", sb.container_name] + cmd)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(
        description="Dev sandbox container manager (podman/docker)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--docker", metavar="BIN", help="Override docker/podman binary")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="Initialise .sandbox-template in current directory")
    p_init.add_argument("template_name", nargs="?", default="default", metavar="TEMPLATE")

    p_up = sub.add_parser("up", help="Provision and start sandbox (no-op if already running)")
    p_up.add_argument("--name", "-n", default=_REPO_DEFAULT)
    p_up.add_argument("--repo", "-r", default=None)
    p_up.add_argument("--template", default=None, metavar="DIR")
    p_up.add_argument("--no-cache", dest="no_cache", action="store_true")

    p_restart = sub.add_parser("restart", help="Rebuild image from seeded files and restart container")
    p_restart.add_argument("--name", "-n", default=_REPO_DEFAULT)
    p_restart.add_argument("--no-cache", dest="no_cache", action="store_true")

    p_replace = sub.add_parser("replace", help="Re-seed from template, rebuild, restart (keeps volumes/workspace)")
    p_replace.add_argument("--name", "-n", default=_REPO_DEFAULT)
    p_replace.add_argument("--template", default=None, metavar="DIR")
    p_replace.add_argument("--no-cache", dest="no_cache", action="store_true")
    p_down = sub.add_parser("down", help="Stop sandbox container (keep volumes/image)")
    p_down.add_argument("--name", "-n", default=_REPO_DEFAULT)

    p_destroy = sub.add_parser("destroy", help="Remove container, image, and meta (preserves workspace/state)")
    p_destroy.add_argument("--name", "-n", default=_REPO_DEFAULT)

    sub.add_parser("status", help="Show all sandboxes and infrastructure")
    sub.add_parser("infra-down", help="Tear down shared network")

    p_ed = sub.add_parser("edit-dockerfile", aliases=["ed"])
    p_ed.add_argument("--name", "-n", default=_REPO_DEFAULT)

    p_em = sub.add_parser("edit-mounts", aliases=["em"])
    p_em.add_argument("--name", "-n", default=_REPO_DEFAULT)

    p_exec = sub.add_parser("exec", help="Exec into running sandbox")
    p_exec.add_argument("--name", "-n", default=_REPO_DEFAULT)
    p_exec.add_argument("cmd", nargs="*", default=["bash"])

    return parser.parse_args()


def main():
    args = parse_args()

    global cfg
    cfg = Config.detect(override=args.docker)

    sb = lambda: resolve_sandbox(args)
    commands = {
        "init":            lambda: cmd_init(args.template_name),
        "up":              lambda: up(sb(), template_dir=resolve_template_dir(args.template), no_cache=args.no_cache),
        "restart":         lambda: restart(sb(), no_cache=args.no_cache),
        "replace":         lambda: replace(sb(), template_dir=resolve_template_dir(args.template), no_cache=args.no_cache),
        "down":            lambda: down(sb()),
        "destroy":         lambda: destroy(sb()),
        "status":          lambda: status(),
        "infra-down":      lambda: infra_down(),
        "edit-dockerfile": lambda: edit_dockerfile(sb()),
        "ed":              lambda: edit_dockerfile(sb()),
        "edit-mounts":     lambda: edit_mounts(sb()),
        "em":              lambda: edit_mounts(sb()),
        "exec":            lambda: exec_cmd(sb(), args.cmd),
    }
    commands[args.command]()

if __name__ == "__main__":
    main()