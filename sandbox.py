#!/usr/bin/env python3
"""
sandbox.py — Dev sandbox container manager (podman/docker, docker-compose-free)

All infrastructure (network, squid proxy) is created if not present — idempotent.
Default sandbox name is derived from the repo directory name.

TODO:
  - Add `--pull` flag to `up` to refresh the base image before rebuilding (for OS/base updates)
  - Orphaned volume dirs in workspaces flagged in `status` but not cleaned automatically;
    consider an `sandbox.py prune` command
  - On `up`, add cli prompt to set git username and email. Default to values from repo, but allow changing them.
  - Warn or error on `up` if the same --name is reused across different repos
    (currently only catches this if meta already exists)

YAGNI / worth revisiting:
  - The allowlist editor (edit-allowlist / reconfigure_squid / write_squid_conf) is ~80 lines for
    a feature that might never be used if the proxy is always deny-all or always the same config.
    Could be replaced with "just edit ~/.sandbox/squid/squid.conf and restart squid manually".
  - Profile files (config.json, Dockerfile, entrypoint.sh) — mounts and rebuild_triggers
    are now unified in config.json; Dockerfile and entrypoint.sh remain separate files as
    they are non-JSON and benefit from syntax highlighting / direct editing.
"""

import argparse
import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

_REPO_DEFAULT = "__repo__"  # sentinel: derive name from repo dir

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SANDBOX_HOME = Path.home() / ".sandbox"
INSTANCES_DIR = SANDBOX_HOME / "instances"
WORKSPACES_DIR = SANDBOX_HOME / "workspaces"
STATE_DIR = SANDBOX_HOME / "state"

NETWORK_NAME = "sandbox-net"
SQUID_CONTAINER_NAME = "sandbox-squid"
SQUID_IMAGE = "ubuntu/squid:latest"
SQUID_PORT = 3128

DEFAULT_ALLOWLIST = [
]

DOCKER = shutil.which("podman") or shutil.which("docker")


DEFAULT_PROFILE = {
    "dockerfile": """\
FROM python:3.13-slim

WORKDIR /workspace

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git curl ca-certificates ripgrep \\
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv ruff ty

CMD ["bash"]
""",
    # Maps relative path in state_dir -> absolute path in container
    "mounts": {
        # example:
        # ".config": "/root/.config"
    },
    "rebuild_triggers": [
        "uv.lock",
        "pyproject.toml",
        "requirements.txt",
        "requirements-dev.txt",
        "package.json",
        "package-lock.json",
        "yarn.lock",
    ],
    "entrypoint_script": """\
#!/bin/bash
set -e

WORKSPACE=/llm-workspace
GIT_MOUNT=/repo-git

# Clone from the mounted .git if workspace is empty
if [ ! -d "$WORKSPACE/.git" ]; then
    echo "[sandbox] Cloning from host .git ..."
    git clone --no-hardlinks "$GIT_MOUNT" "$WORKSPACE"
    cd "$WORKSPACE"
    git config user.email "agent@sandbox"
    git config user.name "Agent"
else
    echo "[sandbox] Workspace already cloned."
    cd "$WORKSPACE"
fi

echo "[sandbox] Ready. Repo: $(git log --oneline -1 2>/dev/null || echo 'empty')"
exec "$@"
""",
}


def load_profile(profile_dir: Path | None) -> dict:
    """Load profile config for `up`. If profile_dir is None, return defaults.

    A profile directory may contain:
      config.json   — optional, keys: mounts (object), rebuild_triggers (array)
      Dockerfile    — optional, replaces the default image definition
      entrypoint.sh — optional, replaces the default container entrypoint
    """
    profile = copy.deepcopy(DEFAULT_PROFILE)
    if profile_dir is None:
        return profile

    profile_dir = profile_dir.resolve()
    if not profile_dir.exists():
        die(f"--profile directory not found: {profile_dir}")
    if not profile_dir.is_dir():
        die(f"--profile must point to a directory: {profile_dir}")

    config_path = profile_dir / "config.json"
    if config_path.exists():
        try:
            cfg = json.loads(config_path.read_text())
        except json.JSONDecodeError as e:
            die(f"Invalid JSON in {config_path}: {e}")
        if "mounts" in cfg:
            if not isinstance(cfg["mounts"], dict):
                die(f"{config_path}: 'mounts' must be a JSON object")
            profile["mounts"] = cfg["mounts"]
        if "rebuild_triggers" in cfg:
            if not isinstance(cfg["rebuild_triggers"], list):
                die(f"{config_path}: 'rebuild_triggers' must be a JSON array")
            profile["rebuild_triggers"] = cfg["rebuild_triggers"]

    dockerfile_path = profile_dir / "Dockerfile"
    if dockerfile_path.exists():
        profile["dockerfile"] = dockerfile_path.read_text()

    entrypoint_path = profile_dir / "entrypoint.sh"
    if entrypoint_path.exists():
        profile["entrypoint_script"] = entrypoint_path.read_text()

    return profile

