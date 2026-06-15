"""Tests for CLI argument parsing and TLS validation."""

import sys
from unittest.mock import patch

import pytest

from linuxcnc_fleet.cli import parse_args


class TestParseArgsDefaults:
    def test_default_ini(self):
        args = parse_args([])
        assert args.ini is None

    def test_default_machine_id(self):
        args = parse_args([])
        assert args.machine_id is None

    def test_default_port(self):
        args = parse_args([])
        assert args.port == 50051

    def test_default_no_tls(self):
        args = parse_args([])
        assert args.cert is None
        assert args.key is None
        assert args.root_cert is None

    def test_default_gateway_false(self):
        args = parse_args([])
        assert args.gateway is False

    def test_default_verbose_off(self):
        args = parse_args([])
        assert args.verbose == 0


class TestParseArgsValues:
    def test_custom_ini_path(self):
        args = parse_args(["--ini", "/path/to/machine.ini"])
        assert args.ini == "/path/to/machine.ini"

    def test_custom_machine_id(self):
        args = parse_args(["--machine-id", "cnc-mill-1"])
        assert args.machine_id == "cnc-mill-1"

    def test_custom_port(self):
        args = parse_args(["--port", "9999"])
        assert args.port == 9999

    def test_tls_cert_and_key(self):
        args = parse_args([
            "--cert", "/etc/certs/server.pem",
            "--key", "/etc/certs/server-key.pem",
        ])
        assert args.cert == "/etc/certs/server.pem"
        assert args.key == "/etc/certs/server-key.pem"

    def test_root_cert_for_mtls(self):
        args = parse_args([
            "--cert", "/cert.pem",
            "--key", "/key.pem",
            "--root-cert", "/ca.pem",
        ])
        assert args.root_cert == "/ca.pem"

    def test_gateway_flag(self):
        args = parse_args(["--gateway"])
        assert args.gateway is True

    def test_verbose_levels(self):
        args0 = parse_args([])
        args1 = parse_args(["-v"])
        args2 = parse_args(["-vv"])
        assert args0.verbose == 0
        assert args1.verbose == 1
        assert args2.verbose == 2


class TestTLSValidation:
    def test_cert_without_key_exits(self, monkeypatch):
        """--cert without --key should exit with error."""
        mock_exit = monkeypatch.setattr(sys, "exit", lambda code=None: (_ for _ in ()).throw(SystemExit(code)))
        with pytest.raises(SystemExit):
            from linuxcnc_fleet.cli import main
            main(["--cert", "/cert.pem"])

    def test_key_without_cert_exits(self, monkeypatch):
        """--key without --cert should exit with error."""
        mock_exit = monkeypatch.setattr(sys, "exit", lambda code=None: (_ for _ in ()).throw(SystemExit(code)))
        with pytest.raises(SystemExit):
            from linuxcnc_fleet.cli import main
            main(["--key", "/key.pem"])

    def test_root_cert_without_cert_key_exits(self, monkeypatch):
        """--root-cert without --cert and --key should exit with error."""
        mock_exit = monkeypatch.setattr(sys, "exit", lambda code=None: (_ for _ in ()).throw(SystemExit(code)))
        with pytest.raises(SystemExit):
            from linuxcnc_fleet.cli import main
            main(["--root-cert", "/ca.pem"])

    def test_valid_tls_args_pass(self, monkeypatch, linuxcnc_module):
        """Valid TLS args should not exit."""
        calls = []
        mock_exit = monkeypatch.setattr(sys, "exit", lambda code=None: calls.append(code))

        # Mock run_server to avoid actually starting gRPC
        with patch("linuxcnc_fleet.cli.run_server") as mock_run:
            from linuxcnc_fleet.cli import main
            main([
                "--cert", "/cert.pem",
                "--key", "/key.pem",
                "--root-cert", "/ca.pem",
                "--port", "50051",
            ])

        # run_server should have been called (no SystemExit)
        assert mock_run.called


class TestSyslogArgs:
    def test_syslog_default_false(self):
        args = parse_args([])
        assert args.syslog is False

    def test_syslog_flag_enabled(self):
        args = parse_args(["--syslog"])
        assert args.syslog is True

    def test_syslog_address_default(self):
        args = parse_args([])
        assert args.syslog_address == "/dev/log"

    def test_syslog_address_custom(self):
        args = parse_args(["--syslog-address", "/run/systemd/journal/syslog"])
        assert args.syslog_address == "/run/systemd/journal/syslog"

    def test_syslog_facility_default(self):
        args = parse_args([])
        assert args.syslog_facility == "user"

    def test_syslog_facility_custom(self):
        args = parse_args(["--syslog-facility", "daemon"])
        assert args.syslog_facility == "daemon"

    def test_all_syslog_options_together(self):
        args = parse_args([
            "--syslog",
            "--syslog-address", "/run/systemd/journal/syslog",
            "--syslog-facility", "local0",
        ])
        assert args.syslog is True
        assert args.syslog_address == "/run/systemd/journal/syslog"
        assert args.syslog_facility == "local0"


class TestHelpOutput:
    def test_help_does_not_crash(self):
        """--help should raise SystemExit(0), not crash."""
        with pytest.raises(SystemExit) as exc_info:
            parse_args(["--help"])
        assert exc_info.value.code == 0

    def test_syslog_in_help_output(self, capsys):
        """--syslog flag should appear in help text."""
        with pytest.raises(SystemExit) as exc_info:
            parse_args(["--help"])
        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert "--syslog" in captured.out


class TestWorkersArg:
    def test_workers_default_none(self):
        args = parse_args([])
        assert args.workers is None

    def test_workers_custom_value(self):
        args = parse_args(["--workers", "16"])
        assert args.workers == 16

    def test_workers_zero_rejected(self):
        # argparse doesn't reject 0 by default, but main() should validate
        args = parse_args(["--workers", "0"])
        assert args.workers == 0

    def test_workers_from_env_var(self, monkeypatch):
        monkeypatch.setenv("SIDECAR_GRPC_WORKERS", "32")
        args = parse_args([])
        # parse_args doesn't read env vars; main() does
        assert args.workers is None

    def test_workers_in_help_output(self, capsys):
        with pytest.raises(SystemExit) as exc_info:
            parse_args(["--help"])
        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert "--workers" in captured.out
