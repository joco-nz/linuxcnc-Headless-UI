# Single-Machine Setup — LinuxCNC Fleet

Run the gateway and sidecar on a single LinuxCNC 2.9 machine for testing. Uses insecure gRPC with HS256 shared-secret JWT auth.

## Prerequisites

- LinuxCNC 2.9 installed (uspace or RTAI)
- `linuxcnc` Python module importable
- `linuxcnc-fleet` pip package installed: `pip install dist/linuxcnc_fleet-*.whl`

## Quick Start

```bash
# Start both servers (auto-detects INI file, derives machine ID from hostname)
./scripts/setup-single-machine.sh

# Or specify an INI file and machine ID explicitly
./scripts/setup-single-machine.sh --ini /path/to/file.ini --machine-id my-machine-01
```

Environment variables override defaults:

| Variable | Default | Purpose |
|----------|---------|---------|
| `FLEET_JWT_SECRET` | `my-shared-secret` | HS256 signing key |
| `FLEET_JWT_ISSUER` | `linuxcnc-fleet` | JWT issuer claim |
| `FLEET_JWT_AUDIENCE` | `fleet-api` | JWT audience claim |

```bash
FLEET_JWT_SECRET=supersecret ./scripts/setup-single-machine.sh --ini ~/linuxcnc/configs/my_machine/my_machine.ini
```

## What It Does

1. Validates prerequisites (linuxcnc module, pip package)
2. Kills any existing processes on ports 50051/50052
3. Starts **gateway** on `:50052` with HS256 JWT auth
4. Starts **sidecar** on `:50051` with `--gateway` flag (exposes FleetGatewayService too)
5. Monitors both processes; exits if either dies

## Verify Connectivity

```bash
python3 scripts/verify.py
```

This runs 5 checks:

| # | Check | What it tests |
|---|-------|---------------|
| 1 | Discover machines | Gateway → Registry lookup |
| 2 | Route machine | Gateway routing to sidecar address |
| 3 | Get status | Sidecar → MachineStatus protobuf |
| 4 | Get machine info | INI file parsing + version info |
| 5 | List HAL components | `hal` module enumeration (skipped on non-RT) |

### Custom verification parameters

```bash
python3 scripts/verify.py --gateway localhost:50052 --token <jwt> --secret my-shared-secret
```

## Architecture

```
┌─────────────────────────────────────┐
│  Same LinuxCNC 2.9 machine          │
│                                     │
│  ┌──────────┐   gRPC :50051   ┌────┴────┐
│  │ Sidecar  │ ◄──────────────► │ Gateway │
│  │          │                  │         │
│  │ linuxcnc │                  │ JWT     │
│  │ module   │                  │ HS256   │
│  └──────────┘                  └─────────┘
└─────────────────────────────────────┘
```

## Ports

| Service | Port | Description |
|---------|------|-------------|
| Sidecar | `50051` | FleetService + FleetGatewayService RPCs |
| Gateway | `50052` | FleetGatewayService RPCs (discovery, routing, broadcast) |

## Stopping Servers

The setup script monitors both processes. Press **Ctrl+C** to gracefully shut them down.

Or kill manually:

```bash
kill $(lsof -ti :50051) $(lsof -ti :50052) 2>/dev/null || true
```

## Using FleetClient Remotely

For external clients connecting through the gateway:

```python
import asyncio
from fleet_client import FleetClient

async def main():
    client = FleetClient(
        gateway_address="localhost:50052",  # or remote host
        token="<jwt-token>",                # generate with scripts/verify.py --help
        tls_enabled=False,                  # True in production
    )
    async with client:
        machines = await client.get_machines()
        for m in machines:
            print(f"{m.machine_id} at {m.host_address}")

asyncio.run(main())
```

Generate a valid token:

```bash
python3 -c "
import jwt, time
token = jwt.encode({
    'iss': 'linuxcnc-fleet',
    'aud': 'fleet-api',
    'sub': 'my-client',
    'scope': 'admin',
    'iat': int(time.time()),
}, 'my-shared-secret', algorithm='HS256')
print(token)
"
```

## Troubleshooting

### "linuxcnc Python module not found"
Activate your LinuxCNC environment first:
```bash
source /usr/linuxcnc/environment  # or wherever your setup is
```

### "linuxcnc-fleet pip package not installed"
```bash
pip install dist/linuxcnc_fleet-*.whl
```

### Sidecar can't find INI file
Pass it explicitly:
```bash
./scripts/setup-single-machine.sh --ini /path/to/your.ini
```

### Port already in use
The script kills existing processes automatically. If it fails, kill manually:
```bash
kill $(lsof -ti :50051) $(lsof -ti :50052) 2>/dev/null || true
```

### No machines discovered
- Ensure the sidecar started successfully (check its logs)
- Verify `--gateway` flag is present on the sidecar
- Check that the gateway and sidecar are using matching JWT secrets/issuer/audience

### HAL components not listed
The `hal` module may not be available on all LinuxCNC configurations (e.g., userspace simulation without RT kernel). On non-RT setups this check will be skipped with a note — this is expected.
