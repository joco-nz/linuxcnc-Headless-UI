"""Tests for gateway CLI entry point."""

import argparse
import sys
from unittest.mock import patch, MagicMock

import pytest


def test_parse_args_defaults():
    from gateway.cli import parse_args
    args = parse_args([])
    assert args.port == 50051
    assert args.cert is None
    assert args.key is None
    assert args.root_cert is None
    assert args.jwt_secret is None
    assert args.jwks_url is None
    assert args.issuer is None
    assert args.audience is None
    assert args.verbose is False


def test_parse_args_custom_values():
    from gateway.cli import parse_args
    args = parse_args([
        "--port", "9000",
        "--cert", "/path/to/cert.pem",
        "--key", "/path/to/key.pem",
        "--root-cert", "/path/to/root.pem",
        "--jwt-secret", "my-secret",
        "--issuer", "https://example.com",
        "--audience", "fleet-api",
        "-v",
    ])
    assert args.port == 9000
    assert args.cert == "/path/to/cert.pem"
    assert args.key == "/path/to/key.pem"
    assert args.root_cert == "/path/to/root.pem"
    assert args.jwt_secret == "my-secret"
    assert args.issuer == "https://example.com"
    assert args.audience == "fleet-api"
    assert args.verbose is True


def test_validate_args_cert_without_key():
    from gateway.cli import validate_args, parse_args
    args = parse_args(["--cert", "/path/to/cert.pem"])
    errors = validate_args(args)
    assert len(errors) >= 1
    assert any("--cert and --key must both be provided" in e for e in errors)


def test_validate_args_key_without_cert():
    from gateway.cli import validate_args, parse_args
    args = parse_args(["--key", "/path/to/key.pem"])
    errors = validate_args(args)
    assert len(errors) >= 1
    assert any("--cert and --key must both be provided" in e for e in errors)


def test_validate_args_both_jwt_options():
    from gateway.cli import validate_args, parse_args
    args = parse_args(["--jwt-secret", "secret", "--jwks-url", "https://example.com/.well-known/jwks.json"])
    errors = validate_args(args)
    assert len(errors) >= 1
    assert any("mutually exclusive" in e for e in errors)


def test_validate_args_no_jwt_options():
    from gateway.cli import validate_args, parse_args
    args = parse_args([])
    errors = validate_args(args)
    assert len(errors) >= 1
    assert any("Either --jwt-secret or --jwks-url must be provided" in e for e in errors)


def test_validate_args_valid_jwt_secret():
    from gateway.cli import validate_args, parse_args
    args = parse_args(["--jwt-secret", "my-secret"])
    errors = validate_args(args)
    assert len(errors) == 0


def test_validate_args_valid_jwks_url():
    from gateway.cli import validate_args, parse_args
    args = parse_args(["--jwks-url", "https://example.com/.well-known/jwks.json"])
    errors = validate_args(args)
    assert len(errors) == 0


def test_main_exits_on_errors(capsys):
    from gateway.cli import main
    with pytest.raises(SystemExit) as exc_info:
        main(["--cert", "/path/to/cert.pem"])
    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "Error:" in captured.err


def test_main_exits_on_mutually_exclusive_jwt(capsys):
    from gateway.cli import main
    with pytest.raises(SystemExit) as exc_info:
        main(["--jwt-secret", "secret", "--jwks-url", "https://example.com/jwks"])
    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "mutually exclusive" in captured.err


def test_main_exits_on_missing_jwt_option(capsys):
    from gateway.cli import main
    with pytest.raises(SystemExit) as exc_info:
        main([])
    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "Either --jwt-secret or --jwks-url must be provided" in captured.err


def test_setup_logging_default():
    from gateway.cli import setup_logging
    import logging
    
    # Reset logging configuration before testing
    for handler in logging.root.handlers[:]:
        logging.root.removeHandler(handler)
    
    setup_logging(False)
    assert logging.root.level == logging.INFO


def test_setup_logging_verbose():
    from gateway.cli import setup_logging
    import logging
    
    # Reset logging configuration before testing
    for handler in logging.root.handlers[:]:
        logging.root.removeHandler(handler)
    
    setup_logging(True)
    assert logging.root.level == logging.DEBUG


def test_create_auth_manager_with_secret():
    from gateway.cli import create_auth_manager, parse_args
    args = parse_args(["--jwt-secret", "test-secret"])
    auth_manager = create_auth_manager(args)
    assert auth_manager is not None
    assert auth_manager.secret_key == "test-secret"


def test_create_auth_manager_with_jwks():
    from gateway.cli import create_auth_manager, parse_args
    args = parse_args(["--jwks-url", "https://example.com/.well-known/jwks.json"])
    auth_manager = create_auth_manager(args)
    assert auth_manager is not None
    assert auth_manager.jwks_url == "https://example.com/.well-known/jwks.json"


def test_create_auth_manager_with_issuer():
    from gateway.cli import create_auth_manager, parse_args
    args = parse_args(["--jwt-secret", "secret", "--issuer", "https://keycloak.example.com"])
    auth_manager = create_auth_manager(args)
    assert auth_manager.issuer == "https://keycloak.example.com"


def test_create_auth_manager_with_audience():
    from gateway.cli import create_auth_manager, parse_args
    args = parse_args(["--jwt-secret", "secret", "--audience", "fleet-api"])
    auth_manager = create_auth_manager(args)
    assert auth_manager.audience == "fleet-api"


def test_main_success_with_mock(capsys):
    from gateway.cli import main
    
    mock_registry_instance = MagicMock()
    mock_registry_instance.stop = lambda: None
    
    with patch("gateway.server.run_gateway_server") as mock_run:
        with patch("gateway.registry.MachineRegistry", return_value=mock_registry_instance):
            main(["--jwt-secret", "test-secret"])
            mock_run.assert_called_once()


def test_main_keyboard_interrupt(capsys):
    from gateway.cli import main
    
    def raise_keyboard_interrupt(*args, **kwargs):
        raise KeyboardInterrupt()
    
    mock_registry_instance = MagicMock()
    mock_registry_instance.stop = lambda: None
    
    with patch("gateway.server.run_gateway_server", side_effect=raise_keyboard_interrupt):
        with patch("gateway.registry.MachineRegistry", return_value=mock_registry_instance):
            with pytest.raises(SystemExit) as exc_info:
                main(["--jwt-secret", "test-secret"])
            assert exc_info.value.code == 0


def test_help_output():
    from gateway.cli import parse_args
    with pytest.raises(SystemExit) as exc_info:
        parse_args(["--help"])
    assert exc_info.value.code == 0
