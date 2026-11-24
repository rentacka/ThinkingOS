#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UPSTREAM="${FSMCOT_UPSTREAM:-http://127.0.0.1:1918}"
PORT="${FSMCOT_PORT:-1921}"

echo "============================================================================"
echo " Aria FSMCoT Compact - Standalone Zero-Dependency OpenAI Proxy"
echo " Listen   : http://127.0.0.1:${PORT}"
echo " Upstream : ${UPSTREAM}"
echo "============================================================================"
exec python3 "${SCRIPT_DIR}/fsmcot_gateway.py" --host 127.0.0.1 --port "${PORT}" --upstream "${UPSTREAM}" "$@"
