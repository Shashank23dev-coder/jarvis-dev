#!/bin/sh
# Only dependency metadata enters the build context: no checkout, state or secrets.
set -eu
cd "$(dirname "$0")/.."
context=$(mktemp -d)
trap 'rm -rf "$context"' EXIT HUP INT TERM
cp sandbox/runtime/Dockerfile "$context/Dockerfile"
cp sandbox/runtime/guard.py "$context/guard.py"
cp sandbox/runtime/supervisor.py "$context/supervisor.py"
cp requirements.txt "$context/requirements.txt"
/usr/bin/docker --host=unix:///var/run/docker.sock build --tag jarvis-sandbox:local "$context"
