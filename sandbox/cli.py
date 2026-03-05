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
from dataclasses import dataclass
from pathlib import Path

_REPO_DEFAULT = "__repo__"  # sentinel: derive name from repo dir

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SANDBOX_HOME = Path.home() / ".sandbox"

NETWORK_NAME = "sandbox-net"
SQUID_IMAGE = "ubuntu/squid:latest"
SQUID_PORT = 3128

DOCKER = shutil.which("podman") or shutil.which("docker")



def load_profile(profile_dir: Path) -> dict:
    """Load profile from a directory.

    A profile directory must contain:
      image/Dockerfile  — required, Docker build context
      config.json       — required, keys: mounts (object)
    And may also contain:
      image/*           — any other files the Dockerfile COPYs
      allowlist.txt     — optional, one domain per line
    """
    profile = {
        "allowlist": [],
    }

    profile_dir = profile_dir.resolve()
    if not profile_dir.exists():
        die(f"Profile directory not found: {profile_dir}")
    if not profile_dir.is_dir():
        die(f"Profile must be a directory: {profile_dir}")

    image_dir = profile_dir / "image"
    if not image_dir.exists():
        die(f"Profile is missing image/ directory: {profile_dir}")
    if not (image_dir / "Dockerfile").exists():
        die(f"Profile image/ directory has no Dockerfile: {image_dir}")
    profile["image_files"] = {
        str(p.relative_to(image_dir)): p.read_text()
        for p in image_dir.rglob("*") if p.is_file()
    }

    config_path = profile_dir / "config.json"
    if not config_path.exists():
        die(f"Profile is missing config.json: {profile_dir}")
    try:
        cfg = json.loads(config_path.read_text())
    except json.JSONDecodeError as e:
        die(f"Invalid JSON in {config_path}: {e}")
    if "mounts" not in cfg or not isinstance(cfg["mounts"], dict):
        die(f"{config_path}: 'mounts' must be a JSON object")
    profile["mounts"] = cfg["mounts"]

    allowlist_path = profile_dir / "allowlist.txt"
    if allowlist_path.exists():
        profile["allowlist"] = [
            line.strip() for line in allowlist_path.read_text().splitlines()
            if line.strip() and not line.startswith("#")
        ]

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
    def allowlist_path(self) -> Path:
        return self.meta_dir / "allowlist.txt"

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
        meta_path = SANDBOX_HOME / name / "config" / "meta.json"
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

def resolve_dockerfile(sb: Sandbox) -> Path:
    """Return the seeded Dockerfile path, dying if missing."""
    if not sb.dockerfile_path.exists():
        die(f"Dockerfile not found at {sb.dockerfile_path}. Instance may be corrupted — re-run `up`.")
    return sb.dockerfile_path


def load_config(sb: Sandbox) -> dict:
    """Load and validate config.json. Dies on missing file or invalid JSON."""
    if not sb.config_file.exists():
        die(f"config.json not found at {sb.config_file}. Instance may be corrupted — re-run `up`.")
    try:
        return json.loads(sb.config_file.read_text())
    except json.JSONDecodeError as e:
        die(f"Malformed config.json at {sb.config_file}: {e}. Instance may be corrupted — re-run `up`.")


def load_mounts(sb: Sandbox) -> dict:
    """Load mount mappings from seeded config.json."""
    cfg = load_config(sb)
    if "mounts" not in cfg:
        die(f"config.json at {sb.config_file} is missing 'mounts' key. Instance may be corrupted — re-run `up`.")
    return cfg["mounts"]


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
        meta_path = SANDBOX_HOME / name_arg / "config" / "meta.json"
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



def edit_file(path: Path) -> bool:
    mtime_before = path.stat().st_mtime
    subprocess.run([os.environ.get("EDITOR", "vi"), str(path)])
    return path.stat().st_mtime > mtime_before


# ---------------------------------------------------------------------------
# Squid config generation
# ---------------------------------------------------------------------------

def write_squid_conf(sb: Sandbox, allowlist: list[str]) -> Path:
    conf_dir = sb.meta_dir / "squid"
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


def load_sandbox_allowlist(sb: Sandbox) -> list[str]:
    if not sb.allowlist_path.exists():
        return []
    return [line.strip() for line in sb.allowlist_path.read_text().splitlines()
            if line.strip() and not line.startswith("#")]


