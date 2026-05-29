# Installation Guide

## Prerequisites

- **Python 3.10+** (tested with 3.10, 3.11, 3.12)
- **pip** ≥ 23.0
- LinuxCNC Python bindings (`linuxcnc`, `_hal`) — sidecar machines only

## Quick Install

| Component | Package | Purpose |
|-----------|---------|---------|
| **Sidecar** | `linuxcnc-fleet[sidecar]` | Runs on each CNC machine. Wraps `linuxcnc` and `_hal` Python modules behind a gRPC server. |
| **Gateway** | `linuxcnc-fleet[gateway]` | Central auth & routing service. OIDC token validation, RBAC policies, machine discovery, broadcast fan-out. |
| **FleetClient** | `linuxcnc-fleet[client]` | Python client library with automatic OIDC auth injection, retry logic, channel caching, and streaming subscriptions. |
| **fleet_ui** | `linuxcnc-fleet[ui]` | Web dashboard with real-time status via Server-Sent Events (SSE). |
| **Development** | `linuxcnc-fleet[dev]` | All optional deps plus `pytest`, `pytest-asyncio`, and `mypy`. |

### Sidecar (runs on each CNC machine)

```bash
pip install "linuxcnc-fleet[sidecar]"
```

This installs the core gRPC dependencies plus `grpcio-tools` for proto regeneration if needed.

### Gateway (central auth & routing server)

```bash
pip install "linuxcnc-fleet[gateway]"
```

Includes all sidecar deps plus `PyJWT`, `cryptography`, and `aiohttp` for OIDC validation.

### Client Library (for building UIs or scripts)

```bash
pip install "linuxcnc-fleet[client]"
```

Minimal dependencies: just `grpcio` and `protobuf`.

### Web Dashboard

```bash
pip install "linuxcnc-fleet[ui]"
```

Installs core deps plus `aiohttp` for the web server. Run with:

```bash
fleet-ui --gateway gateway-host:50051
```

By default the dashboard listens on port 8080. Use `--port` to change it and `--tls-cert`/`--tls-key` for HTTPS.

### Development

```bash
pip install "linuxcnc-fleet[dev]"
```

Installs all optional deps plus `pytest`, `pytest-asyncio`, and `mypy`.

## Install from Source

Clone the repository and install in editable mode:

```bash
git clone https://github.com/<user>/linuxcnc-Headless-UI.git
cd linuxcnc-Headless-UI
pip install -e ".[dev]"
```

Or build and install a wheel:

```bash
pip install build
python -m build
pip install dist/linuxcnc_fleet-0.1.0-py3-none-any.whl
```

## Proto Code Generation

If you modify `proto/fleet.proto`, regenerate the Python stubs:

```bash
python -m grpc_tools.protoc -I. \
    --python_out=linuxcnc_fleet \
    --grpc_python_out=linuxcnc_fleet \
    proto/fleet.proto

# Fix imports for package installation
sed -i 's/^import fleet_pb2 as/import linuxcnc_fleet.fleet_pb2 as/' linuxcnc_fleet/fleet_pb2_grpc.py
```

The generated `fleet_pb2_grpc.py` uses top-level imports (`import fleet_pb2`) which break when installed as a package. The sed fix is required.

## Verify Installation

```bash
# Check entry points are available
headless-server --help
fleet-gateway --help
fleet-ui --help

# Run the test suite (requires [dev] extras)
python -m pytest tests/ -v
```

Expected output: `379 passed`.

## Minimal Testing Setup

The test suite uses mock modules for `linuxcnc` and `_hal`, so you can run all tests and verify installations **without** having LinuxCNC installed. This is useful for CI pipelines, development workstations, and containerized environments.

```bash
# Install dev dependencies
pip install "linuxcnc-fleet[dev]"

# Run the full test suite — no LinuxCNC required
python -m pytest tests/ -v
```

### Quick integration test (gateway + sidecar)

To verify gateway-to-sidecar communication without real LinuxCNC, use the conftest fixtures:

```bash
python -m pytest tests/test_integration.py -v -k "test_discover"
```

This spins up a real gRPC sidecar server (with mocked linuxcnc bindings) and a real gateway server, then exercises the full request-routing pipeline.

### Build a wheel for testing on another machine

```bash
pip install build
python -m build
# Copy the wheel to the target machine
scp dist/linuxcnc_fleet-0.1.0-py3-none-any.whl user@target:/tmp/
pip install /tmp/linuxcnc_fleet-0.1.0-py3-none-any.whl
```

## Component-Specific Setup

### Sidecar on a CNC Machine

After installation, configure the sidecar to start after LinuxCNC:

