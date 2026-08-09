#!/usr/bin/env bash
set -euo pipefail

# Portable launcher for a VPS, VM, container, or another unrestricted runtime.
# Telegram credentials must be provided through the environment; never put them
# in this file or on the command line.
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec python3 -u "${SCRIPT_DIR}/monitor.py" "$@"