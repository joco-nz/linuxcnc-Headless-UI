"""Tests for shared logging configuration."""

import logging
import logging.handlers
import syslog
from unittest.mock import MagicMock, patch

from linuxcnc_fleet.logging_config import (
    CONSOLE_FORMAT,
    DATEFMT,
    SYSLOG_FORMAT,
    setup_logging,
)


class TestSyslogFacilities:
    def test_all_facilities_exist(self):
        from linuxcnc_fleet.logging_config import _SYSLOG_FACILITY_NAMES
        assert "user" in _SYSLOG_FACILITY_NAMES
        assert "daemon" in _SYSLOG_FACILITY_NAMES
        assert "local0" in _SYSLOG_FACILITY_NAMES
        assert "local7" in _SYSLOG_FACILITY_NAMES

    def test_facility_values_are_ints(self):
        from linuxcnc_fleet.logging_config import _SYSLOG_FACILITY_NAMES
        for name, value in _SYSLOG_FACILITY_NAMES.items():
            assert isinstance(value, int)

    def test_facility_count(self):
        from linuxcnc_fleet.logging_config import _SYSLOG_FACILITY_NAMES
        assert len(_SYSLOG_FACILITY_NAMES) == 20

    def test_facility_values_match_syslog_module(self):
        from linuxcnc_fleet.logging_config import _SYSLOG_FACILITY_NAMES
        assert _SYSLOG_FACILITY_NAMES["user"] == syslog.LOG_USER
        assert _SYSLOG_FACILITY_NAMES["daemon"] == syslog.LOG_DAEMON
        assert _SYSLOG_FACILITY_NAMES["local0"] == syslog.LOG_LOCAL0


class TestSetupLoggingNoSyslog:
    def setup_method(self):
        root = logging.getLogger()
        for handler in root.handlers[:]:
            root.removeHandler(handler)

    def teardown_method(self):
        root = logging.getLogger()
        for handler in root.handlers[:]:
            root.removeHandler(handler)

    def test_creates_console_handler(self):
        setup_logging(level=logging.INFO, use_syslog=False)
        root = logging.getLogger()
        # Count only StreamHandlers that are NOT LogCaptureHandler (pytest adds these)
        our_handlers = [h for h in root.handlers if type(h).__name__ == "StreamHandler"]
        assert len(our_handlers) == 1

    def test_console_format_correct(self):
        setup_logging(level=logging.INFO, use_syslog=False)
        root = logging.getLogger()
        handler = [h for h in root.handlers if type(h).__name__ == "StreamHandler"][0]
        fmt = handler.formatter._fmt
        assert fmt == CONSOLE_FORMAT

    def test_level_set_correctly(self):
        setup_logging(level=logging.DEBUG)
        assert logging.getLogger().level == logging.DEBUG

    def test_default_level_info(self):
        setup_logging()
        assert logging.getLogger().level == logging.INFO


class TestSetupLoggingWithSyslog:
    def setup_method(self):
        root = logging.getLogger()
        for handler in root.handlers[:]:
            root.removeHandler(handler)

    def teardown_method(self, method):
        root = logging.getLogger()
        for handler in root.handlers[:]:
            # Remove any MagicMock that may have been left by monkeypatch
            if type(handler).__name__ == "MagicMock":
                root.removeHandler(handler)
        for handler in root.handlers[:]:
            root.removeHandler(handler)

    def test_creates_both_handlers(self, monkeypatch):
        mock_handler = MagicMock()
        monkeypatch.setattr(
            "logging.handlers.SysLogHandler", lambda **kwargs: mock_handler
        )
        setup_logging(level=logging.INFO, use_syslog=True)
        root = logging.getLogger()
        # Count only handlers we added (not pytest's LogCaptureHandler)
        our_handlers = [h for h in root.handlers if type(h).__name__ in ("StreamHandler", "SysLogHandler", "MagicMock")]
        assert len(our_handlers) == 2

    def test_syslog_formatter_differs_from_console(self):
        import logging as lg

        captured_formats = []

        original_formatter = lg.Formatter

        class CaptureFormatter(original_formatter):
            def __init__(self, fmt=None, datefmt=None, *args, **kwargs):
                captured_formats.append(fmt)
                super().__init__(fmt, datefmt, *args, **kwargs)

        with patch.object(lg, "Formatter", CaptureFormatter):
            setup_logging(level=logging.INFO, use_syslog=True)

        # Should have 2 Formatter calls: one for console (CONSOLE_FORMAT), one for syslog (SYSLOG_FORMAT)
        assert len(captured_formats) == 2
        assert CONSOLE_FORMAT in captured_formats
        assert SYSLOG_FORMAT in captured_formats

    def test_syslog_facility_passed_correct_value(self, monkeypatch):
        mock_handler = MagicMock()
        original_init = logging.handlers.SysLogHandler.__init__

        def capture_init(self, **kwargs):
            self._capture_kwargs = kwargs
            return original_init(self, **kwargs)

        monkeypatch.setattr("logging.handlers.SysLogHandler.__init__", capture_init)
        setup_logging(use_syslog=True, syslog_facility="daemon")

        root = logging.getLogger()
        for handler in root.handlers:
            if type(handler).__name__ == "SysLogHandler":
                assert hasattr(handler, "_capture_kwargs")
                assert handler._capture_kwargs["facility"] == syslog.LOG_DAEMON

    def test_console_still_present_with_syslog(self, monkeypatch):
        mock_handler = MagicMock()
        monkeypatch.setattr(
            "logging.handlers.SysLogHandler", lambda **kwargs: mock_handler
        )
        setup_logging(level=logging.DEBUG, use_syslog=True)
        root = logging.getLogger()
        console_handlers = [h for h in root.handlers if type(h).__name__ == "StreamHandler"]
        syslog_handlers = [h for h in root.handlers if type(h).__name__ == "MagicMock"]
        assert len(console_handlers) == 1
        assert len(syslog_handlers) == 1

    def test_unknown_facility_defaults_to_user(self, monkeypatch):
        mock_handler = MagicMock()
        original_init = logging.handlers.SysLogHandler.__init__

        def capture_init(self, **kwargs):
            self._capture_kwargs = kwargs
            return original_init(self, **kwargs)

        monkeypatch.setattr("logging.handlers.SysLogHandler.__init__", capture_init)
        setup_logging(use_syslog=True, syslog_facility="nonexistent")

        root = logging.getLogger()
        for handler in root.handlers:
            if type(handler).__name__ == "SysLogHandler":
                assert handler._capture_kwargs["facility"] == syslog.LOG_USER


class TestSetupLoggingLevels:
    def setup_method(self):
        root = logging.getLogger()
        for handler in root.handlers[:]:
            root.removeHandler(handler)

    def teardown_method(self):
        root = logging.getLogger()
        for handler in root.handlers[:]:
            root.removeHandler(handler)

    def test_debug_level(self):
        setup_logging(level=logging.DEBUG)
        assert logging.getLogger().level == logging.DEBUG

    def test_warning_level(self):
        setup_logging(level=logging.WARNING)
        assert logging.getLogger().level == logging.WARNING

    def test_error_level(self):
        setup_logging(level=logging.ERROR)
        assert logging.getLogger().level == logging.ERROR