def ensure_squid(sb: Sandbox):
    allowlist = load_sandbox_allowlist(sb)

    if container_running(sb.squid_container_name):
        print(f"[squid] {sb.squid_container_name} already running.")
        return

    if container_exists(sb.squid_container_name):
        print(f"[squid] Removing stopped squid container {sb.squid_container_name} ...")
        run([DOCKER, "rm", sb.squid_container_name])

    conf_dir = write_squid_conf(sb, allowlist)
    print(f"[squid] Starting {sb.squid_container_name} ...")
    run([
        DOCKER, "run", "-d",
        "--name", sb.squid_container_name,
        "--network", NETWORK_NAME,
        "--restart", "unless-stopped",
        "-v", f"{conf_dir}/squid.conf:/etc/squid/squid.conf:ro",
        SQUID_IMAGE,
    ])


# ---------------------------------------------------------------------------
# Per-sandbox allowlist editor
# ---------------------------------------------------------------------------


def reconfigure_squid(sb: Sandbox):
    allowlist = load_sandbox_allowlist(sb)
    write_squid_conf(sb, allowlist)
    if not container_running(sb.squid_container_name):
        print("[squid] Not running — config saved, will apply on next start.")
        return
    print(f"[squid] Restarting {sb.squid_container_name} ...")
    run([DOCKER, "restart", sb.squid_container_name])


def edit_allowlist(sb: Sandbox):
    if not sb.allowlist_path.exists():
        die(f"allowlist.txt not found at {sb.allowlist_path}. Instance may be corrupted — re-run `up`.")

    if not edit_file(sb.allowlist_path):
        print("[allowlist] No changes.")
        return

    allowlist = load_sandbox_allowlist(sb)
    print(f"[allowlist] {len(allowlist)} domain(s) saved; reconfiguring squid ...")
    reconfigure_squid(sb)


# ---------------------------------------------------------------------------
# Image build
# ---------------------------------------------------------------------------

def build_image(sb: Sandbox, no_cache=False) -> None:
    """Unconditionally build the image. Relies on Docker's layer cache."""
    dockerfile = resolve_dockerfile(sb)

    print(f"[image] Building {sb.image_tag}{' [no-cache]' if no_cache else ''} ...")
    print(f"[image] Dockerfile: {dockerfile}")
    cmd = [DOCKER, "build", "-t", sb.image_tag, "-f", str(dockerfile)]
    if no_cache:
        cmd.append("--no-cache")
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
            print(f"      Check logs with: {DOCKER} logs {sb.container_name}")
            return False
        time.sleep(0.5)
    print(f"[git] Timeout: Clone did not complete within {timeout}s.")
    print(f"      Check logs with: {DOCKER} logs {sb.container_name}")
    return False


def up(sb: Sandbox, no_cache=False):
    print(f"\n=== sandbox up: {sb.name} ===\n")

    is_new = not sb.meta_dir.exists()

    ensure_network()
    try:
        _provision(sb)

        # Always build — relies on Docker's layer cache for efficiency.
        build_image(sb, no_cache=no_cache)

        if container_running(sb.container_name):
            print(f"[container] {sb.container_name} is already running.")
            setup_git_remotes(sb)
            return

        if container_exists(sb.container_name):
            print(f"[container] Removing stopped container {sb.container_name} ...")
            run([DOCKER, "rm", sb.container_name], check=False, capture=True)

        _start(sb)
    except BaseException:
        if is_new:
            print(f"\n[up] First-time setup failed — rolling back '{sb.name}' ...")
            _unprovision(sb, force=True)
        raise


def _provision(sb: Sandbox):
    """Seed instance files on first creation only. Idempotent: no-ops if already provisioned."""
    if sb.meta_dir.exists():
        if sb.profile_explicit:
            print(f"[profile] Instance '{sb.name}' already exists — ignoring --profile to protect manual edits.")
            print(f"          To reset, run: sandbox.py destroy --name {sb.name} && sandbox.py up --profile ...")
        return

    print(f"[provision] First-time setup for '{sb.name}' ...")
    profile = sb.profile

    sb.meta_dir.mkdir(parents=True, exist_ok=True)
    sb.workspace_dir.mkdir(parents=True, exist_ok=True)
    sb.state_dir.mkdir(parents=True, exist_ok=True)

    config_data = {"mounts": profile["mounts"]}
    sb.config_file.write_text(json.dumps(config_data, indent=2))
    print(f"[provision] Wrote {sb.config_file}")

    sb.image_dir.mkdir(parents=True, exist_ok=True)
    for filename, content in profile["image_files"].items():
        dest = sb.image_dir / filename
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content)
        print(f"[provision] Wrote {dest}")

    allowlist = profile.get("allowlist", [])
    sb.allowlist_path.write_text(
        "# Squid allowlist — one domain per line, # = comment\n"
        + "".join(d + "\n" for d in allowlist)
    )
    print(f"[provision] Wrote {sb.allowlist_path}")

    sb.save_meta({
        "sandbox_name": sb.name,
        "repo": str(sb.repo),
    })
    print(f"[provision] Instance '{sb.name}' provisioned.")


