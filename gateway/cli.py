"""Gateway CLI entry point — fleet-gateway command."""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Optional


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="fleet-gateway",
        description="LinuxCNC Fleet Gateway — central gRPC server for machine discovery, routing, and broadcast commands.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=50051,
        help="gRPC server port (default: 50051)",
    )
    parser.add_argument(
        "--cert",
        type=str,
        default=None,
        help="Server TLS certificate path (PEM). Required for TLS.",
    )
    parser.add_argument(
        "--key",
        type=str,
        default=None,
        help="Server TLS private key path (PEM). Required for TLS.",
    )
    parser.add_argument(
        "--root-cert",
        type=str,
        default=None,
        help="Root CA certificate for mTLS client verification. Enables mTLS when provided.",
    )
    parser.add_argument(
        "--jwt-secret",
        type=str,
        default=None,
        help="HS256 JWT signing secret. Mutually exclusive with JWKS URL.",
    )
    parser.add_argument(
        "--jwks-url",
        type=str,
        default=None,
        help="JWKS endpoint URL for RS256/RS384/RS512 token validation. Mutually exclusive with JWT secret.",
    )
    parser.add_argument(
        "--issuer",
        type=str,
        default=None,
        help="Expected JWT issuer (iss) claim. Validates against Keycloak/Auth0 etc.",
    )
    parser.add_argument(
        "--audience",
        type=str,
        default=None,
        help="Expected JWT audience (aud) claim.",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        default=False,
        help="Enable verbose logging.",
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


def validate_args(args: argparse.Namespace) -> list[str]:
    errors = []

    if (args.cert is None) != (args.key is None):
        errors.append("--cert and --key must both be provided or both omitted")

    if args.jwt_secret and args.jwks_url:
        errors.append("--jwt-secret and --jwks-url are mutually exclusive")

    if not args.jwt_secret and not args.jwks_url:
        errors.append("Either --jwt-secret or --jwks-url must be provided for OIDC validation")

    return errors


def setup_logging(
    verbose: bool = False,
    use_syslog: bool = False,
    syslog_address: str = "/dev/log",
    syslog_facility: str = "user",
) -> None:
    level = logging.DEBUG if verbose else logging.INFO

    from linuxcnc_fleet.logging_config import setup_logging as _setup_logging

    _setup_logging(
        level=level,
        use_syslog=use_syslog,
        syslog_address=syslog_address,
        syslog_facility=syslog_facility,
    )


def create_auth_manager(args: argparse.Namespace):
    from gateway.auth import AuthManager

    return AuthManager(
        issuer=args.issuer or "https://example.com",
        audience=args.audience or "fleet-api",
        jwks_url=args.jwks_url,
        secret_key=args.jwt_secret,
    )


def main(argv: Optional[list[str]] = None) -> None:
    args = parse_args(argv)

    errors = validate_args(args)
    if errors:
        for error in errors:
            print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)

    setup_logging(
        verbose=args.verbose,
        use_syslog=args.syslog,
        syslog_address=args.syslog_address,
        syslog_facility=args.syslog_facility,
    )
    log = logging.getLogger(__name__)

    auth_manager = create_auth_manager(args)

    from gateway.policies import PolicyEngine
    from gateway.registry import MachineRegistry
    from gateway.server import run_gateway_server

    policy_engine = PolicyEngine()
    registry = MachineRegistry()

    try:
        tls_enabled = args.cert is not None and args.key is not None
        run_gateway_server(
            auth_manager=auth_manager,
            policy_engine=policy_engine,
            registry=registry,
            port=args.port,
            tls_enabled=tls_enabled,
            cert_file=args.cert,
            key_file=args.key,
            root_cert_file=args.root_cert,
        )
    except KeyboardInterrupt:
        log.info("Gateway server interrupted")
        sys.exit(0)
