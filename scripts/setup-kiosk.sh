#!/usr/bin/env bash
set -euo pipefail
TASK_SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec /usr/bin/python3 "${TASK_SCRIPT_DIR}/deploy.py" "$@"
