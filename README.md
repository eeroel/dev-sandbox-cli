# sandbox

THIS IS A WORK IN PROGRESS. USE AT YOUR OWN RISK.

Dev sandbox container manager (podman/docker, docker-compose-free).

## Install

```bash
uv tool install git+https://github.com/eeroel/sandbox
```

Then use `sandbox` as a command from anywhere.

To update:

```bash
uv tool upgrade sandbox
```

## Usage

Initialise a profile in your repo:

```bash
cd my-project
sandbox init                  # uses default profile
```

This creates `.sandbox-profile/` in your current directory. Commit it to your repo.

Then:

```bash
sandbox up        # picks up .sandbox-profile automatically if present in cwd
sandbox down
sandbox exec
```