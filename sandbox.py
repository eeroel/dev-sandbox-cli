#!/usr/bin/env python3
"""
sandbox.py — Dev sandbox container manager (podman/docker, docker-compose-free)

Usage:
    sandbox.py up [--name NAME] [--repo PATH] [--rebuild] [--no-cache]
    sandbox.py down [--name NAME]
    sandbox.py destroy [--name NAME] [--volumes]
    sandbox.py status
    sandbox.py exec [--name NAME] [CMD...]
    sandbox.py edit-allowlist
    sandbox.py edit-dockerfile [--name NAME]
    sandbox.py edit-mounts [--name NAME]
    sandbox.py infra-down

All infrastructure (network, squid proxy) is created if not present — idempotent.
Default sandbox name is derived from the repo directory name.

TODO:
  - [depends on SandboxConfig] add "--profile" argument that takes a directory which supplies a SandboxConfig as files. Hardcode the default profile as JSON in this file (ie move all the relevant strings as fields to that one).
  - Add `--pull` flag to `up` to refresh the base image before rebuilding (for OS/base updates)
  - Orphaned volume dirs in workspaces flagged in `status` but not cleaned automatically;
    consider an `sandbox.py prune` command
  - On `up`, add cli prompt to set git username and email. Default to values from repo, but allow changing them.
  - Warn or error on `up` if the same --name is reused across different repos
    (currently only catches this if meta already exists)
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
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

# Files that trigger an image rebuild when changed
DEFAULT_DOCKERFILE = """\
FROM python:3.13-slim

WORKDIR /workspace

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git curl ca-certificates ripgrep \\
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv ruff ty

CMD ["bash"]
"""

# Default state mounts if none specified
# Maps relative path in state_dir -> absolute path in container
DEFAULT_MOUNTS = {
    # example:
    # ".config": "/root/.config"
}

REBUILD_TRIGGERS = [
    ".sandbox-dockerfile",
    "uv.lock",
    "pyproject.toml",
    "requirements.txt",
    "requirements-dev.txt",
    "package.json",
    "package-lock.json",
    "yarn.lock",
]

DOCKER = shutil.which("podman") or shutil.which("docker")
SANDBOX_DOCKERFILE_NAME = ".sandbox-dockerfile"  # repo-level override
ENTRYPOINT_TEMPLATE = """\
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
    git config user.name "Agent ({sandbox_name})"
else
    echo "[sandbox] Workspace already cloned."
    cd "$WORKSPACE"
fi