```bash
headless-server \
    --ini /path/to/machine.ini \
    --machine-id lathe-01 \
    --port 50051

# With TLS (mTLS)
headless-server \
    --ini /path/to/machine.ini \
    --machine-id lathe-01 \
    --cert server.pem \
    --key private.pem \
    --root-cert ca.pem

# Logging to syslog
headless-server \
    --ini /path/to/machine.ini \
    --machine-id lathe-01 \
    --syslog \
    --syslog-facility local0
```

| Flag | Description | Default |
|------|-------------|---------|
| `--ini` | Path to LinuxCNC INI file | auto-detect |
| `--machine-id` | Unique machine identifier | `default` |
| `--port` | gRPC listen port | `50051` |
| `--cert` | TLS server certificate (PEM) | — |
| `--key` | TLS server private key (PEM) | — |
| `--root-cert` | Root CA for mTLS client auth | — |
| `-v / -vv` | Increase log verbosity | WARNING |
| `--syslog` | Enable logging to syslog | disabled |
| `--syslog-address` | Syslog socket path | `/dev/log` |
| `--syslog-facility` | Syslog facility (user, daemon, local0-local7) | `user` |

For production, create a systemd service:

```ini
[Unit]
Description=LinuxCNC Fleet Sidecar
After=linuxcnc.service
Wants=linuxcnc.service

[Service]
Type=simple
User=linuxcnc
Group=linuxcnc
Environment=LINUXCNC_FLEET_JWT_SECRET_FILE=/etc/linuxcnc-fleet/jwt-secret
ExecStart=/path/to/venv/bin/headless-server \
    --ini /path/to/machine.ini \
    --machine-id machine1 \
    --port 50051 \
    --cert /etc/linuxcnc-fleet/certs/server.pem \
    --key /etc/linuxcnc-fleet/certs/server-key.pem \
    --syslog \
    --syslog-facility local0

ExecStartPre=/bin/sh -c 'sleep 5'

[Install]
WantedBy=multi-user.target
```

### Gateway Server

```bash
fleet-gateway \
    --port 50051 \
    --jwt-secret "your-32-byte-minimum-secret-key-here!!" \
    --issuer https://keycloak.example.com/realms/linuxcnc \
    --audience fleet-api

# For RS256 with JWKS:
fleet-gateway \
    --port 50051 \
    --jwks-url https://keycloak.example.com/realms/linuxcnc/protocol/openid-connect/certs \
    --issuer https://keycloak.example.com/realms/linuxcnc \
    --audience fleet-api

# With mTLS (require client certs from instances):
fleet-gateway \
    --port 50051 \
    --jwt-secret "your-secret" \
    --issuer https://keycloak.example.com/realms/linuxcnc \
    --audience fleet-api \
    --cert gateway.pem \
    --key gateway-key.pem \
    --root-cert ca.pem

# Logging to syslog with custom facility
fleet-gateway \
    --jwt-secret "your-secret" \
    --issuer https://keycloak.example.com/realms/linuxcnc \
    --audience fleet-api \
    --syslog \
    --syslog-facility daemon
```

| Flag | Description | Default |
|------|-------------|---------|
| `--port` | gRPC server port | `50051` |
| `--cert` | Server TLS certificate (PEM) | — |
| `--key` | Server TLS private key (PEM) | — |
| `--root-cert` | Root CA for mTLS client verification | — |
| `--jwt-secret` | HS256 JWT signing secret | — |
| `--jwks-url` | JWKS endpoint for RS256/RS384/RS512 | — |
| `--issuer` | Expected JWT issuer claim | — |
| `--audience` | Expected JWT audience claim | — |
| `-v` | Enable verbose logging | disabled |
| `--syslog` | Enable logging to syslog | disabled |
| `--syslog-address` | Syslog socket path | `/dev/log` |
| `--syslog-facility` | Syslog facility (user, daemon, local0-local7) | `user` |

For production, create a systemd service:

```ini
[Unit]
Description=LinuxCNC Fleet Gateway
After=network.target

[Service]
Type=simple
User=fleet
Group=fleet
Environment=LINUXCNC_FLEET_JWT_SECRET_FILE=/etc/linuxcnc-fleet/jwt-secret
ExecStart=/path/to/venv/bin/fleet-gateway \
    --port 50051 \
    --issuer https://keycloak.example.com/realms/linuxcnc \
    --audience fleet-api \
    --cert /etc/linuxcnc-fleet/certs/gateway.pem \
    --key /etc/linuxcnc-fleet/certs/gateway-key.pem \
    --syslog \
    --syslog-facility local0

[Install]
WantedBy=multi-user.target
```

### Web Dashboard (fleet_ui)

```bash
# Development — connects to local gateway
fleet-ui --gateway localhost:50051

# Production — with TLS and token from environment
fleet-ui \
    --gateway fleet-gateway.internal:50051 \
    --token "${LINUXCNC_FLEET_TOKEN}" \
    --port 443 \
    --tls-cert /etc/linuxcnc-fleet/certs/ui.pem \
    --tls-key /etc/linuxcnc-fleet/certs/ui-key.pem

# With systemd service
```

