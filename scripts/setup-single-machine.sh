#!/usr/bin/env bash
# setup-single-machine.sh — Start gateway + sidecar on a single LinuxCNC 2.9 machine
# Usage: ./scripts/setup-single-machine.sh [--ini /path/to/file.ini] [--machine-id my-machine]
set -euo pipefail

# ── Configuration ──────────────────────────────────────────────────────────────
GATEWAY_PORT=50052
SIDECAR_PORT=50051
JWT_SECRET="${FLEET_JWT_SECRET:-my-shared-secret}"
JWT_ISSUER="${FLEET_JWT_ISSUER:-linuxcnc-fleet}"
JWT_AUDIENCE="${FLEET_JWT_AUDIENCE:-fleet-api}"

# ── Parse arguments ────────────────────────────────────────────────────────────
INI_PATH=""
MACHINE_ID=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --ini)
            INI_PATH="$2"; shift 2 ;;
        --machine-id)
            MACHINE_ID="$2"; shift 2 ;;
        *)
            echo "Unknown option: $1" >&2
            exit 1
            ;;
    esac
done

# Auto-detect machine ID if not provided
if [[ -z "$MACHINE_ID" ]]; then
    MACHINE_ID="$(hostname)-linuxcnc"
fi

echo "=============================================="
echo " LinuxCNC Fleet — Single-Machine Setup"
echo "=============================================="
echo "  Gateway port:   $GATEWAY_PORT"
echo "  Sidecar port:   $SIDECAR_PORT"
echo "  Machine ID:     $MACHINE_ID"
echo "  JWT issuer:     $JWT_ISSUER"
echo "  JWT audience:   $JWT_AUDIENCE"
echo ""

# ── Prerequisite checks ────────────────────────────────────────────────────────

# Check linuxcnc module
if ! python3 -c "import linuxcnc; print(f'  linuxcnc module: OK (version {linuxcnc.FILE_VERSION()})')" 2>/dev/null; then
    echo "ERROR: linuxcnc Python module not found." >&2
    echo "       Install LinuxCNC 2.9 or activate its environment first." >&2
    exit 1
fi

# Check pip package is installed
if ! pip show linuxcnc-fleet &>/dev/null; then
    echo "ERROR: linuxcnc-fleet pip package not installed." >&2
    echo "       Run: pip install dist/linuxcnc_fleet-*.whl" >&2
    exit 1
fi

echo "  linuxcnc module: OK"
echo "  linuxcnc-fleet package: OK ($(pip show linuxcnc-fleet 2>/dev/null | grep Version))"
echo ""

# ── Kill any existing processes on our ports ───────────────────────────────────
for PORT in "$GATEWAY_PORT" "$SIDECAR_PORT"; do
    PID=$(lsof -ti :"$PORT" 2>/dev/null || true)
    if [[ -n "$PID" ]]; then
        echo "Stopping existing process on port $PORT (PID: $PID)..."
        kill "$PID" 2>/dev/null || true
        sleep 1
    fi
done

# ── Start Gateway ──────────────────────────────────────────────────────────────
echo "Starting gateway on :$GATEWAY_PORT ..."
fleet-gateway \
    --port "$GATEWAY_PORT" \
    --jwt-secret "$JWT_SECRET" \
    --issuer "$JWT_ISSUER" \
    --audience "$JWT_AUDIENCE" \
    -v &
GATEWAY_PID=$!
echo "  Gateway PID: $GATEWAY_PID"

# ── Start Sidecar ──────────────────────────────────────────────────────────────
echo "Starting sidecar on :$SIDECAR_PORT ..."
SIDEcar_ARGS=(
    --port "$SIDECAR_PORT"
    --machine-id "$MACHINE_ID"
    --gateway
    --jwt-secret "$JWT_SECRET"
    --issuer "$JWT_ISSUER"
    --audience "$JWT_AUDIENCE"
    -v
)

if [[ -n "$INI_PATH" ]]; then
    SIDEcar_ARGS+=(--ini "$INI_PATH")
    echo "  INI file: $INI_PATH"
else
    # Try to find INI via linuxcnc module
    FOUND_INI=$(python3 -c "import linuxcnc; print(linuxcnc.find_file('ini') or '')" 2>/dev/null || true)
    if [[ -n "$FOUND_INI" ]]; then
        SIDEcar_ARGS+=(--ini "$FOUND_INI")
        echo "  INI file: $FOUND_INI (auto-detected)"
    else
        echo "  WARNING: No INI file found. Sidecar will use default machine ID." >&2
    fi
fi

headless-server "${SIDEcar_ARGS[@]}" &
SIDECAR_PID=$!
echo "  Sidecar PID: $SIDECAR_PID"
echo ""

# ── Wait for servers to be ready ───────────────────────────────────────────────
echo "Waiting for servers to start ..."
READY=false

for i in $(seq 1 30); do
    sleep 1
    
    GATEWAY_OK=false
    SIDECAR_OK=false
    
    # Check if processes are still running
    if kill -0 "$GATEWAY_PID" 2>/dev/null; then
        GATEWAY_OK=true
    fi
    if kill -0 "$SIDECAR_PID" 2>/dev/null; then
        SIDECAR_OK=true
    fi
    
    if $GATEWAY_OK && $SIDECAR_OK; then
        READY=true
        break
    fi
    
    echo "  Waiting... ($i/30)"
done

if ! $READY; then
    echo "ERROR: One or both servers failed to start." >&2
    kill "$GATEWAY_PID" "$SIDECAR_PID" 2>/dev/null || true
    exit 1
fi

echo ""
echo "=============================================="
echo " Servers are running!"
echo "=============================================="
echo ""
echo "  Gateway:  localhost:$GATEWAY_PORT (PID $GATEWAY_PID)"
echo "  Sidecar:  localhost:$SIDECAR_PORT (PID $SIDECAR_PID)"
echo ""
echo " To verify connectivity, run:"
echo "   python3 scripts/verify.py"
echo ""
echo " To stop both servers:"
echo "   kill $GATEWAY_PID $SIDECAR_PID"
echo ""

# ── Keep script alive and monitor processes ────────────────────────────────────
echo "Monitoring servers (Ctrl+C to stop)..."
trap 'echo; echo "Shutting down..."; kill "$GATEWAY_PID" "$SIDECAR_PID" 2>/dev/null; exit 0' INT TERM

while true; do
    sleep 5
    
    # Check if either process died
    if ! kill -0 "$GATEWAY_PID" 2>/dev/null; then
        echo "ERROR: Gateway (PID $GATEWAY_PID) has exited." >&2
        kill "$SIDECAR_PID" 2>/dev/null || true
        exit 1
    fi
    if ! kill -0 "$SIDECAR_PID" 2>/dev/null; then
        echo "ERROR: Sidecar (PID $SIDECAR_PID) has exited." >&2
        kill "$GATEWAY_PID" 2>/dev/null || true
        exit 1
    fi
    
    echo "[$(date +%H:%M:%S)] Both servers running OK"
done