echo "[sandbox] Ready. Repo: $(git log --oneline -1 2>/dev/null || echo 'empty')"
exec "$@"
"""

# ---------------------------------------------------------------------------
# Sandbox dataclass
# ---------------------------------------------------------------------------

@dataclass
class Sandbox:
    name: str
    repo: Path

    # --- Path properties ---------------------------------------------------

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
    def mounts_file(self) -> Path:
        return self.meta_dir / "mounts.json"

    @property
    def config(self) -> "SandboxConfig":
        return SandboxConfig(self)

    # --- Meta persistence --------------------------------------------------

    def load_meta(self) -> dict:
        p = self.meta_dir / "meta.json"
        if p.exists():
            return json.loads(p.read_text())
        return {}

    def save_meta(self, meta: dict):
        """Write meta atomically via temp file + rename."""
        p = self.meta_dir / "meta.json"
        dir_ = p.parent
        dir_.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=dir_, prefix=".meta-")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(meta, f, indent=2)
            os.replace(tmp, p)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def load_mounts(self) -> dict:
        """Backward-compatible wrapper around SandboxConfig."""
        return self.config.load_mounts()

    def ensure_mounts_file(self) -> Path:
        """Backward-compatible wrapper around SandboxConfig."""
        return self.config.ensure_mounts_file()

    # --- Alternative constructors ------------------------------------------

    @classmethod
    def load(cls, name: str) -> "Sandbox":
        """Reconstruct a Sandbox from its persisted meta.json."""
        meta_path = INSTANCES_DIR / name / "meta.json"
        if not meta_path.exists():
            die(f"No meta found for sandbox '{name}'. Has it been created with `up`?")
        meta = json.loads(meta_path.read_text())
        repo = meta.get("repo")
        if not repo:
            die(f"meta.json for sandbox '{name}' is missing 'repo' field.")
        return cls(name=name, repo=Path(repo))


# ---------------------------------------------------------------------------
# Sandbox config abstraction
# ---------------------------------------------------------------------------

@dataclass
class SandboxConfig:
    sandbox: Sandbox

    @property
    def mounts_file(self) -> Path:
        return self.sandbox.meta_dir / "mounts.json"

    @property
    def default_dockerfile_path(self) -> Path:
        return self.sandbox.meta_dir / "Dockerfile"

    @property
    def repo_dockerfile_path(self) -> Path:
        return self.sandbox.repo / SANDBOX_DOCKERFILE_NAME

    @property
    def entrypoint_path(self) -> Path:
        return self.sandbox.meta_dir / "entrypoint.sh"

    def ensure_mounts_file(self) -> Path:
        """Write default mounts.json if missing."""
        self.mounts_file.parent.mkdir(parents=True, exist_ok=True)
        if not self.mounts_file.exists():
            self.mounts_file.write_text(json.dumps(DEFAULT_MOUNTS, indent=2))
            print(f"[mounts] No manifest found — wrote default to {self.mounts_file}")
        return self.mounts_file

    def load_mounts(self) -> dict:
        """Load mount mappings. Relative path -> Container path."""
        if self.mounts_file.exists():
            try:
                return json.loads(self.mounts_file.read_text())
            except json.JSONDecodeError:
                print(f"WARNING: Malformed {self.mounts_file}. Using defaults.")
        return DEFAULT_MOUNTS

    def ensure_default_dockerfile(self) -> Path:
        """Write default Dockerfile if missing."""
        self.default_dockerfile_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.default_dockerfile_path.exists():
            self.default_dockerfile_path.write_text(DEFAULT_DOCKERFILE)
            print(f"[image] No repo Dockerfile found — wrote default to {self.default_dockerfile_path}")
        else:
            print(f"[image] No repo Dockerfile found — using existing default at {self.default_dockerfile_path}")
        return self.default_dockerfile_path

    def resolve_dockerfile(self, explicit_dockerfile: str = None) -> Path:
        """Resolve Dockerfile path by precedence and ensure default fallback exists."""
        if explicit_dockerfile:
            dockerfile = Path(explicit_dockerfile).resolve()
            if not dockerfile.exists():
                raise FileNotFoundError(f"--dockerfile not found: {dockerfile}")
            return dockerfile
        if self.repo_dockerfile_path.exists():
            return self.repo_dockerfile_path
        return self.ensure_default_dockerfile()

    def ensure_entrypoint(self) -> Path:
        """Write entrypoint script every run so template changes are picked up."""
        script = ENTRYPOINT_TEMPLATE.format(sandbox_name=self.sandbox.name)
        self.entrypoint_path.parent.mkdir(parents=True, exist_ok=True)
        previous = self.entrypoint_path.read_text() if self.entrypoint_path.exists() else None
        if previous != script:
            self.entrypoint_path.write_text(script)
        self.entrypoint_path.chmod(0o755)
        return self.entrypoint_path


# ---------------------------------------------------------------------------
# CLI name/repo resolution
# ---------------------------------------------------------------------------

def resolve_sandbox(args) -> "Sandbox":
    """
    Resolve the sandbox name and repo from CLI args, returning a Sandbox.

    Resolution rules:
    - If args.repo is set, use it; otherwise look for git root of cwd.
    - If args.name is the sentinel _REPO_DEFAULT, derive name from the repo dir.
    - If only --name is given (no --repo), load repo from meta.json.
    """
    name_arg = getattr(args, "name", _REPO_DEFAULT)
    repo_arg = getattr(args, "repo", None)

    if repo_arg:
        repo = Path(repo_arg).resolve()
        name = repo.name if name_arg == _REPO_DEFAULT else name_arg
        return Sandbox(name=name, repo=repo)

    if name_arg != _REPO_DEFAULT:
        # Name was explicitly provided — try to load repo from meta
        meta_path = INSTANCES_DIR / name_arg / "meta.json"
        if meta_path.exists():
            return Sandbox.load(name_arg)
        # Meta not yet created (e.g. first `up --name foo`) — fall through to git root
        try:
            repo = repo_root()
        except SystemExit:
            die(f"Sandbox '{name_arg}' not yet created and not inside a git repo. "
                "Use --repo to specify the repo path.")
        return Sandbox(name=name_arg, repo=repo)

    # Neither explicit name nor repo — derive both from cwd git root
    try:
        repo = repo_root()
    except SystemExit:
        die("Not inside a git repository. Use --repo or --name to specify the sandbox.")
    return Sandbox(name=repo.name, repo=repo)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run(cmd: list, capture=False, check=True, **kwargs):
    """Run a command, optionally capturing output."""
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


def container_exists(name: str) -> bool:
    r = run([DOCKER, "inspect", name], capture=True, check=False)
    return r.returncode == 0


def container_running(name: str) -> bool:
    r = run(
        [DOCKER, "inspect", "--format", "{{.State.Running}}", name],
        capture=True,
        check=False,
    )
    return r.returncode == 0 and r.stdout.strip() == "true"


def network_exists(name: str) -> bool:
    r = run([DOCKER, "network", "inspect", name], capture=True, check=False)
    return r.returncode == 0


def image_exists(name: str) -> bool:
    r = run([DOCKER, "image", "inspect", name], capture=True, check=False)
    return r.returncode == 0


def repo_root() -> Path:
    """Find git repo root from cwd."""
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
    """Write a new squid.conf and restart squid to apply it."""
    write_squid_conf(allowlist)
    if not container_running(SQUID_CONTAINER_NAME):
        print("[squid] Not running — config saved, will apply on next start.")
        return
    print("[squid] Restarting squid ...")
    run([DOCKER, "restart", SQUID_CONTAINER_NAME])


def edit_allowlist():
    """Open $EDITOR on the global squid allowlist, persist it, reconfigure squid."""
    SANDBOX_HOME.mkdir(parents=True, exist_ok=True)
    GLOBAL_ALLOWLIST_FILE.write_text(
        "# Global squid allowlist (one domain per line, # = comment)\n"
        + "".join(d + "\n" for d in load_global_allowlist())
    )

    mtime_before = GLOBAL_ALLOWLIST_FILE.stat().st_mtime
    subprocess.run([os.environ.get("EDITOR", "vi"), str(GLOBAL_ALLOWLIST_FILE)])

    if GLOBAL_ALLOWLIST_FILE.stat().st_mtime <= mtime_before:
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
    Returns (needs_rebuild, current_hash). Does not modify any state."""
    trigger_files = [sb.repo / f for f in REBUILD_TRIGGERS]
    current_hash = hash_files(trigger_files)

    if force:
        return True, current_hash

    meta = sb.load_meta()
    last_hash = meta.get("image_hash")

    if image_exists(sb.image_tag) and last_hash == current_hash:
        print(f"[image] {sb.image_tag} is up to date (hash {current_hash}).")
        return False, current_hash

    return True, current_hash


