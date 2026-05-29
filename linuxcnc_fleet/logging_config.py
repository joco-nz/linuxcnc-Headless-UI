"""Shared logging configuration for all LinuxCNC Fleet components."""

from __future__ import annotations

import logging
import logging.handlers
import syslog
import sys


CONSOLE_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
SYSLOG_FORMAT = "%%(levelname)s %%(name)s: %%(message)s"
DATEFMT = "%Y-%m-%d %H:%M:%S"

# Build facility mapping from syslog module constants
_SYSLOG_FACILITY_NAMES = {
    "kern": syslog.LOG_KERN,
    "user": syslog.LOG_USER,
    "mail": syslog.LOG_MAIL,
    "daemon": syslog.LOG_DAEMON,
    "auth": syslog.LOG_AUTH,
    "syslog": syslog.LOG_SYSLOG,
    "lpr": syslog.LOG_LPR,
    "news": syslog.LOG_NEWS,
    "uucp": syslog.LOG_UUCP,
    "cron": syslog.LOG_CRON,
    "authpriv": syslog.LOG_AUTHPRIV,
    "ftp": syslog.LOG_FTP,
    "local0": syslog.LOG_LOCAL0,
    "local1": syslog.LOG_LOCAL1,
    "local2": syslog.LOG_LOCAL2,
    "local3": syslog.LOG_LOCAL3,
    "local4": syslog.LOG_LOCAL4,
    "local5": syslog.LOG_LOCAL5,
    "local6": syslog.LOG_LOCAL6,
    "local7": syslog.LOG_LOCAL7,
}

DEFAULT_SYSLOG_ADDRESS = "/dev/log"


def setup_logging(
    level: int = logging.INFO,
    use_syslog: bool = False,
    syslog_address: str = DEFAULT_SYSLOG_ADDRESS,
    syslog_facility: str = "user",
) -> None:
    """Configure the root logger with console and/or syslog handlers.

    Args:
        level: Logging level (DEBUG, INFO, WARNING, etc.)
        use_syslog: If True, add a SysLogHandler in addition to the console handler.
        syslog_address: Syslog socket path (e.g., "/dev/log" or "/run/systemd/journal/syslog").
        syslog_facility: Syslog facility name (one of kern, user, daemon, local0-local7, etc.)
    """
    formatter = logging.Formatter(CONSOLE_FORMAT, datefmt=DATEFMT)

    root = logging.getLogger()
    root.setLevel(level)

    # Always add console handler
    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setFormatter(formatter)
    root.addHandler(console_handler)

    if use_syslog:
        facility_num = _SYSLOG_FACILITY_NAMES.get(
            syslog_facility,
            syslog.LOG_USER,
        )
        syslog_handler = logging.handlers.SysLogHandler(
            address=syslog_address,
            facility=facility_num,
        )
        syslog_formatter = logging.Formatter(SYSLOG_FORMAT)
        syslog_handler.setFormatter(syslog_formatter)
        root.addHandler(syslog_handler)