# ---------------------------------------------------------------------------
# Sandbox dataclass
# ---------------------------------------------------------------------------

@dataclass
class Sandbox:
    name: str
    repo: Path
    # profile is only used during first-time provisioning (_provision).
    # After that, seeded files on disk are the source of truth.
    profile: dict | None = None
    profile_explicit: bool = False

    # --- Paths ---------------------------------------------------

    @property
    def meta_dir(self) -> Path:
        return INSTANCES_DIR / self.name

    @property
    def workspace_dir(self) -> Path:
        return WORKSPACES_DIR / self.name

    @property
    def state_dir(self) -> Path:
        return STATE_DIR / self.name

    @property
    def container_name(self) -> str:
        return f"sandbox-{self.name}"

    @property
    def image_tag(self) -> str:
        return f"sandbox-{self.name}:latest"

    @property
    def dockerfile_path(self) -> Path:
        return self.meta_dir / "Dockerfile"

    @property
    def config_file(self) -> Path:
        return self.meta_dir / "config.json"

    @property
    def entrypoint_path(self) -> Path:
        return self.meta_dir / "entrypoint.sh"

    # --- Meta persistence --------------------------------------------------

    def load_meta(self) -> dict:
        p = self.meta_dir / "meta.json"
        return json.loads(p.read_text()) if p.exists() else {}

    def save_meta(self, meta: dict):
        p = self.meta_dir / "meta.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(meta, indent=2))

    def update_meta(self, patch: dict):
        meta = self.load_meta()
        meta.update(patch)
        self.save_meta(meta)

    # --- Alternative constructor ------------------------------------------

    @classmethod
    def load(cls, name: str) -> "Sandbox":
        meta_path = INSTANCES_DIR / name / "meta.json"
        if not meta_path.exists():
            die(f"No meta found for sandbox '{name}'. Has it been created with `up`?")
        meta = json.loads(meta_path.read_text())
        repo = meta.get("repo")
        if not repo:
            die(f"meta.json for sandbox '{name}' is missing 'repo' field.")
        return cls(name=name, repo=Path(repo))


# ---------------------------------------------------------------------------
# Per-sandbox file helpers (operate on seeded files in meta_dir)
# ---------------------------------------------------------------------------

def resolve_dockerfile(sb: Sandbox, explicit_dockerfile: str = None) -> Path:
    """Resolve Dockerfile: explicit path if given, otherwise the seeded instance default."""
    if explicit_dockerfile:
        p = Path(explicit_dockerfile).resolve()
        if not p.exists():
            raise FileNotFoundError(f"--dockerfile not found: {p}")
        return p
    if not sb.dockerfile_path.exists():
        profile = sb.profile or copy.deepcopy(DEFAULT_PROFILE)
        sb.dockerfile_path.write_text(profile["dockerfile"])
        print(f"[image] Wrote default Dockerfile to {sb.dockerfile_path}")
    return sb.dockerfile_path


def load_mounts(sb: Sandbox) -> dict:
    """Load mount mappings from seeded config.json. Falls back to profile defaults."""
    if sb.config_file.exists():
        try:
            cfg = json.loads(sb.config_file.read_text())
            if "mounts" in cfg:
                return cfg["mounts"]
        except json.JSONDecodeError:
            print(f"WARNING: Malformed {sb.config_file}. Using defaults.")
    profile = sb.profile or copy.deepcopy(DEFAULT_PROFILE)
    return profile["mounts"]


# ---------------------------------------------------------------------------
# CLI name/repo resolution
# ---------------------------------------------------------------------------

