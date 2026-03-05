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