def _start(sb: Sandbox):
    """Start the sandbox container. Assumes _provision() has already run."""
    ensure_squid(sb)

    proxy_url = f"http://{sb.squid_container_name}:{SQUID_PORT}"
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
    ]

    for rel_path, container_path in load_mounts(sb).items():
        host_path = (sb.state_dir / rel_path).resolve()
        if not host_path.is_relative_to(sb.state_dir.resolve()):
            print(f"WARNING: Skipping unsafe mount path '{rel_path}'")
            continue
        host_path.mkdir(parents=True, exist_ok=True)
        docker_cmd.extend(["-v", f"{host_path}:{container_path}:rw"])

    docker_cmd.extend([
        sb.image_tag,
        "bash",
    ])

    print(f"[container] Starting {sb.container_name} ...")
    run(docker_cmd)  # raises CalledProcessError on failure

    print(f"\n[container] {sb.container_name} is up.")
    print(f"  Workspace : {sb.workspace_dir}")
    print(f"  Volumes   : {sb.state_dir}")
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

    if container_running(sb.squid_container_name):
        print(f"[squid] Stopping {sb.squid_container_name} ...")
        run([DOCKER, "stop", "-t", "0", sb.squid_container_name], check=False, capture=True)
    if container_exists(sb.squid_container_name):
        run([DOCKER, "rm", "-f", sb.squid_container_name], check=False, capture=True)


def _unprovision(sb: Sandbox, force: bool = False):
    """Tear down all per-sandbox runtime resources and seeded files.
    Safe to call at any point — all steps are best-effort.
    Does NOT remove the image (expensive, may be reused on retry).
    If force=True, removes workspace and volumes even if they have content.
    """
    # Stop/remove main container
    if container_running(sb.container_name):
        run([DOCKER, "stop", "-t", "0", sb.container_name], check=False, capture=True)
    if container_exists(sb.container_name):
        run([DOCKER, "rm", "-f", sb.container_name], check=False, capture=True)

    # Stop/remove squid container
    if container_running(sb.squid_container_name):
        run([DOCKER, "stop", "-t", "0", sb.squid_container_name], check=False, capture=True)
    if container_exists(sb.squid_container_name):
        run([DOCKER, "rm", "-f", sb.squid_container_name], check=False, capture=True)

    # Remove git remote on host (may or may not have been added)
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
    _unprovision(sb)

    if image_exists(sb.image_tag):
        print(f"[image] Removing {sb.image_tag} ...")
        run([DOCKER, "rmi", sb.image_tag], check=False)

    print(f"[destroy] {sb.name} destroyed.")

    lingering = [d for d in (sb.workspace_dir, sb.state_dir) if d.exists()]
    if lingering:
        print(f"  Data remaining — remove manually if no longer needed:")
        for d in lingering:
            print(f"    rm -rf {d}")


def infra_down():
    if network_exists(NETWORK_NAME):
        print(f"[infra] Removing network {NETWORK_NAME} ...")
        r = run([DOCKER, "network", "rm", NETWORK_NAME], check=False, capture=True)
        if r.returncode != 0:
            die(f"ERROR: Could not remove network {NETWORK_NAME}.\n"
                "       It is likely still in use by running sandboxes.")
        else:
            print("[infra] Network removed successfully.")
    else:
        print(f"[infra] Network {NETWORK_NAME} not found.")


