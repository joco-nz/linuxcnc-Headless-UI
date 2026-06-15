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
    args = parse_args(["--jwt-secret", "test-secret-key-for-testing-32bytes!"])
    auth_manager = create_auth_manager(args)
    assert auth_manager is not None
    assert auth_manager.secret_key == "test-secret-key-for-testing-32bytes!"


def test_create_auth_manager_with_jwks():
    from gateway.cli import create_auth_manager, parse_args
    args = parse_args(["--jwks-url", "https://example.com/.well-known/jwks.json"])
    auth_manager = create_auth_manager(args)
    assert auth_manager is not None
    assert auth_manager.jwks_url == "https://example.com/.well-known/jwks.json"


def test_create_auth_manager_with_issuer():
    from gateway.cli import create_auth_manager, parse_args
    args = parse_args(["--jwt-secret", "secret-key-for-testing-32bytes!!!", "--issuer", "https://keycloak.example.com"])
    auth_manager = create_auth_manager(args)
    assert auth_manager.issuer == "https://keycloak.example.com"


def test_create_auth_manager_with_audience():
    from gateway.cli import create_auth_manager, parse_args
    args = parse_args(["--jwt-secret", "secret-key-for-testing-32bytes!!!", "--audience", "fleet-api"])
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


class TestSyslogArgs:
    def test_syslog_default_false(self):
        from gateway.cli import parse_args
        args = parse_args([])
        assert args.syslog is False

    def test_syslog_flag_enabled(self):
        from gateway.cli import parse_args
        args = parse_args(["--syslog"])
        assert args.syslog is True

    def test_syslog_address_default(self):
        from gateway.cli import parse_args
        args = parse_args([])
        assert args.syslog_address == "/dev/log"

    def test_syslog_address_custom(self):
        from gateway.cli import parse_args
        args = parse_args(["--syslog-address", "/run/systemd/journal/syslog"])
        assert args.syslog_address == "/run/systemd/journal/syslog"

    def test_syslog_facility_default(self):
        from gateway.cli import parse_args
        args = parse_args([])
        assert args.syslog_facility == "user"

    def test_syslog_facility_custom(self):
        from gateway.cli import parse_args
        args = parse_args(["--syslog-facility", "daemon"])
        assert args.syslog_facility == "daemon"

    def test_all_syslog_options_together(self):
        from gateway.cli import parse_args
        args = parse_args([
            "--syslog",
            "--syslog-address", "/run/systemd/journal/syslog",
            "--syslog-facility", "local0",
        ])
        assert args.syslog is True
        assert args.syslog_address == "/run/systemd/journal/syslog"
        assert args.syslog_facility == "local0"


def test_help_output():
    from gateway.cli import parse_args
    with pytest.raises(SystemExit) as exc_info:
        parse_args(["--help"])
    assert exc_info.value.code == 0


def test_syslog_in_help(capsys):
    from gateway.cli import parse_args
    with pytest.raises(SystemExit) as exc_info:
        parse_args(["--help"])
    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert "--syslog" in captured.out


def test_setup_logging_with_syslog(monkeypatch):
    import logging
    from gateway.cli import setup_logging

    mock_handler = MagicMock()
    monkeypatch.setattr("logging.handlers.SysLogHandler", lambda **kwargs: mock_handler)

    for handler in logging.root.handlers[:]:
        logging.root.removeHandler(handler)

    setup_logging(verbose=False, use_syslog=True)

    root = logging.getLogger()
    console_handlers = [h for h in root.handlers if type(h).__name__ == "StreamHandler"]
    syslog_handlers = [h for h in root.handlers if type(h).__name__ == "MagicMock" or type(h).__name__ == "SysLogHandler"]
    assert len(console_handlers) == 1
    assert len(syslog_handlers) == 1


def test_setup_logging_syslog_facility(monkeypatch):
    import logging
    from gateway.cli import setup_logging

    mock_handler = MagicMock()
    monkeypatch.setattr("logging.handlers.SysLogHandler", lambda **kwargs: mock_handler)

    for handler in logging.root.handlers[:]:
        logging.root.removeHandler(handler)

    setup_logging(verbose=False, use_syslog=True, syslog_facility="daemon")


def test_setup_logging_syslog_address(monkeypatch):
    import logging
    from gateway.cli import setup_logging

    mock_handler = MagicMock()
    monkeypatch.setattr("logging.handlers.SysLogHandler", lambda **kwargs: mock_handler)

    for handler in logging.root.handlers[:]:
        logging.root.removeHandler(handler)

    setup_logging(verbose=False, use_syslog=True, syslog_address="/run/systemd/journal/syslog")