def build_image(sb: Sandbox, current_hash: str, no_cache=False, explicit_dockerfile: str = None) -> None:
    """Unconditionally build the image and write the new hash to meta on success.
    Raises CalledProcessError on build failure (meta is not updated)."""
    dockerfile = sb.config.resolve_dockerfile(explicit_dockerfile=explicit_dockerfile)

    print(f"[image] Building {sb.image_tag} (trigger hash: {current_hash}){' [no-cache]' if no_cache else ''} ...")
    print(f"[image] Dockerfile: {dockerfile}")
    cmd = [DOCKER, "build", "-t", sb.image_tag, "-f", str(dockerfile)]
    if no_cache:
        cmd.append("--no-cache")
    cmd.append(str(sb.repo))
    run(cmd)  # raises CalledProcessError on failure

    # Build succeeded — persist the new hash
    meta = sb.load_meta()
    meta["image_hash"] = current_hash
    sb.save_meta(meta)


# ---------------------------------------------------------------------------
# Git: two-way remote setup
# ---------------------------------------------------------------------------

def setup_git_remotes(sb: Sandbox):
    """
    Inside the container the agent's origin = /repo/.git (the host .git, read-only mount).
    On the host, we add a remote pointing at the agent's clone so you can cherry-pick.
    """
    remote_name = f"agent-{sb.name}"

    # Check if remote already exists on host
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
# Container entrypoint script
# ---------------------------------------------------------------------------

def write_entrypoint(sb: Sandbox) -> Path:
    """Backward-compatible wrapper around SandboxConfig."""
    return sb.config.ensure_entrypoint()


# ---------------------------------------------------------------------------
# Sandbox container
# ---------------------------------------------------------------------------

