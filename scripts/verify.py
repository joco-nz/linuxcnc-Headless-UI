#!/usr/bin/env python3
"""verify.py — End-to-end verification for single-machine LinuxCNC Fleet setup.

Connects to the gateway, discovers machines, reads status, and tests
command routing through the full stack.

Usage:
    python3 scripts/verify.py
    python3 scripts/verify.py --gateway localhost:50052 --token <jwt>
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time

try:
    import jwt
except ImportError:
    jwt = None  # type: ignore[assignment]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="LinuxCNC Fleet end-to-end verification")
    parser.add_argument("--gateway", default="localhost:50052", help="Gateway address (host:port)")
    parser.add_argument("--token", default=None, help="JWT token (auto-generated if not provided)")
    parser.add_argument("--secret", default="my-shared-secret", help="HS256 signing secret")
    parser.add_argument("--issuer", default="linuxcnc-fleet", help="JWT issuer claim")
    parser.add_argument("--audience", default="fleet-api", help="JWT audience claim")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging")
    return parser.parse_args()


def make_token(secret: str, issuer: str = "linuxcnc-fleet", audience: str = "fleet-api") -> str:
    """Create a valid HS256 JWT token with admin scope."""
    if jwt is None:
        print("ERROR: PyJWT not installed. Install it with: pip install pyjwt")
        sys.exit(1)

    payload = {
        "iss": issuer,
        "aud": audience,
        "sub": "verify-script",
        "role": "admin",
        "iat": int(time.time()),
        "exp": int(time.time()) + 3600,
    }
    return jwt.encode(payload, secret, algorithm="HS256")


async def run_checks(client, args) -> bool:
    """Run all verification checks. Returns True if all passed."""
    from fleet_client import MachineEntry
    from linuxcnc_fleet.fleet_pb2 import Mode

    results: list[tuple[str, bool, str]] = []
    machines: list[MachineEntry] = []

    # ── Check 1: Discover machines ───────────────────────────────────────
    print("\n[1/5] Discovering machines via gateway ...")
    try:
        machines = await client.get_machines()
        if not machines:
            results.append(("Discover machines", False, "No machines registered"))
        else:
            for m in machines:
                print(f"  Found: {m.machine_id} ({m.host_address}, joints={m.num_joints})")
            results.append(("Discover machines", True, f"{len(machines)} machine(s) found"))
    except Exception as e:
        results.append(("Discover machines", False, str(e)))

    # ── Check 2: Route to first machine ──────────────────────────────────
    if machines:
        target = machines[0]
        print(f"\n[2/5] Routing to {target.machine_id} ...")
        try:
            addr, port = await client.route_machine(target.machine_id)
            print(f"  Routed to: {addr}:{port}")
            results.append(("Route machine", True, f"{addr}:{port}"))
        except Exception as e:
            results.append(("Route machine", False, str(e)))

        # ── Check 3: Get status via gateway routing ───────────────────────
        print(f"\n[3/5] Getting status from {target.machine_id} ...")
        try:
            status = await client.get_status(target.machine_id)
            mode_name = Mode.Name(status.mode) if status.mode else "UNKNOWN"
            state_name = type(status.state).__name__  # enum int value
            print(f"  Machine ID: {status.machine_id}")
            print(f"  State: {state_name}")
            print(f"  Mode: {mode_name}")
            print(f"  Estop: {'E-stopped' if status.estop_state else 'Not E-stopped'}")
            print(f"  Interp state: {status.interp_state}")
            print(f"  Program file: {status.program_file or '(none)'}")
            results.append(("Get status", True, f"mode={mode_name}"))
        except Exception as e:
            results.append(("Get status", False, str(e)))

        # ── Check 4: Get machine info ─────────────────────────────────────
        print(f"\n[4/5] Getting machine info from {target.machine_id} ...")
        try:
            info = await client.get_machine_info(target.machine_id)
            print(f"  Machine name: {info.machine_name}")
            print(f"  Host address: {info.host_address}")
            print(f"  Num joints: {info.num_joints}")
            if info.version:
                print(f"  Version: {info.version.version_string or '(none)'}")
                print(f"  Build type: {info.version.build_type or '(unknown)'}")
            results.append(("Get machine info", True, f"joints={info.num_joints}"))
        except Exception as e:
            results.append(("Get machine info", False, str(e)))

        # ── Check 5: List HAL components (best-effort) ────────────────────
        print(f"\n[5/5] Listing HAL components from {target.machine_id} ...")
        try:
            hal_list = await client.list_hal_components(target.machine_id)
            if not hal_list.components:
                print("  No HAL components (expected on non-RT machines)")
                results.append(("List HAL components", True, "no components"))
            else:
                for comp in hal_list.components[:5]:  # Show first 5
                    pin_count = len(comp.pins)
                    print(f"  {comp.name}: {pin_count} pins")
                if len(hal_list.components) > 5:
                    print(f"  ... and {len(hal_list.components) - 5} more")
                results.append(("List HAL components", True, f"{len(hal_list.components)} component(s)"))
        except Exception as e:
            # hal module not available is expected on non-RT setups
            if "hal" in str(e).lower() or "not available" in str(e):
                print(f"  HAL not available (expected without RT kernel): {e}")
                results.append(("List HAL components", True, "skipped (no hal)"))
            else:
                results.append(("List HAL components", False, str(e)))

    # ── Summary ──────────────────────────────────────────────────────────
    print("\n" + "=" * 50)
    print(" VERIFICATION SUMMARY")
    print("=" * 50)
    all_passed = True
    for name, passed, detail in results:
        status = "PASS" if passed else "FAIL"
        marker = "[OK]" if passed else "[!!]"
        print(f"  {marker} {status}: {name} — {detail}")
        if not passed:
            all_passed = False

    print("=" * 50)
    if all_passed and machines:
        print("  All checks PASSED. Stack is working correctly.")
    elif not machines:
        print("  WARNING: No machines registered. Make sure the sidecar")
        print("           is running and has connected to the gateway.")
    else:
        print("  Some checks FAILED. See details above.")

    return all_passed


async def main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )

    # Generate or use provided token
    token = args.token or make_token(args.secret, args.issuer, args.audience)
    print(f"Connecting to gateway at {args.gateway} ...")
    print(f"Token: {token[:40]}... (truncated)")

    from fleet_client import FleetClient

    client = FleetClient(
        gateway_address=args.gateway,
        token=token,
        tls_enabled=False,
    )

    try:
        async with client:
            passed = await run_checks(client, args)
            sys.exit(0 if passed else 1)
    except Exception as e:
        print(f"\nFATAL: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(2)


if __name__ == "__main__":
    asyncio.run(main())
