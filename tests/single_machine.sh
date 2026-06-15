#!/usr/bin/env bash
# single_machine.sh — End-to-end test orchestrator for LinuxCNC Fleet setup.
#
# Starts gateway + sidecar, runs verify.py, then cleans up.
#
# Usage:
#   ./tests/single_machine.sh                                          # auto-detect venv
#   ./tests/single_machine.sh --venv /home/james/dev/venv              # explicit venv
#   ./tests/single_machine.sh --ini /path/to/file.ini                  # custom INI
#   ./tests/single_machine.sh --machine-id my-machine                  # custom machine ID
#
# Exit codes: 0 = all checks passed, 1 = failure, 2 = setup error

set -euo pipefail

# ── Defaults ───────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
VENV_BIN=""
INI_PATH=""
MACHINE_ID=""
GATEWAY_PORT=50052
SIDECAR_PORT=50051

# ── Parse arguments ────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --venv)
            VENV_BIN="$2"; shift 2 ;;
        --ini)
            INI_PATH="$2"; shift 2 ;;
        --machine-id)
            MACHINE_ID="$2"; shift 2 ;;
        --help|-h)
            echo "Usage: $0 [--venv <path>] [--ini <path>] [--machine-id <id>]"
            exit 0 ;;
        *)
            echo "Unknown option: $1" >&2
            exit 1 ;;
    esac
done

# ── Resolve venv ───────────────────────────────────────────────────────────
if [[ -z "$VENV_BIN" ]]; then
    GW_PATH="$(which fleet-gateway 2>/dev/null || true)"
    if [[ -n "$GW_PATH" ]]; then
        VENV_BIN="$(dirname "$GW_PATH")"
    else
        echo "ERROR: No venv specified and 'fleet-gateway' not found in PATH." >&2
        echo "       Use --venv /path/to/venv/bin or install linuxcnc-fleet first." >&2
        exit 1
    fi
fi

# Handle both /path/to/venv and /path/to/venv/bin
if [[ ! -x "$VENV_BIN/python3" && -d "$VENV_BIN/bin" ]]; then
    VENV_BIN="$VENV_BIN/bin"
fi

if [[ ! -d "$VENV_BIN" ]]; then
    echo "ERROR: Venv directory does not exist: $VENV_BIN" >&2
    exit 1
fi

PYTHON="$VENV_BIN/python3"
GATEWAY_CMD="$VENV_BIN/fleet-gateway"
SIDECAR_CMD="$VENV_BIN/headless-server"

for CMD in "$PYTHON" "$GATEWAY_CMD" "$SIDECAR_CMD"; do
    if [[ ! -x "$CMD" ]]; then
        echo "ERROR: Not found or not executable: $CMD" >&2
        exit 1
    fi
done

echo "=============================================="
echo " LinuxCNC Fleet — Single-Machine Test"
echo "=============================================="
echo "  Venv:       $VENV_BIN"
echo "  Python:     $($PYTHON --version 2>&1 || true)"
echo "  Gateway:    $GATEWAY_CMD"
echo "  Sidecar:    $SIDECAR_CMD"
echo "  Ports:      $SIDECAR_PORT / $GATEWAY_PORT"

if [[ -n "$INI_PATH" ]]; then
    echo "  INI file:   $INI_PATH"
fi
echo ""

# ── Auto-detect machine ID ─────────────────────────────────────────────────
if [[ -z "$MACHINE_ID" ]]; then
    MACHINE_ID="$(hostname)-linuxcnc"
fi

# ── Kill stale processes ───────────────────────────────────────────────────
for PORT in "$GATEWAY_PORT" "$SIDECAR_PORT"; do
    PID=$(lsof -ti :"$PORT" 2>/dev/null || true)
    if [[ -n "$PID" ]]; then
        echo "Killing stale process on port $PORT (PID: $PID)..."
        kill "$PID" 2>/dev/null || true
        sleep 1
    fi
done