Create `/etc/systemd/system/fleet-ui.service`:

```ini
[Unit]
Description=LinuxCNC Fleet Web Dashboard
After=network.target fleet-gateway.service

[Service]
Type=simple
User=fleet
Group=fleet
Environment=LINUXCNC_FLEET_TOKEN=/etc/linuxcnc-ffleet/ui-token
ExecStart=/path/to/venv/bin/fleet-ui \
    --gateway localhost:50051 \
    --port 8080

[Install]
WantedBy=multi-user.target
```

## Production Security

### Secrets Management

**Never pass secrets on the command line** in production — they appear in process listings (`ps aux`) and shell history. Instead, use environment variables or secret files:

```bash
# Set the JWT secret via environment variable (gateway reads it automatically)
export LINUXCNC_FLEET_JWT_SECRET="your-32-byte-minimum-secret-key-here!!"
fleet-gateway --issuer https://keycloak.example.com/realms/linuxcnc --audience fleet-api

# Or use a secret file — read the value at runtime
echo "your-secret" > /etc/linuxcnc-fleet/jwt-secret
chmod 600 /etc/linuxcnc-fleet/jwt-secret
```

For systemd services, set secrets in the unit file using `EnvironmentFile`:

```ini
[Service]
EnvironmentFile=/etc/linuxcnc-fleet/gateway.env
```

Where `/etc/linuxcnc-fleet/gateway.env` contains:

```
LINUXCNC_FLEET_JWT_SECRET=your-32-byte-minimum-secret-key-here!!
```

Set file permissions to `600` (owner read/write only).

### Firewall / Network Ports

The following ports must be accessible for a production deployment:

| Service | Port | Direction | Purpose |
|---------|------|-----------|---------|
| Sidecar | 50051 (default) | Inbound to CNC machine | gRPC from gateway |
| Gateway | 50051 (default) | Inbound to gateway host | gRPC from fleet_ui / FleetClient |
| fleet_ui | 8080 (default) | Inbound to UI host | HTTP(S) for browser access |

Example `ufw` rules:

```bash
sudo ufw allow 50051/tcp   # gateway gRPC
sudo ufw allow 8080/tcp    # fleet_ui web dashboard
# Sidecar ports are inbound on CNC machines only — do not expose to the internet
```

### Certificate Generation (Self-Signed)

For initial testing or internal deployments, generate self-signed certificates:

```bash
# Create a CA key and certificate
openssl genrsa -out ca-key.pem 4096
openssl req -new -x509 -days 365 -key ca-key.pem -out ca.pem \
    -subj "/CN=LinuxCNC Fleet CA"

# Generate server key and CSR
openssl genrsa -out server-key.pem 2048
openssl req -new -key server-key.pem -out server.csr \
    -subj "/CN=lathe-01"

# Sign the server certificate with the CA
openssl x509 -req -days 365 -in server.csr -CA ca.pem -CAkey ca-key.pem \
    -CAcreateserial -out server.pem

# Verify
openssl verify -CAfile ca.pem server.pem
```

