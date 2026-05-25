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


def setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
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

    setup_logging(args.verbose)
    log = logging.getLogger(__name__)

    auth_manager = create_auth_manager(args)

    from gateway.policies import PolicyEngine
    from gateway.registry import MachineRegistry
    from gateway.server import run_gateway_server

    policy_engine = PolicyEngine()
    registry = MachineRegistry()

    try:
        run_gateway_server(
            auth_manager=auth_manager,
            policy_engine=policy_engine,
            registry=registry,
            port=args.port,
        )
    except KeyboardInterrupt:
        log.info("Gateway server interrupted")
        sys.exit(0)