# ── Check prerequisites ────────────────────────────────────────────────────
if ! "$PYTHON" -c "import linuxcnc" 2>/dev/null; then
    echo "ERROR: linuxcnc Python module not available in venv." >&2
    exit 1
fi

# Check that a LinuxCNC instance is actually running
if ! pgrep -f "milltask|linuxcncsvr" >/dev/null 2>&1; then
    echo "ERROR: No running LinuxCNC instance found." >&2
    echo "       Start LinuxCNC first (e.g. 'linuxcnc /path/to/config.ini')." >&2
    exit 1
fi

echo "Prerequisites OK"
echo ""

# ── Start Gateway ──────────────────────────────────────────────────────────
echo "Starting gateway on :$GATEWAY_PORT ..."
"$GATEWAY_CMD" \
    --port "$GATEWAY_PORT" \
    --jwt-secret "my-shared-secret" \
    --issuer "linuxcnc-fleet" \
    --audience "fleet-api" \
    -v &
GATEWAY_PID=$!
echo "  Gateway PID: $GATEWAY_PID"

# ── Start Sidecar ──────────────────────────────────────────────────────────
echo "Starting sidecar on :$SIDECAR_PORT ..."
SIDECAR_ARGS=(
    --port "$SIDECAR_PORT"
    --machine-id "$MACHINE_ID"
    --gateway
    --jwt-secret "my-shared-secret"
    --issuer "linuxcnc-fleet"
    --audience "fleet-api"
    --gateway-address "localhost:$GATEWAY_PORT"
    -v
)

if [[ -n "$INI_PATH" ]]; then
    SIDECAR_ARGS+=(--ini "$INI_PATH")
else
    FOUND_INI=$("$PYTHON" -c "import linuxcnc; print(linuxcnc.find_file('ini') or '')" 2>/dev/null || true)
    if [[ -n "$FOUND_INI" ]]; then
        SIDECAR_ARGS+=(--ini "$FOUND_INI")
    else
        echo "ERROR: No INI file found and none specified via --ini." >&2
        kill "$GATEWAY_PID" 2>/dev/null || true
        exit 1
    fi
fi

"$SIDECAR_CMD" "${SIDECAR_ARGS[@]}" &
SIDECAR_PID=$!
echo "  Sidecar PID: $SIDECAR_PID"
echo ""

# ── Cleanup function ───────────────────────────────────────────────────────
cleanup() {
    echo ""
    echo "Cleaning up ..."
    kill "$GATEWAY_PID" "$SIDECAR_PID" 2>/dev/null || true
    sleep 1
    # Force kill if still alive
    kill -9 "$GATEWAY_PID" "$SIDECAR_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# ── Wait for servers to be ready ───────────────────────────────────────────
echo "Waiting for servers to start ..."
READY=false
for i in $(seq 1 30); do
    sleep 1
    if kill -0 "$GATEWAY_PID" 2>/dev/null && kill -0 "$SIDECAR_PID" 2>/dev/null; then
        READY=true
        break
    fi
    echo "  Waiting... ($i/30)"
done

if ! $READY; then
    echo "ERROR: One or both servers failed to start." >&2
    exit 1
fi

echo "Both servers are running."
echo ""

# ── Wait for auto-registration to complete ──────────────────────────────────
if [[ -n "$VENV_BIN" ]]; then
    echo "Waiting for sidecar auto-registration ..."
    sleep 3
fi

# ── Run verification ───────────────────────────────────────────────────────
cd "$PROJECT_DIR"
echo "Running verify.py ..."
"$PYTHON" scripts/verify.py --gateway "localhost:$GATEWAY_PORT"
VERIFY_EXIT=$?

echo ""
if [[ $VERIFY_EXIT -eq 0 ]]; then
    echo "=============================================="
    echo "  ALL CHECKS PASSED"
    echo "=============================================="
else
    echo "=============================================="
    echo "  SOME CHECKS FAILED (exit code: $VERIFY_EXIT)"
    echo "=============================================="
fi

exit $VERIFY_EXIT