For production, use a proper CA (Let's Encrypt, internal PKI) and ensure certificates are renewed before expiry.

### OIDC Provider Configuration

The gateway validates JWTs from an OIDC identity provider. Required configuration on the provider side:

- **Issuer**: Must match the `iss` claim in issued tokens (e.g., `https://keycloak.example.com/realms/linuxcnc`)
- **Audience**: Must match the `--audience` flag (e.g., `fleet-api`)
- **Token format**: Standard JWT with `sub`, `iss`, `aud`, `exp`, and custom role/facility claims

For Keycloak, create a client with:
- Client ID: `fleet-api`
- Access Type: `confidential`
- Valid Redirect URIs: your dashboard URL (e.g., `https://dashboard.example.com/*`)
- Standard Flow Enabled: yes
- Service Accounts Enabled: no (unless using service account tokens)

## Logging Configuration

All components log to stderr by default. Use `--syslog` to also send logs to the system syslog daemon — this is recommended for production deployments managed by journald or rsyslog.

```bash
# Sidecar — logs to both stderr and syslog
headless-server --ini /path/to/machine.ini --machine-id lathe-01 --syslog

# Gateway — logs to both stderr with a custom facility
fleet-gateway --jwt-secret mysecret --issuer https://auth.example.com --audience fleet-api --syslog --syslog-facility local0

# Use systemd-journald socket instead of /dev/log
headless-server --ini /path/to/machine.ini --machine-id lathe-01 --syslog --syslog-address /run/systemd/journal/syslog
```

### Syslog Flags

| Flag | Description | Default |
|------|-------------|---------|
| `--syslog` | Enable logging to syslog (in addition to stderr) | disabled |
| `--syslog-address` | Syslog socket path | `/dev/log` |
| `--syslog-facility` | Facility for syslog messages | `user` |

Supported facilities: `user`, `daemon`, `kern`, `local0` through `local7`.

### Log Format

Syslog messages use a simplified format without timestamps (the syslog daemon adds its own):

```
INFO linuxcnc_fleet.headless: Machine registered: lathe-01
ERROR gateway.server: Failed to route request for unknown machine: ghost-machine
WARNING fleet_client.client: Retry 2/3: UNAVAILABLE connecting to lathe-01
```

Console output retains full timestamps and formatting for interactive use.

### Log Rotation

When using rsyslog, add rotation for the facility you configured:

```bash
# /etc/rsyslog.d/linuxcnc-fleet.conf
local0.*    /var/log/linuxcnc-fleet/sidecar.log
& stop

$AddUnixListenSocket /run/systemd/journal/syslog
```

When using journald, logs are automatically managed. Filter by service name or facility:

```bash
# View sidecar logs from journald
journalctl -u linuxcnc-fleet-sidecar -f

# View gateway logs with a custom facility
journalctl -f _FACILITY=local0
```

## Upgrade & Maintenance

### Upgrading the Package

```bash
# Upgrade all components at once
pip install --upgrade "linuxcnc-fleet[sidecar,gateway,client,ui]"

# Or upgrade individual components
pip install --upgrade "linuxcnc-fleet[gateway]"
```

### Zero-Downtime Sidecar Restart

Sidecars can be restarted without affecting the gateway's machine registry (heartbeats have a TTL):

```bash
sudo systemctl restart linuxcnc-fleet-sidecar@lathe-01
# The gateway will briefly see the machine as "unavailable" but will reconnect automatically
```

### Gateway Config Migration

When upgrading the gateway, ensure OIDC configuration is preserved:

```bash
# Backup current config
cp /etc/linuxcnc-fleet/gateway.env /etc/linuxcnc-fleet/gateway.env.bak

# Restart with new version
sudo systemctl restart fleet-gateway
```

### Checking Component Versions

```bash
python -c "import linuxcnc_fleet; print('sidecar:', linuxcnc_fleet.__version__)"
python -c "import gateway; print('gateway:', gateway.__version__)"
python -c "import fleet_client; print('client:', fleet_client.__version__)"
```

## Troubleshooting

### `ModuleNotFoundError: No module named 'fleet_pb2'`

The generated gRPC stubs need a post-processing step. Run the proto regeneration commands above to fix imports.

### Sidecar fails to start with `RuntimeError`

The sidecar needs access to LinuxCNC's INI file and Python bindings. Ensure:
- The `--ini` path points to a valid LinuxCNC `.ini` file
- The `linuxcnc` and `_hal` modules are importable (LinuxCNC must be installed)
- The user has read access to the INI file and LinuxCNC hal comp pins

### Gateway rejects connections with `UNAUTHENTICATED`

Verify the OIDC configuration matches your identity provider:
- `--issuer` must match the `iss` claim in tokens
- `--audience` must match the `aud` claim
- For HS256, the secret key must exactly match the one used to sign tokens
- For RS256, ensure the JWKS URL is reachable and returns valid keys

### Tests fail with mock errors

The test suite injects mock `linuxcnc` and `_hal` modules via `pytest_configure` in `tests/conftest.py`. Ensure you're running from the project root:

```bash
cd /path/to/linuxcnc-Headless-UI
python -m pytest tests/ -v
```

### Sidecar systemd service fails to start

Check that LinuxCNC has started before the sidecar:
```bash
journalctl -u linuxcnc-fleet-sidecar -n 50 --no-pager
```

Ensure the `linuxcnc` user exists and has access to the INI file:
```bash
sudo -u linuxcnc headless-server --ini /path/to/machine.ini --machine-id test --port 0
```

### Gateway cannot reach sidecar

Verify network connectivity from the gateway host to the sidecar port:
```bash
grpcurl -plaintext gateway-host:50051 list
# or
nc -zv sidecar-host 50051
```

If mTLS is enabled, verify certificates are valid and trusted:
```bash
openssl s_client -connect sidecar-host:50051 -CAfile ca.pem
```

### fleet_ui cannot connect to gateway

Check that the gateway address and token are correct:
```bash
# Test direct connection
python -c "
from fleet_client import FleetClient
import asyncio
async def test():
    c = FleetClient(gateway_address='localhost:50051', token='your-token-here')
    await c.init()
    machines = await c.list_machines('FACILITY', 'shop-floor')
    print(f'Found {len(machines)} machines')
asyncio.run(test())
"
```
