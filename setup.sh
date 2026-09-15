#!/usr/bin/env bash
# Additive entry point; existing scripts and compose defaults remain supported.
set -euo pipefail
script_root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$script_root/scripts/deployment.py" "$@"