def resolve_sandbox(args) -> "Sandbox":
    name_arg = getattr(args, "name", _REPO_DEFAULT)
    repo_arg = getattr(args, "repo", None)

    if repo_arg:
        repo = Path(repo_arg).resolve()
        name = repo.name if name_arg == _REPO_DEFAULT else name_arg
        return Sandbox(name=name, repo=repo)

    if name_arg != _REPO_DEFAULT:
        meta_path = INSTANCES_DIR / name_arg / "meta.json"
        if meta_path.exists():
            return Sandbox.load(name_arg)
        try:
            repo = repo_root()
        except SystemExit:
            die(f"Sandbox '{name_arg}' not yet created and not inside a git repo. "
                "Use --repo to specify the repo path.")
        return Sandbox(name=name_arg, repo=repo)

    try:
        repo = repo_root()
    except SystemExit:
        die("Not inside a git repository. Use --repo or --name to specify the sandbox.")
    return Sandbox(name=repo.name, repo=repo)


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
    return run([DOCKER, *docker_args], capture=True, check=False).returncode == 0


def container_exists(name: str) -> bool:
    return _docker_exists("inspect", name)


def container_running(name: str) -> bool:
    r = run(
        [DOCKER, "inspect", "--format", "{{.State.Running}}", name],
        capture=True,
        check=False,
    )
    return r.returncode == 0 and r.stdout.strip() == "true"


def network_exists(name: str) -> bool:
    return _docker_exists("network", "inspect", name)


def image_exists(name: str) -> bool:
    return _docker_exists("image", "inspect", name)


def repo_root() -> Path:
    r = run(["git", "rev-parse", "--show-toplevel"], capture=True, check=False)
    if r.returncode != 0:
        die("Not inside a git repository.")
    return Path(r.stdout.strip())


def die(msg: str):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def hash_files(paths: list[Path]) -> str:
    h = hashlib.sha256()
    for p in sorted(paths):
        if p.exists():
            h.update(p.read_bytes())
    return h.hexdigest()[:16]


def edit_file(path: Path) -> bool:
    mtime_before = path.stat().st_mtime
    subprocess.run([os.environ.get("EDITOR", "vi"), str(path)])
    return path.stat().st_mtime > mtime_before


# ---------------------------------------------------------------------------
# Squid config generation
# ---------------------------------------------------------------------------

