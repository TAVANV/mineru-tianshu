#!/usr/bin/env bash
# Optional native Apple Silicon/CPU installation; does not download models.
set -euo pipefail
script_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ "${1:-}" == "--help" ]]; then
  echo 'Usage: scripts/install-native.sh [venv-path] (Python 3.12 required; models are prepared separately)'
  exit 0
fi
native_venv="${1:-$script_root/.venv-native}"
python3.12 -m venv "$native_venv"
"$native_venv/bin/python" -m pip install -r "$script_root/backend/requirements.native.txt"
echo "Installed native runtime in $native_venv. Prepare local models separately; start with --accelerator mps or cpu."