def wait_for_clone(sb: Sandbox, timeout=60):
    """Wait for the container to finish cloning the repository into the workspace."""
    print(f"[git] Waiting for initial clone in {sb.name}...")
    start_time = time.time()
    while time.time() - start_time < timeout:
        if (sb.workspace_dir / ".git").exists():
            return True
        if not container_running(sb.container_name):
            print("[git] Container stopped unexpectedly during clone.")
            return False
        time.sleep(0.5)
    print(f"[git] Timeout: Clone did not complete within {timeout}s.")
    return False


def up(sb: Sandbox, force_rebuild=False, no_cache=False, explicit_dockerfile: str = None):
    print(f"\n=== sandbox up: {sb.name} ===\n")

    # For rollback, check if meta dir was there before we create it
    meta_dir_existed = sb.meta_dir.exists()
    sb.meta_dir.mkdir(parents=True, exist_ok=True)

    # Infrastructure (shared, long-lived — always safe to create)
    ensure_network()
    ensure_squid(load_global_allowlist())
    # Build image — may raise; image_needs_rebuild is a pure read, build_image writes hash on success
    needs_rebuild, image_hash = image_needs_rebuild(sb, force=force_rebuild)
    if needs_rebuild:
        build_image(sb, image_hash, no_cache=no_cache, explicit_dockerfile=explicit_dockerfile)

    if container_running(sb.container_name):
        print(f"[container] {sb.container_name} is already running.")
        setup_git_remotes(sb)
        return

    if container_exists(sb.container_name):
        print(f"[container] Removing stopped container {sb.container_name} ...")
        run([DOCKER, "rm", sb.container_name], check=False, capture=True)

    # Create volume dirs and entrypoint only just before we need them
    sb.workspace_dir.mkdir(parents=True, exist_ok=True)
    sb.state_dir.mkdir(parents=True, exist_ok=True)
    config = sb.config
    config.ensure_mounts_file()
    entrypoint = config.ensure_entrypoint()

    proxy_url = f"http://{SQUID_CONTAINER_NAME}:{SQUID_PORT}"
    git_dir = sb.repo / ".git"

    # Assemble Docker run command
    docker_cmd = [
        DOCKER, "run", "-d", "-i",
        "--name", sb.container_name,
        "--network", NETWORK_NAME,
        # Proxy env vars
        "-e", f"http_proxy={proxy_url}",
        "-e", f"https_proxy={proxy_url}",
        "-e", f"HTTP_PROXY={proxy_url}",
        "-e", f"HTTPS_PROXY={proxy_url}",
        "-e", "no_proxy=localhost,127.0.0.1",
        # Default internal volumes
        "-v", f"{git_dir}:/repo-git:ro",
        "-v", f"{sb.workspace_dir}:/llm-workspace:rw",
        "-v", f"{entrypoint}:/entrypoint.sh:ro",
    ]

    # Flexible state mappings
    mounts = config.load_mounts()
    for rel_path, container_path in mounts.items():
        host_path = (sb.state_dir / rel_path).resolve()
        # Safety check to prevent escaping state_dir
        if not str(host_path).startswith(str(sb.state_dir.resolve())):
            print(f"WARNING: Skipping unsafe mount path '{rel_path}'")
            continue
        host_path.mkdir(parents=True, exist_ok=True)
        docker_cmd.extend(["-v", f"{host_path}:{container_path}:rw"])

    docker_cmd.extend([
        "--entrypoint", "/entrypoint.sh",
        sb.image_tag,
        "bash",
    ])

    try:
        print(f"[container] Starting {sb.container_name} ...")
        run(docker_cmd)  # raises CalledProcessError on failure
    except Exception:
        # If we created the meta dir this run, remove it to avoid ghost entries in status
        if not meta_dir_existed:
            if sb.meta_dir.exists():
                shutil.rmtree(sb.meta_dir)
        raise

    # Container is confirmed up — persist remaining state. image_hash was already
    # written by build_image on success, so load first to avoid overwriting it.
    meta = sb.load_meta()
    meta.update({
        "sandbox_name": sb.name,
        "repo": str(sb.repo),
        "workspace_dir": str(sb.workspace_dir),
        "state_dir": str(sb.state_dir),
        "container": sb.container_name,
    })
    sb.save_meta(meta)

    print(f"\n[container] {sb.container_name} is up.")
    print(f"  Workspace : {sb.workspace_dir}")
    print(f"  State     : {sb.state_dir}")
    print(f"  Proxy     : {proxy_url}")

    # Robust wait for git clone to finish
    if wait_for_clone(sb):
        setup_git_remotes(sb)

    print(f"\n  Attach with: {Path(sys.argv[0]).name} exec --name {sb.name}")
    print(f"  Or directly: {DOCKER} exec -it -w /llm-workspace {sb.container_name} bash\n")


