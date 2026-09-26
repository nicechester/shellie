#!/bin/sh
# Run Shellie in the foreground from the project root.
set -eu
cd "$(dirname "$0")"
export PYTHONPATH=.
exec python3 -m src.agent
