#!/usr/bin/env bash
# start-ui.sh — Start Fleet Dashboard UI connected to gateway+sidecar.
#
# Assumes gateway and sidecar were started by setup-single-machine.sh.
# Auto-generates a JWT token using the same secret, then launches fleet-ui.
#
# Usage:
#   ./scripts/start-ui.sh                  # defaults (port 8080, bind 0.0.0.0)
#   ./scripts/start-ui.sh --port 9090      # custom port
#   ./scripts/start-ui.sh --bind 127.0.0.1 # bind to specific interface only
#   ./scripts/start-ui.sh --allow-origin http://example.com # restrict CORS origin
#
# Exit codes: 0 = UI started successfully, 1 = failure

set -euo pipefail

# ── Configuration (must match setup-single-machine.sh defaults) ───────────────
GATEWAY_PORT=50052
JWT_SECRET="${FLEET_JWT_SECRET:-my-shared-secret}"
JWT_ISSUER="${FLEET_JWT_ISSUER:-linuxcnc-fleet}"
JWT_AUDIENCE="${FLEET_JWT_AUDIENCE:-fleet-api}"

# ── Parse arguments ───────────────────────────────────────────────────────────
UI_PORT=8080
BIND_ADDR="0.0.0.0"
ALLOW_ORIGIN="*"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --port)
            UI_PORT="$2"; shift 2 ;;
        --bind)
            BIND_ADDR="$2"; shift 2 ;;
        --allow-origin)
            ALLOW_ORIGIN="$2"; shift 2 ;;
        --help|-h)
            echo "Usage: $0 [OPTIONS]"
            echo ""
            echo "Starts the Fleet Dashboard UI connected to the gateway."
            echo "Assumes gateway is running on localhost:$GATEWAY_PORT."
            echo ""
            echo "Options:"
            echo "  --port <N>           Listen port (default: $UI_PORT)"
            echo "  --bind <addr>        Bind address (default: $BIND_ADDR)"
            echo "  --allow-origin <url> CORS allowed origin (default: * for all, or http://example.com)"
            exit 0 ;;
        *)
            echo "Unknown option: $1" >&2
            exit 1 ;;
    esac
done

echo "=============================================="
echo " LinuxCNC Fleet — Dashboard UI"
echo "=============================================="
echo "  Gateway:      localhost:$GATEWAY_PORT"
echo "  Listen:       $BIND_ADDR:$UI_PORT"
echo "  CORS origin:  ${ALLOW_ORIGIN}"
echo ""

# ── Prerequisite checks ───────────────────────────────────────────────────────
if ! pip show linuxcnc-fleet &>/dev/null; then
    echo "ERROR: linuxcnc-fleet pip package not installed." >&2
    exit 1
fi

if ! python3 -c "import jwt" 2>/dev/null && ! python3 -c "import PyJWT as jwt" 2>/dev/null; then
    echo "ERROR: PyJWT not installed. Install it with: pip install pyjwt" >&2
    exit 1
fi

if ! command -v fleet-ui &>/dev/null; then
    echo "ERROR: 'fleet-ui' command not found in PATH." >&2
    echo "       Ensure linuxcnc-fleet package is installed and venv is activated." >&2
    exit 1
fi

# ── Check gateway is reachable ────────────────────────────────────────────────
echo -n "Checking gateway on localhost:$GATEWAY_PORT ... "
if ! python3 -c "
import socket, sys
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.settimeout(2)
try:
    s.connect(('localhost', $GATEWAY_PORT))
    print('OK')
    sys.exit(0)
except Exception:
    print('NOT REACHABLE', file=sys.stderr)
    sys.exit(1)
finally:
    s.close()
" 2>/dev/null; then
    echo "ERROR: Gateway not reachable on localhost:$GATEWAY_PORT." >&2
    echo "       Start it first with: ./scripts/setup-single-machine.sh" >&2
    exit 1
fi

# ── Generate JWT token ────────────────────────────────────────────────────────
echo -n "Generating JWT token ... "
TOKEN=$(python3 -c "
import jwt, time, sys
payload = {
    'iss': '$JWT_ISSUER',
    'aud': '$JWT_AUDIENCE',
    'sub': 'fleet-ui',
    'role': 'admin',
    'iat': int(time.time()),
    'exp': int(time.time()) + 3600,
}
print(jwt.encode(payload, '$JWT_SECRET', algorithm='HS256'))
")
echo "OK"

# ── Kill any existing UI on this port ─────────────────────────────────────────
EXISTING=$(lsof -ti :"$UI_PORT" 2>/dev/null || true)
if [[ -n "$EXISTING" ]]; then
    echo "Stopping existing process on port $UI_PORT (PID: $EXISTING)..."
    kill "$EXISTING" 2>/dev/null || true
    sleep 1
fi

# ── Start Fleet UI ────────────────────────────────────────────────────────────
echo ""
echo "Starting Fleet Dashboard on :$UI_PORT ..."
fleet-ui \
    --gateway "localhost:$GATEWAY_PORT" \
    --bind "$BIND_ADDR" \
    --port "$UI_PORT" \
    --allow-origin "$ALLOW_ORIGIN" \
    --token "$TOKEN" &
UI_PID=$!
echo "  UI PID: $UI_PID"
echo ""

# ── Wait for UI to be ready ───────────────────────────────────────────────────
echo "Waiting for UI to start ..."
for i in $(seq 1 10); do
    sleep 1
    if curl -s -o /dev/null -w "%{http_code}" "http://localhost:$UI_PORT/" | grep -q "200"; then
        echo ""
        echo "=============================================="
        echo " Fleet Dashboard is ready!"
        echo "=============================================="
        echo ""
        if [[ "$BIND_ADDR" == "0.0.0.0" ]]; then
            MY_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || true)
            if [[ -n "$MY_IP" ]]; then
                echo "  Local access:   http://localhost:$UI_PORT"
                echo "  Remote access:  http://$MY_IP:$UI_PORT"
            else
                echo "  Access URL:     http://localhost:$UI_PORT"
            fi
        else
            echo "  Access URL:     http://$BIND_ADDR:$UI_PORT"
        fi
        echo ""
        echo " To stop the UI:"
        echo "   kill $UI_PID"
        echo ""
        break
    fi
    echo -n "."
done

# ── Monitor and keep alive ────────────────────────────────────────────────────
trap 'echo; echo "Shutting down UI..."; kill "$UI_PID" 2>/dev/null; exit 0' INT TERM

if [[ $i -eq 10 ]]; then
    echo ""
    echo "WARNING: UI may not have started successfully."
    echo "         Check output above for errors."
fi

wait "$UI_PID"