def down(sb: Sandbox):
    if container_running(sb.container_name):
        print(f"[container] Stopping {sb.container_name} (volumes preserved) ...")
        # WARNING: -t 0 sends SIGKILL immediately, skipping graceful shutdown.
        run([DOCKER, "stop", "-t", "0", sb.container_name], check=False)
        run([DOCKER, "rm", sb.container_name], check=False, capture=True)
    elif container_exists(sb.container_name):
        run([DOCKER, "rm", sb.container_name], check=False, capture=True)
    else:
        print(f"[container] {sb.container_name} not found.")


def destroy(sb: Sandbox, remove_volumes=False):
    meta = sb.load_meta()
    down(sb)
    if image_exists(sb.image_tag):
        print(f"[image] Removing {sb.image_tag} ...")
        run([DOCKER, "rmi", sb.image_tag], check=False)

    if remove_volumes:
        for key in ("workspace_dir", "state_dir"):
            p = Path(meta[key]) if key in meta else None
            if p and p.exists():
                print(f"[volumes] Removing {p} ...")
                shutil.rmtree(p)

    # Remove the agent remote from the host repo if it exists
    remote_name = f"agent-{sb.name}"
    r = run(["git", "-C", str(sb.repo), "remote"], capture=True, check=False)
    if remote_name in r.stdout.splitlines():
        print(f"[git] Removing host remote '{remote_name}' ...")
        run(["git", "-C", str(sb.repo), "remote", "remove", remote_name], check=False)

    if sb.meta_dir.exists():
        shutil.rmtree(sb.meta_dir)

    print(f"[destroy] {sb.name} destroyed.")


def infra_down():
    """Stop and remove the shared squid container and sandbox-net network."""
    errors = False

    if container_exists(SQUID_CONTAINER_NAME):
        print(f"[infra] Removing {SQUID_CONTAINER_NAME} ...")
        # stop first to be nice, then rm -f to be sure
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
    """Show status of infrastructure and all known sandboxes."""
    # 1. Fetch container states in bulk to avoid jitter
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
                repo_p = Path(repo_str) if repo_str != "Unknown" else None

                # Determine status
                container_name = f"sandbox-{name}"
                state = c_states.get(container_name, "no container")

                # Determine Dockerfile origin
                repo_df = repo_p / SANDBOX_DOCKERFILE_NAME if repo_p else None
                fallback_df = d / "Dockerfile"
                if repo_df and repo_df.exists():
                    df_info = f"{repo_df} (repo override)"
                elif fallback_df.exists():
                    df_info = f"{fallback_df} (default)"
                else:
                    df_info = "None found"

                print(f"  Sandbox: {name}")
                print(f"    State     : {state}")
                print(f"    Repo      : {repo_str}")
                print(f"    Dockerfile: {df_info}")
                print()

    # Flag orphaned workspace dirs (no matching meta entry)
    if WORKSPACES_DIR.exists():
        known_names = {d.name for d in INSTANCES_DIR.iterdir()} if INSTANCES_DIR.exists() else set()
        orphans = [d for d in WORKSPACES_DIR.iterdir() if d.is_dir() and d.name not in known_names]
        if orphans:
            print("=== Orphans (Workspaces without metadata) ===")
            for o in orphans:
                print(f"  {o}")
            print("  (Safe to delete manually or use prune when implemented)\n")