def write_squid_conf(allowlist: list[str]) -> Path:
    conf_dir = SANDBOX_HOME / "squid"
    conf_dir.mkdir(parents=True, exist_ok=True)
    acl_lines = "\n".join(f"acl allowed_domains dstdomain .{d}" for d in allowlist)
    conf = f"""\
# Generated by sandbox.py
http_port {SQUID_PORT}

# Allow localhost (health checks)
acl localhost src 127.0.0.1/32

# Domain allowlist
{acl_lines}

http_access allow localhost
{"http_access allow allowed_domains" if acl_lines else ""}
http_access deny all

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

def ensure_network():
    if network_exists(NETWORK_NAME):
        print(f"[network] {NETWORK_NAME} already exists.")
        return
    print(f"[network] Creating {NETWORK_NAME} ...")
    run([DOCKER, "network", "create", NETWORK_NAME])


def ensure_squid(allowlist: list[str] = None):
    allowlist = allowlist or DEFAULT_ALLOWLIST

    if container_running(SQUID_CONTAINER_NAME):
        print(f"[squid] {SQUID_CONTAINER_NAME} already running.")
        return

    if container_exists(SQUID_CONTAINER_NAME):
        print("[squid] Removing stopped squid container ...")
        run([DOCKER, "rm", SQUID_CONTAINER_NAME])

    conf_dir = write_squid_conf(allowlist)
    print("[squid] Starting squid proxy ...")
    run([
        DOCKER, "run", "-d",
        "--name", SQUID_CONTAINER_NAME,
        "--network", NETWORK_NAME,
        "--restart", "unless-stopped",
        "-v", f"{conf_dir}/squid.conf:/etc/squid/squid.conf:ro",
        SQUID_IMAGE,
    ])


# ---------------------------------------------------------------------------
# Global allowlist editor
# ---------------------------------------------------------------------------

GLOBAL_ALLOWLIST_FILE = SANDBOX_HOME / "allowlist.txt"


def load_global_allowlist() -> list[str]:
    if not GLOBAL_ALLOWLIST_FILE.exists():
        return list(DEFAULT_ALLOWLIST)
    return [line.strip() for line in GLOBAL_ALLOWLIST_FILE.read_text().splitlines()
            if line.strip() and not line.startswith("#")]


def reconfigure_squid(allowlist: list[str]):
    write_squid_conf(allowlist)
    if not container_running(SQUID_CONTAINER_NAME):
        print("[squid] Not running — config saved, will apply on next start.")
        return
    print("[squid] Restarting squid ...")
    run([DOCKER, "restart", SQUID_CONTAINER_NAME])


def edit_allowlist():
    SANDBOX_HOME.mkdir(parents=True, exist_ok=True)
    # Seed the file with the header comment only on first creation.
    # Never overwrite before editing — that would dirty mtime even on no-op edits.
    if not GLOBAL_ALLOWLIST_FILE.exists():
        GLOBAL_ALLOWLIST_FILE.write_text(
            "# Global squid allowlist (one domain per line, # = comment)\n"
            + "".join(d + "\n" for d in load_global_allowlist())
        )

    if not edit_file(GLOBAL_ALLOWLIST_FILE):
        print("[allowlist] No changes.")
        return

    allowlist = load_global_allowlist()
    print(f"[allowlist] {len(allowlist)} domain(s) saved; reconfiguring squid ...")
    reconfigure_squid(allowlist)


# ---------------------------------------------------------------------------
# Image build
# ---------------------------------------------------------------------------

def image_needs_rebuild(sb: Sandbox, force: bool) -> tuple[bool, str]:
    """Pure read: check whether the image needs to be rebuilt.

    Hashes repo trigger files AND the seeded Dockerfile so that manual edits
    to it are reflected on the next `up`. config.json is excluded — mounts are
    a runtime concern (passed as -v flags) with no bearing on image contents.
    """
    profile = sb.profile or DEFAULT_PROFILE
    triggers = profile["rebuild_triggers"]
    if sb.config_file.exists():
        try:
            cfg = json.loads(sb.config_file.read_text())
            triggers = cfg.get("rebuild_triggers", triggers)
        except json.JSONDecodeError:
            pass
    trigger_files = [sb.repo / f for f in triggers]
    # Include the seeded Dockerfile so manual edits automatically trigger a rebuild.
    instance_files = [sb.dockerfile_path]
    current_hash = hash_files(trigger_files + instance_files)

    if force:
        return True, current_hash

    meta = sb.load_meta()
    last_hash = meta.get("image_hash")

    if image_exists(sb.image_tag) and last_hash == current_hash:
        print(f"[image] {sb.image_tag} is up to date (hash {current_hash}).")
        return False, current_hash

    return True, current_hash


def build_image(sb: Sandbox, current_hash: str, no_cache=False, explicit_dockerfile: str = None) -> None:
    """Unconditionally build the image."""
    dockerfile = resolve_dockerfile(sb, explicit_dockerfile=explicit_dockerfile)

    print(f"[image] Building {sb.image_tag} (trigger hash: {current_hash}){' [no-cache]' if no_cache else ''} ...")
    print(f"[image] Dockerfile: {dockerfile}")
    cmd = [DOCKER, "build", "-t", sb.image_tag, "-f", str(dockerfile)]
    if no_cache:
        cmd.append("--no-cache")
    cmd.append(str(sb.repo))
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
            print(f"      Check logs with: {DOCKER} logs {sb.container_name}")
            return False
        time.sleep(0.5)
    print(f"[git] Timeout: Clone did not complete within {timeout}s.")
    print(f"      Check logs with: {DOCKER} logs {sb.container_name}")
    return False


def _ensure_infra():
    """Preflight: bring up shared network and squid proxy if not already running."""
    ensure_network()
    ensure_squid(load_global_allowlist())


def up(sb: Sandbox, force_rebuild=False, no_cache=False, explicit_dockerfile: str = None):
    print(f"\n=== sandbox up: {sb.name} ===\n")

    _ensure_infra()
    _provision(sb)

    # Build image — may raise; image_needs_rebuild is a pure read.
    needs_rebuild, image_hash = image_needs_rebuild(sb, force=force_rebuild)
    if needs_rebuild:
        build_image(sb, image_hash, no_cache=no_cache, explicit_dockerfile=explicit_dockerfile)
        sb.update_meta({"image_hash": image_hash})

    if container_running(sb.container_name):
        print(f"[container] {sb.container_name} is already running.")
        setup_git_remotes(sb)
        return

    if container_exists(sb.container_name):
        print(f"[container] Removing stopped container {sb.container_name} ...")
        run([DOCKER, "rm", sb.container_name], check=False, capture=True)

    _start(sb, explicit_dockerfile=explicit_dockerfile)


def _provision(sb: Sandbox):
    """Seed instance files on first creation only. Idempotent: no-ops if already provisioned."""
    if sb.meta_dir.exists():
        if sb.profile_explicit:
            print(f"[profile] Instance '{sb.name}' already exists — ignoring --profile to protect manual edits.")
            print(f"          To reset, run: sandbox.py destroy --name {sb.name} && sandbox.py up --profile ...")
        return

    print(f"[provision] First-time setup for '{sb.name}' ...")
    profile = sb.profile or copy.deepcopy(DEFAULT_PROFILE)

    sb.meta_dir.mkdir(parents=True, exist_ok=True)
    sb.workspace_dir.mkdir(parents=True, exist_ok=True)
    sb.state_dir.mkdir(parents=True, exist_ok=True)

    config_data = {"mounts": profile["mounts"], "rebuild_triggers": profile["rebuild_triggers"]}
    sb.config_file.write_text(json.dumps(config_data, indent=2))
    print(f"[provision] Wrote {sb.config_file}")

    sb.dockerfile_path.write_text(profile["dockerfile"])
    print(f"[provision] Wrote {sb.dockerfile_path}")

    sb.entrypoint_path.write_text(profile["entrypoint_script"])
    sb.entrypoint_path.chmod(0o755)
    print(f"[provision] Wrote {sb.entrypoint_path}")

    sb.save_meta({
        "sandbox_name": sb.name,
        "repo": str(sb.repo),
        "workspace_dir": str(sb.workspace_dir),
        "state_dir": str(sb.state_dir),
        "container": sb.container_name,
    })
    print(f"[provision] Instance '{sb.name}' provisioned.")


def _start(sb: Sandbox, explicit_dockerfile: str = None):
    """Start the sandbox container. Assumes _provision() has already run."""
    if not sb.entrypoint_path.exists():
        die(f"entrypoint.sh not found at {sb.entrypoint_path}. Was the instance provisioned?")

    proxy_url = f"http://{SQUID_CONTAINER_NAME}:{SQUID_PORT}"
    git_dir = sb.repo / ".git"

    docker_cmd = [
        DOCKER, "run", "-d", "-i",
        "--name", sb.container_name,
        "--network", NETWORK_NAME,
        "-e", f"http_proxy={proxy_url}",
        "-e", f"https_proxy={proxy_url}",
        "-e", f"HTTP_PROXY={proxy_url}",
        "-e", f"HTTPS_PROXY={proxy_url}",
        "-e", "no_proxy=localhost,127.0.0.1",
        "-v", f"{git_dir}:/repo-git:ro",
        "-v", f"{sb.workspace_dir}:/llm-workspace:rw",
        "-v", f"{sb.entrypoint_path}:/entrypoint.sh:ro",
    ]

    for rel_path, container_path in load_mounts(sb).items():
        host_path = (sb.state_dir / rel_path).resolve()
        if not host_path.is_relative_to(sb.state_dir.resolve()):
            print(f"WARNING: Skipping unsafe mount path '{rel_path}'")
            continue
        host_path.mkdir(parents=True, exist_ok=True)
        docker_cmd.extend(["-v", f"{host_path}:{container_path}:rw"])

    docker_cmd.extend([
        "--entrypoint", "/entrypoint.sh",
        sb.image_tag,
        "bash",
    ])

    print(f"[container] Starting {sb.container_name} ...")
    run(docker_cmd)  # raises CalledProcessError on failure

    print(f"\n[container] {sb.container_name} is up.")
    print(f"  Workspace : {sb.workspace_dir}")
    print(f"  State     : {sb.state_dir}")
    print(f"  Proxy     : {proxy_url}")

    if wait_for_clone(sb):
        setup_git_remotes(sb)

    print(f"\n  Attach with: {Path(sys.argv[0]).name} exec --name {sb.name}")
    print(f"  Or directly: {DOCKER} exec -it -w /llm-workspace {sb.container_name} bash\n")


def down(sb: Sandbox):
    if container_running(sb.container_name):
        print(f"[container] Stopping {sb.container_name} (volumes preserved) ...")
        run([DOCKER, "stop", "-t", "0", sb.container_name], check=False)
        run([DOCKER, "rm", sb.container_name], check=False, capture=True)
    elif container_exists(sb.container_name):
        run([DOCKER, "rm", sb.container_name], check=False, capture=True)
    else:
        print(f"[container] {sb.container_name} not found.")


def destroy(sb: Sandbox):
    """Remove all cheap/dangling resources. Workspace and state dirs are left intact."""
    down(sb)
    if image_exists(sb.image_tag):
        print(f"[image] Removing {sb.image_tag} ...")
        run([DOCKER, "rmi", sb.image_tag], check=False)

    remote_name = f"agent-{sb.name}"
    r = run(["git", "-C", str(sb.repo), "remote"], capture=True, check=False)
    if remote_name in r.stdout.splitlines():
        print(f"[git] Removing host remote '{remote_name}' ...")
        run(["git", "-C", str(sb.repo), "remote", "remove", remote_name], check=False)

    if sb.meta_dir.exists():
        shutil.rmtree(sb.meta_dir)

    print(f"[destroy] {sb.name} destroyed.")
    print(f"  Workspace and state preserved — remove manually if needed:")
    print(f"    rm -rf {sb.workspace_dir} {sb.state_dir}")


def infra_down():
    errors = False

    if container_exists(SQUID_CONTAINER_NAME):
        print(f"[infra] Removing {SQUID_CONTAINER_NAME} ...")
        run([DOCKER, "stop", "-t", "0", SQUID_CONTAINER_NAME], check=False, capture=True)
        r = run([DOCKER, "rm", "-f", SQUID_CONTAINER_NAME], check=False, capture=True)
        if r.returncode != 0:
            print(f"ERROR: Could not remove container {SQUID_CONTAINER_NAME}: {r.stderr.strip()}")
            errors = True

    if network_exists(NETWORK_NAME):
        print(f"[infra] Removing network {NETWORK_NAME} ...")
        r = run([DOCKER, "network", "rm", NETWORK_NAME], check=False, capture=True)
        if r.returncode != 0:
            print(f"ERROR: Could not remove network {NETWORK_NAME}.")
            print("       It is likely still in use by running sandboxes.")
            errors = True

    if errors:
        die("infra-down failed to clean up all resources.")
    else:
        print("[infra] Infrastructure removed successfully.")


def status():
    r = run([DOCKER, "ps", "-a", "--format", "{{.Names}} {{.State}}"], capture=True, check=False)
    c_states = {}
    for line in r.stdout.strip().splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2:
            c_states[parts[0]] = parts[1]

    print("\n=== Infrastructure ===")
    net_ok = "✓" if network_exists(NETWORK_NAME) else "✗"
    print(f"  Network    : {net_ok} {NETWORK_NAME}")

    sq_state = c_states.get(SQUID_CONTAINER_NAME, "absent")
    print(f"  Squid Proxy: {sq_state}")

    print("\n=== Sandboxes ===")
    if not INSTANCES_DIR.exists():
        print("  No sandboxes found.")
    else:
        instances = sorted([d for d in INSTANCES_DIR.iterdir() if d.is_dir()])
        if not instances:
            print("  No sandboxes found.")
        else:
            for d in instances:
                name = d.name
                meta_path = d / "meta.json"
                meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}

                repo_str = meta.get("repo", "Unknown")

                container_name = f"sandbox-{name}"
                state = c_states.get(container_name, "no container")

                df = d / "Dockerfile"
                df_info = str(df) if df.exists() else "None found"

                print(f"  Sandbox: {name}")
                print(f"    State     : {state}")
                print(f"    Repo      : {repo_str}")
                print(f"    Dockerfile: {df_info}")
                print()

    if WORKSPACES_DIR.exists():
        known_names = {d.name for d in INSTANCES_DIR.iterdir()} if INSTANCES_DIR.exists() else set()
        orphans = [d for d in WORKSPACES_DIR.iterdir() if d.is_dir() and d.name not in known_names]
        if orphans:
            print("=== Orphans (Workspaces without metadata) ===")
            for o in orphans:
                print(f"  {o}")
            print("  (Safe to delete manually or use prune when implemented)\n")


def edit_dockerfile(sb: Sandbox):
    dockerfile = resolve_dockerfile(sb)

    if edit_file(dockerfile):
        print("[ed] Dockerfile changed, rebuilding ...")
        was_running = container_running(sb.container_name)
        try:
            _, new_hash = image_needs_rebuild(sb, force=True)
            build_image(sb, new_hash)
            sb.update_meta({"image_hash": new_hash})
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
        profile = sb.profile or copy.deepcopy(DEFAULT_PROFILE)
        cfg = {"mounts": profile["mounts"], "rebuild_triggers": profile["rebuild_triggers"]}
        sb.config_file.write_text(json.dumps(cfg, indent=2))
        print(f"[mounts] No config found — wrote default to {sb.config_file}")

    if not edit_file(sb.config_file):
        print("[mounts] No changes.")
        return

    try:
        json.loads(sb.config_file.read_text())
    except json.JSONDecodeError as e:
        die(f"Invalid JSON in {sb.config_file}: {e}")

    print("[mounts] Config updated.")
    if container_running(sb.container_name):
        resp = input(f"Restart {sb.container_name} now to apply changes? [y/N] ").lower()
        if resp == "y":
            down(sb)
            up(sb)


def exec_cmd(sb: Sandbox, cmd: list[str]):
    if not container_running(sb.container_name):
        die(f"{sb.container_name} is not running. Run `sandbox.py up --name {sb.name}` first.")
    cmd = cmd or ["bash"]
    os.execvp(DOCKER, [DOCKER, "exec", "-it", "-w", "/llm-workspace", sb.container_name] + cmd)


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

    p_up = sub.add_parser("up", help="Create/start sandbox (idempotent)")
    p_up.add_argument("--name", "-n", default=_REPO_DEFAULT)
    p_up.add_argument("--repo", "-r", default=None)
    p_up.add_argument("--profile", default=None, metavar="DIR")
    p_up.add_argument("--rebuild", action="store_true")
    p_up.add_argument("--no-cache", dest="no_cache", action="store_true")
    p_up.add_argument("--dockerfile", default=None, metavar="PATH")

    p_down = sub.add_parser("down", help="Stop sandbox container (keep volumes/image)")
    p_down.add_argument("--name", "-n", default=_REPO_DEFAULT)

    p_destroy = sub.add_parser("destroy", help="Remove container, image, and meta (preserves workspace/state)")
    p_destroy.add_argument("--name", "-n", default=_REPO_DEFAULT)

    sub.add_parser("status", help="Show all sandboxes and infrastructure")
    sub.add_parser("infra-down", help="Tear down shared squid container and network")
    sub.add_parser("edit-allowlist", aliases=["ea"])

    p_ed = sub.add_parser("edit-dockerfile", aliases=["ed"])
    p_ed.add_argument("--name", "-n", default=_REPO_DEFAULT)
    p_ed.add_argument("--dockerfile", default=None, metavar="PATH")

    p_em = sub.add_parser("edit-mounts", aliases=["em"])
    p_em.add_argument("--name", "-n", default=_REPO_DEFAULT)

    p_exec = sub.add_parser("exec", help="Exec into running sandbox")
    p_exec.add_argument("--name", "-n", default=_REPO_DEFAULT)
    p_exec.add_argument("cmd", nargs="*", default=["bash"])

    return parser.parse_args()


def main():
    args = parse_args()

    global DOCKER
    if args.docker:
        DOCKER = args.docker
    if not DOCKER:
        die("Neither podman nor docker found in PATH. Install one or use --docker.")

    if args.command == "up":
        sb = resolve_sandbox(args)
        sb.profile = load_profile(Path(args.profile)) if args.profile else load_profile(None)
        sb.profile_explicit = bool(args.profile)
        up(sb, force_rebuild=args.rebuild or args.no_cache, no_cache=args.no_cache,
           explicit_dockerfile=args.dockerfile)
    elif args.command == "down":
        sb = resolve_sandbox(args)
        down(sb)
    elif args.command == "destroy":
        sb = resolve_sandbox(args)
        destroy(sb)
    elif args.command == "status":
        status()
    elif args.command == "infra-down":
        infra_down()
    elif args.command in ("edit-allowlist", "ea"):
        edit_allowlist()
    elif args.command in ("edit-dockerfile", "ed"):
        sb = resolve_sandbox(args)
        edit_dockerfile(sb)
    elif args.command in ("edit-mounts", "em"):
        sb = resolve_sandbox(args)
        edit_mounts(sb)
    elif args.command == "exec":
        sb = resolve_sandbox(args)
        exec_cmd(sb, args.cmd)


if __name__ == "__main__":
    main()