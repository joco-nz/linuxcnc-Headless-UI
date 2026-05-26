"""CLI entry point: headless-server --ini ... --machine-id ... --port ..."""

from __future__ import annotations

import argparse
import logging
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
        "--port",
        type=int,
        default=50051,
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

    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)

    # Configure logging
    log_level = logging.DEBUG if args.verbose >= 2 else logging.INFO if args.verbose >= 1 else logging.WARNING
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
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
        sidecar = LinuxCncSidecar(ini_path=args.ini, machine_id=args.machine_id)
    except RuntimeError as e:
        logging.error("Failed to initialize sidecar: %s", e)
        sys.exit(1)

    # Run server (blocks until interrupted)
    run_server(
        sidecar=sidecar,
        port=args.port,
        cert_file=args.cert,
        key_file=args.key,
        root_cert_file=args.root_cert,
        use_gateway=args.gateway,
        user_extractor=user_extractor,
    )


if __name__ == "__main__":
    main()