def status():
    r = run([DOCKER, "ps", "-a", "--format", "{{.Names}} {{.State}}"], capture=True, check=False)
    c_states = {}
    for line in r.stdout.strip().splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2:
            c_states[parts[0]] = parts[1]

    print("\n=== Infrastructure ===")
    net_ok = "✓" if network_exists(NETWORK_NAME) else "✗"
    print(f"  Network: {net_ok} {NETWORK_NAME}")

    print("\n=== Sandboxes ===")
    if not SANDBOX_HOME.exists():
        print("  No sandboxes found.")
    else:
        instances = sorted([d for d in SANDBOX_HOME.iterdir() if d.is_dir() and (d / "config" / "meta.json").exists()])
        if not instances:
            print("  No sandboxes found.")
        else:
            for d in instances:
                name = d.name
                meta_path = d / "config" / "meta.json"
                meta = json.loads(meta_path.read_text())

                repo_str = meta.get("repo", "Unknown")

                container_name = f"sandbox-{name}"
                state = c_states.get(container_name, "no container")

                squid_name = f"sandbox-squid-{name}"
                squid_state = c_states.get(squid_name, "no container")

                df = d / "config" / "image"
                df_info = str(df) if df.exists() else "None found"

                print(f"  Sandbox: {name}")
                print(f"    State     : {state}")
                print(f"    Squid     : {squid_state}")
                print(f"    Repo      : {repo_str}")
                print(f"    Image dir : {df_info}")
                print()

    if SANDBOX_HOME.exists():
        orphans = [d for d in SANDBOX_HOME.iterdir() if d.is_dir() and not (d / "config" / "meta.json").exists()]
        if orphans:
            print("=== Orphans (dirs without config/meta.json) ===")
            for o in orphans:
                print(f"  {o}")
            print("  (Safe to delete manually or use prune when implemented)\n")


def edit_dockerfile(sb: Sandbox):
    dockerfile = resolve_dockerfile(sb)

    if edit_file(dockerfile):
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

    load_config(sb)  # validates JSON after editing

    print("[mounts] Config updated.")
    if container_running(sb.container_name):
        resp = input(f"Restart {sb.container_name} now to apply changes? [y/N] ").lower()
        if resp == "y":
            down(sb)
            up(sb)


def cmd_init(profile_name: str):
    dest = Path.cwd() / ".sandbox-profile"
    if dest.exists():
        die(f".sandbox-profile already exists in {Path.cwd()}. Remove it first to reinitialise.")

    profiles_dir = Path(__file__).parent / "profiles"
    if not profiles_dir.exists():
        die(f"Profiles directory not found at {profiles_dir}. Is the package installed correctly?")

    profile_dir = profiles_dir / profile_name
    if not profile_dir.exists():
        available = [d.name for d in profiles_dir.iterdir() if d.is_dir()]
        die(f"Profile '{profile_name}' not found. Available: {', '.join(sorted(available))}")

    shutil.copytree(profile_dir, dest)
    print(f"[init] Initialised .sandbox-profile from profile '{profile_name}' in {Path.cwd()}")
    print(f"  Edit {dest} to customise, then run: sandbox up")


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

    p_init = sub.add_parser("init", help="Initialise .sandbox-profile in current directory from a profile")
    p_init.add_argument("profile_name", nargs="?", default="default", metavar="PROFILE")

    p_up = sub.add_parser("up", help="Create/start sandbox (idempotent)")
    p_up.add_argument("--name", "-n", default=_REPO_DEFAULT)
    p_up.add_argument("--repo", "-r", default=None)
    p_up.add_argument("--profile", default=None, metavar="DIR")
    p_up.add_argument("--no-cache", dest="no_cache", action="store_true")

    p_down = sub.add_parser("down", help="Stop sandbox container (keep volumes/image)")
    p_down.add_argument("--name", "-n", default=_REPO_DEFAULT)

    p_destroy = sub.add_parser("destroy", help="Remove container, image, and meta (preserves workspace/state)")
    p_destroy.add_argument("--name", "-n", default=_REPO_DEFAULT)

    sub.add_parser("status", help="Show all sandboxes and infrastructure")
    sub.add_parser("infra-down", help="Tear down shared network")

    p_ea = sub.add_parser("edit-allowlist", aliases=["ea"])
    p_ea.add_argument("--name", "-n", default=_REPO_DEFAULT)

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

    global DOCKER
    if args.docker:
        DOCKER = args.docker
    if not DOCKER:
        die("Neither podman nor docker found in PATH. Install one or use --docker.")

    if args.command == "init":
        cmd_init(args.profile_name)
    elif args.command == "up":
        sb = resolve_sandbox(args)
        if args.profile:
            profile_dir = Path(args.profile)
        elif (Path.cwd() / ".sandbox-profile").is_dir():
            profile_dir = Path.cwd() / ".sandbox-profile"
            print(f"[profile] Using .sandbox-profile from {Path.cwd()}")
        else:
            die("No profile found. Run `sandbox init` to create a .sandbox-profile in this directory.")
        sb.profile = load_profile(profile_dir)
        sb.profile_explicit = bool(args.profile)
        up(sb, no_cache=args.no_cache)
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
        sb = resolve_sandbox(args)
        edit_allowlist(sb)
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