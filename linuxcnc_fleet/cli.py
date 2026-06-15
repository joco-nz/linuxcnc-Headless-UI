"""CLI entry point: headless-server --ini ... --machine-id ... --port ..."""

from __future__ import annotations

import argparse
import logging
import os
import sys

from linuxcnc_fleet.headless import LinuxCncSidecar
from linuxcnc_fleet.server import run_server


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="headless-server",
        description="LinuxCNC Fleet Sidecar — exposes a gRPC server for remote CNC control",
    )
    parser.add_argument(
        "--ini",
        default=None,
        help="Path to LinuxCNC INI file (default: auto-detect)",
    )
    parser.add_argument(
        "--machine-id",
        default=None,
        help="Unique machine identifier (default: 'default')",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=None,
        help="Sidecar polling interval in seconds (default: 0.02 / 50Hz). Can also be set via LINUXCNC_FLEET_POLL_INTERVAL env var.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=None,
        help="gRPC server worker thread pool size (default: 8). Can also be set via SIDECAR_GRPC_WORKERS env var.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("SIDECAR_PORT", "50051")),
        help="gRPC listen port (default: 50051)",
    )
    parser.add_argument(
        "--cert",
        default=None,
        help="TLS server certificate path (PEM). Omit for insecure mode.",
    )
    parser.add_argument(
        "--key",
        default=None,
        help="TLS server private key path (PEM). Required with --cert.",
    )
    parser.add_argument(
        "--root-cert",
        default=None,
        help="Root CA certificate for mTLS client auth. Requires --cert and --key.",
    )
    parser.add_argument(
        "--gateway",
        action="store_true",
        default=False,
        help="Expose FleetGatewayService RPCs in addition to FleetService",
    )
    parser.add_argument(
        "--gateway-address",
        default=None,
        help="Gateway address for auto-registration (host:port). Sidecar will register itself with the gateway on startup.",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="Increase log verbosity (-v: INFO, -vv: DEBUG)",
    )
    parser.add_argument(
        "--jwt-secret",
        default=None,
        help="HS256 JWT signing secret for OIDC token validation.",
    )
    parser.add_argument(
        "--jwks-url",
        default=None,
        help="JWKS endpoint URL for RS256/RS384/RS512 token validation.",
    )
    parser.add_argument(
        "--issuer",
        default=None,
        help="Expected JWT issuer (iss) claim.",
    )
    parser.add_argument(
        "--audience",
        default=None,
        help="Expected JWT audience (aud) claim.",
    )
    parser.add_argument(
        "--syslog",
        action="store_true",
        default=False,
        help="Enable logging to syslog in addition to stderr.",
    )
    parser.add_argument(
        "--syslog-address",
        default="/dev/log",
        help="Syslog socket path (default: /dev/log).",
    )
    parser.add_argument(
        "--syslog-facility",
        default="user",
        help="Syslog facility name (default: user). Options: kern, user, daemon, mail, syslog, auth, local0-local7.",
    )

    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)

    # Configure logging
    from linuxcnc_fleet.logging_config import setup_logging

    log_level = logging.DEBUG if args.verbose >= 2 else logging.INFO if args.verbose >= 1 else logging.WARNING
    setup_logging(
        level=log_level,
        use_syslog=args.syslog,
        syslog_address=args.syslog_address,
        syslog_facility=args.syslog_facility,
    )

    # Validate TLS arguments
    if args.cert and not args.key:
        logging.error("--key is required when --cert is specified")
        sys.exit(1)
    if args.key and not args.cert:
        logging.error("--cert is required when --key is specified")
        sys.exit(1)
    if args.root_cert and (not args.cert or not args.key):
        logging.error("--root-cert requires both --cert and --key")
        sys.exit(1)

    # Validate OIDC arguments
    user_extractor = None
    if args.jwt_secret or args.jwks_url:
        if args.jwt_secret and args.jwks_url:
            logging.error("--jwt-secret and --jwks-url are mutually exclusive")
            sys.exit(1)
        try:
            from gateway.auth import AuthManager
            auth_manager = AuthManager(
                secret_key=args.jwt_secret,
                jwks_url=args.jwks_url,
                issuer=args.issuer,
                audience=args.audience,
            )
            user_extractor = auth_manager.extract_user
            logging.info("OIDC authentication enabled")
        except Exception as e:
            logging.error("Failed to initialize auth manager: %s", e)
            sys.exit(1)

    # Create sidecar
    try:
        sidecar = LinuxCncSidecar(
            ini_path=args.ini,
            machine_id=args.machine_id,
            poll_interval=args.poll_interval,
        )
    except RuntimeError as e:
        logging.error("Failed to initialize sidecar: %s", e)
        sys.exit(1)

    # Auto-register with gateway if --gateway-address is provided
    if args.gateway_address and args.jwt_secret:
        try:
            import time
            import grpc
            
            from linuxcnc_fleet.fleet_pb2 import RegisterRequest
            from linuxcnc_fleet.fleet_pb2_grpc import FleetGatewayServiceStub

            # Try to import jwt for token creation
            try:
                import jwt as pyjwt
                
                payload = {
                    "iss": args.issuer or "linuxcnc-fleet",
                    "aud": args.audience or "fleet-api",
                    "sub": sidecar._machine_id,
                    "role": "admin",
                    "iat": int(time.time()),
                    "exp": int(time.time()) + 3600,
                }
                token = pyjwt.encode(payload, args.jwt_secret, algorithm="HS256")
                
                from fleet_client.auth import BearerAuthInterceptor
                
                interceptor = BearerAuthInterceptor(token)
                channel = grpc.insecure_channel(args.gateway_address)
                channel = grpc.intercept_channel(channel, interceptor)
                stub = FleetGatewayServiceStub(channel)
                request = RegisterRequest(
                    machine_id=sidecar._machine_id,
                    address="localhost",
                    port=args.port,
                    facility="",
                    tags=[],
                    version="",
                )
                
                # Retry registration with backoff (gateway may not be ready yet)
                registered = False
                for attempt in range(5):
                    try:
                        response = stub.RegisterMachine(request)
                        channel.close()
                        if response.success:
                            logging.info("Registered with gateway at %s: %s", args.gateway_address, response.message)
                        else:
                            logging.error("Gateway rejected registration: %s", response.message)
                            sys.exit(1)
                        registered = True
                        break
                    except grpc.RpcError as e:
                        if attempt < 4:
                            logging.debug("Registration attempt %d failed: %s, retrying...", attempt + 1, e.code())
                            time.sleep(1)
                        else:
                            raise
                
                if not registered:
                    logging.warning("Could not register with gateway after 5 attempts")
            except ImportError as e:
                logging.warning("PyJWT not installed — skipping auto-registration (%s)", e)
        except Exception as e:
            logging.error("Auto-registration failed: %s", e)
            logging.warning("Continuing without gateway registration")

    # Run server (blocks until interrupted)
    workers = args.workers if args.workers is not None else int(os.environ.get("SIDECAR_GRPC_WORKERS", "8"))
    run_server(
        sidecar=sidecar,
        port=args.port,
        cert_file=args.cert,
        key_file=args.key,
        root_cert_file=args.root_cert,
        use_gateway=args.gateway,
        user_extractor=user_extractor,
        max_workers=workers,
    )


if __name__ == "__main__":
    main()