def edit_dockerfile(sb: Sandbox, explicit_dockerfile: str = None):
    dockerfile = sb.config.resolve_dockerfile(explicit_dockerfile=explicit_dockerfile)

    editor = os.environ.get("EDITOR", "vi")
    mtime_before = dockerfile.stat().st_mtime
    subprocess.run([editor, str(dockerfile)])
    mtime_after = dockerfile.stat().st_mtime

    if mtime_after > mtime_before:
        print("[ed] Dockerfile changed, rebuilding ...")
        was_running = container_running(sb.container_name)
        try:
            _, new_hash = image_needs_rebuild(sb, force=True)
            build_image(sb, new_hash)
        except Exception as e:
            print(f"[ed] Build failed: {e}")
            print(f"[ed] Container left {'running' if was_running else 'stopped'} — no restart.")
            return
        # Build succeeded — restart if it was running
        if was_running:
            print(f"[ed] Restarting {sb.container_name} ...")
            down(sb)
            up(sb)
    else:
        print("[ed] No changes.")


def edit_mounts(sb: Sandbox):
    """Open $EDITOR on the mounts manifest, validate JSON, and restart sandbox if running."""
    config = sb.config
    config.ensure_mounts_file()

    editor = os.environ.get("EDITOR", "vi")
    mtime_before = config.mounts_file.stat().st_mtime
    subprocess.run([editor, str(config.mounts_file)])

    if config.mounts_file.stat().st_mtime <= mtime_before:
        print("[mounts] No changes.")
        return

    # Validate JSON
    try:
        json.loads(config.mounts_file.read_text())
    except json.JSONDecodeError as e:
        die(f"Invalid JSON in mounts file: {e}")

    print("[mounts] Manifest updated.")
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

    # up
    p_up = sub.add_parser("up", help="Create/start sandbox (idempotent)")
    p_up.add_argument("--name", "-n", default=_REPO_DEFAULT,
                      help="Sandbox name (default: repo directory name)")
    p_up.add_argument("--repo", "-r", default=None, help="Repo path (default: git root of cwd)")
    p_up.add_argument("--rebuild", action="store_true", help="Force image rebuild")
    p_up.add_argument("--no-cache", dest="no_cache", action="store_true",
                      help="Pass --no-cache to docker build (implies --rebuild)")
    p_up.add_argument("--dockerfile", default=None, metavar="PATH",
                      help="Explicit Dockerfile (overrides .sandbox-dockerfile and default)")

    # down
    p_down = sub.add_parser("down", help="Stop sandbox container (keep volumes/image)")
    p_down.add_argument("--name", "-n", default=_REPO_DEFAULT)

    # destroy
    p_destroy = sub.add_parser("destroy", help="Remove container and image")
    p_destroy.add_argument("--name", "-n", default=_REPO_DEFAULT)
    p_destroy.add_argument("--volumes", action="store_true",
                           help="Also delete workspace and state directories")

    # status
    sub.add_parser("status", help="Show all sandboxes and infrastructure")

    # infra-down
    sub.add_parser("infra-down", help="Tear down shared squid container and network")

    # edit-allowlist
    sub.add_parser("edit-allowlist", aliases=["ea"],
                   help="Edit global squid allowlist and reconfigure squid")

    # edit-dockerfile
    p_ed = sub.add_parser("edit-dockerfile", aliases=["ed"], help="Edit sandbox Dockerfile and rebuild")
    p_ed.add_argument("--name", "-n", default=_REPO_DEFAULT)
    p_ed.add_argument("--dockerfile", default=None, metavar="PATH",
                      help="Explicit Dockerfile to edit")

    # edit-mounts
    p_em = sub.add_parser("edit-mounts", aliases=["em"], help="Edit state directory mounts")
    p_em.add_argument("--name", "-n", default=_REPO_DEFAULT)

    # exec
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
        up(sb, force_rebuild=args.rebuild or args.no_cache, no_cache=args.no_cache,
           explicit_dockerfile=args.dockerfile)

    elif args.command == "down":
        sb = resolve_sandbox(args)
        down(sb)

    elif args.command == "destroy":
        sb = resolve_sandbox(args)
        destroy(sb, remove_volumes=args.volumes)

    elif args.command == "status":
        status()

    elif args.command == "infra-down":
        infra_down()

    elif args.command in ("edit-allowlist", "ea"):
        edit_allowlist()

    elif args.command in ("edit-dockerfile", "ed"):
        sb = resolve_sandbox(args)
        edit_dockerfile(sb, explicit_dockerfile=getattr(args, "dockerfile", None))

    elif args.command in ("edit-mounts", "em"):
        sb = resolve_sandbox(args)
        edit_mounts(sb)

    elif args.command == "exec":
        sb = resolve_sandbox(args)
        exec_cmd(sb, args.cmd)


if __name__ == "__main__":
    main()