class TestHttpTokenArgs:
    def test_http_port_default_none(self):
        from gateway.cli import parse_args
        args = parse_args(["--jwt-secret", "test"])
        assert args.http_port is None

    def test_http_port_custom(self):
        from gateway.cli import parse_args
        args = parse_args(["--jwt-secret", "test", "--http-port", "50053"])
        assert args.http_port == 50053

    def test_allowed_roles_default(self):
        from gateway.cli import parse_args
        args = parse_args(["--jwt-secret", "test"])
        assert args.allowed_roles == "viewer,operator"

    def test_allowed_roles_custom(self):
        from gateway.cli import parse_args
        args = parse_args(["--jwt-secret", "test", "--allowed-roles", "viewer,operator,programmer"])
        assert args.allowed_roles == "viewer,operator,programmer"

    def test_token_ttl_default(self):
        from gateway.cli import parse_args
        args = parse_args(["--jwt-secret", "test"])
        assert args.token_ttl == 900

    def test_token_ttl_custom(self):
        from gateway.cli import parse_args
        args = parse_args(["--jwt-secret", "test", "--token-ttl", "1800"])
        assert args.token_ttl == 1800

    def test_allow_admin_token_default_false(self):
        from gateway.cli import parse_args
        args = parse_args(["--jwt-secret", "test"])
        assert args.allow_admin_token is False

    def test_allow_admin_token_enabled(self):
        from gateway.cli import parse_args
        args = parse_args(["--jwt-secret", "test", "--allow-admin-token"])
        assert args.allow_admin_token is True

    def test_allowed_subjects_default(self):
        from gateway.cli import parse_args
        args = parse_args(["--jwt-secret", "test"])
        assert args.allowed_subjects == "fleet-ui"

    def test_allowed_subjects_custom(self):
        from gateway.cli import parse_args
        args = parse_args(["--jwt-secret", "test", "--allowed-subjects", "fleet-ui,fleet-app"])
        assert args.allowed_subjects == "fleet-ui,fleet-app"

    def test_allowed_ips_default(self):
        from gateway.cli import parse_args
        args = parse_args(["--jwt-secret", "test"])
        assert args.allowed_ips == "127.0.0.1,::1"

    def test_allowed_ips_custom(self):
        from gateway.cli import parse_args
        args = parse_args(["--jwt-secret", "test", "--allowed-ips", "192.168.1.0/24"])
        assert args.allowed_ips == "192.168.1.0/24"

    def test_permissive_default_false(self):
        from gateway.cli import parse_args
        args = parse_args(["--jwt-secret", "test"])
        assert args.permissive is False

    def test_permissive_enabled(self):
        from gateway.cli import parse_args
        args = parse_args(["--jwt-secret", "test", "--permissive"])
        assert args.permissive is True


class TestHttpPortValidation:
    def test_http_port_negative_rejected(self, capsys):
        from gateway.cli import main
        with pytest.raises(SystemExit) as exc_info:
            main(["--jwt-secret", "test", "--http-port", "-1"])
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "--http-port must be a positive integer" in captured.err

    def test_http_port_zero_rejected(self, capsys):
        from gateway.cli import main
        with pytest.raises(SystemExit) as exc_info:
            main(["--jwt-secret", "test", "--http-port", "0"])
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "--http-port must be a positive integer" in captured.err

    def test_http_port_valid(self):
        from gateway.cli import main
        mock_registry_instance = MagicMock()
        mock_registry_instance.stop = lambda: None
        with patch("gateway.server.run_gateway_server") as mock_run:
            with patch("gateway.registry.MachineRegistry", return_value=mock_registry_instance):
                main(["--jwt-secret", "test", "--http-port", "50053"])
                call_kwargs = mock_run.call_args[1]
                assert call_kwargs["http_port"] == 50053


class TestHttpPortPassedToServer:
    def test_all_http_params_passed(self):
        from gateway.cli import main
        mock_registry_instance = MagicMock()
        mock_registry_instance.stop = lambda: None
        with patch("gateway.server.run_gateway_server") as mock_run:
            with patch("gateway.registry.MachineRegistry", return_value=mock_registry_instance):
                main([
                    "--jwt-secret", "test",
                    "--http-port", "50053",
                    "--allowed-roles", "viewer,operator,admin",
                    "--token-ttl", "1800",
                    "--allow-admin-token",
                    "--allowed-subjects", "fleet-ui,app1",
                    "--allowed-ips", "127.0.0.1,192.168.1.1",
                    "--permissive",
                ])
                call_kwargs = mock_run.call_args[1]
                assert call_kwargs["http_port"] == 50053
                assert call_kwargs["allowed_roles"] == ["viewer", "operator", "admin"]
                assert call_kwargs["token_ttl"] == 1800
                assert call_kwargs["allow_admin_token"] is True
                assert call_kwargs["allowed_subjects"] == ["fleet-ui", "app1"]
                assert call_kwargs["allowed_ips"] == ["127.0.0.1", "192.168.1.1"]
                assert call_kwargs["permissive"] is True
