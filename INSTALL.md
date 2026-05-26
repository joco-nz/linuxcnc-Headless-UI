# Installation Guide

## Prerequisites

- **Python 3.10+** (tested with 3.10, 3.11, 3.12)
- **pip** ≥ 23.0
- LinuxCNC Python bindings (`linuxcnc`, `_hal`) — sidecar machines only

## Quick Install

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

# Run the test suite (requires [dev] extras)
python -m pytest tests/ -v
```

Expected output: `344 passed`.

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
```

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
ExecStart=/path/to/venv/bin/headless-server \
    --ini /path/to/machine.ini \
    --machine-id machine1 \
    --port 50051 \
    --cert /etc/linuxcnc-fleet/certs/server.pem \
    --key /etc/linuxcnc-fleet/certs/server-key.pem

ExecStartPre=/bin/sh -c 'sleep 5'

[Install]
WantedBy=multi-user.target
```

### Gateway Server

```bash
fleet-gateway \
    --port 50050 \
    --jwt-secret "your-32-byte-minimum-secret-key-here!!" \
    --issuer https://keycloak.example.com/realms/linuxcnc \
    --audience fleet-api

# For RS256 with JWKS:
fleet-gateway \
    --port 50050 \
    --jwks-url https://keycloak.example.com/realms/linuxcnc/protocol/openid-connect/certs \
    --issuer https://keycloak.example.com/realms/linuxcnc \
    --audience fleet-api

# With mTLS (require client certs from instances):
fleet-gateway \
    --port 50050 \
    --jwt-secret "your-secret" \
    --issuer https://keycloak.example.com/realms/linuxcnc \
    --audience fleet-api \
    --cert gateway.pem \
    --key gateway-key.pem \
    --root-cert ca.pem
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
