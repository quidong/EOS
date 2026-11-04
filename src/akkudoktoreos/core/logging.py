"""Utility for configuring Loguru loggers."""

import json
import logging as pylogging
import os
import re
import sys
from pathlib import Path
from types import FrameType
from typing import Any, Iterator, List, Optional, cast

import pendulum
from loguru import logger

from akkudoktoreos.core.logabc import LOGGING_LEVELS


class InterceptHandler(pylogging.Handler):
    """A logging handler that redirects standard Python logging messages to Loguru.

    This handler ensures consistency between the `logging` module and Loguru by intercepting
    logs sent to the standard logging system and re-emitting them through Loguru with proper
    formatting and context (including exception info and call depth).

    Attributes:
        loglevel_mapping (dict): Mapping from standard logging levels to Loguru level names.
    """

    loglevel_mapping: dict[int, str] = {
        50: "CRITICAL",
        40: "ERROR",
        30: "WARNING",
        20: "INFO",
        10: "DEBUG",
        5: "TRACE",
        0: "NOTSET",
    }

    def emit(self, record: pylogging.LogRecord) -> None:
        """Emits a logging record by forwarding it to Loguru with preserved metadata.

        Args:
            record (logging.LogRecord): A record object containing log message and metadata.
        """
        # Skip DEBUG logs from matplotlib - very noisy
        if record.name.startswith("matplotlib") and record.levelno <= pylogging.DEBUG:
            return

        try:
            level = logger.level(record.levelname).name
        except AttributeError:
            level = self.loglevel_mapping.get(record.levelno, "INFO")

        frame: Optional[FrameType] = pylogging.currentframe()
        depth: int = 2
        while frame and frame.f_code.co_filename == pylogging.__file__:
            frame = frame.f_back
            depth += 1

        log = logger.bind(request_id="app")
        log.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())


console_handler_id = None
file_handler_id = None


def logging_track_config(config_eos: Any, path: str, old_value: Any, value: Any) -> None:
    """Track logging config changes."""
    global console_handler_id, file_handler_id

    if not path.startswith("logging"):
        raise ValueError(f"Logging shall not track '{path}'")

    if not config_eos.logging.console_level:
        # No value given - check environment value - may also be None
        config_eos.logging.console_level = os.getenv("EOS_LOGGING__LEVEL")
    if not config_eos.logging.file_level:
        # No value given - check environment value - may also be None
        config_eos.logging.file_level = os.getenv("EOS_LOGGING__LEVEL")

    # Remove handlers
    if console_handler_id:
        try:
            logger.remove(console_handler_id)
        except Exception as e:
            logger.debug("Exception on logger.remove: {}", e, exc_info=True)
        console_handler_id = None
    if file_handler_id:
        try:
            logger.remove(file_handler_id)
        except Exception as e:
            logger.debug("Exception on logger.remove: {}", e, exc_info=True)
        file_handler_id = None

    # Create handlers with new configuration
    # Always add console handler
    if config_eos.logging.console_level not in LOGGING_LEVELS:
        logger.error(
            f"Invalid console log level '{config_eos.logging.console_level} - forced to INFO'."
        )
        config_eos.logging.console_level = "INFO"

    console_handler_id = logger.add(
        sys.stderr,
        enqueue=True,
        backtrace=True,
        level=config_eos.logging.console_level,
        # format=_console_format
    )

    # Add file handler
    if config_eos.logging.file_level and config_eos.logging.file_path:
        if config_eos.logging.file_level not in LOGGING_LEVELS:
            logger.error(
                f"Invalid file log level '{config_eos.logging.console_level}' - forced to INFO."
            )
            config_eos.logging.file_level = "INFO"

        file_handler_id = logger.add(
            sink=config_eos.logging.file_path,
            rotation="100 MB",
            retention="3 days",
            enqueue=True,
            backtrace=True,
            level=config_eos.logging.file_level,
            serialize=True,  # JSON dict formatting
            # format=_file_format
        )

    # Redirect standard logging to Loguru
    pylogging.basicConfig(handlers=[InterceptHandler()], level=0)
    # Redirect uvicorn and fastapi logging to Loguru
    pylogging.getLogger("uvicorn.access").handlers = [InterceptHandler()]
    for pylogger_name in ["uvicorn", "uvicorn.error", "fastapi"]:
        pylogger = pylogging.getLogger(pylogger_name)
        pylogger.handlers = [InterceptHandler()]
        pylogger.propagate = False

    logger.info(
        f"Logger reconfigured - console: {config_eos.logging.console_level}, file: {config_eos.logging.file_level}."
    )


def normalize_log_entry(log: dict) -> dict:
    """One log line as ``{"time", "level", "message", ...}``.

    RL29 fork: Loguru's ``serialize=True`` writes ``{"text": ..., "record": {...}}``;
    time, level and message are inside ``record``. The filters of ``read_file_log``
    and its callers expect them at the top level, so serialized records are
    flattened. Entries that already have the flat shape pass unchanged.
    """
    record = log.get("record")
    if not isinstance(record, dict):
        return log
    time = record.get("time")
    if isinstance(time, dict):
        time = time.get("repr")
    if isinstance(time, str) and len(time) > 10 and time[10] == " ":
        time = time[:10] + "T" + time[11:]  # ISO 8601, parseable by pendulum
    level = record.get("level")
    if isinstance(level, dict):
        level = {"name": level.get("name"), "no": level.get("no")}
    else:
        level = {"name": level, "no": None}
    exception = record.get("exception")
    return {
        "time": time,
        "level": level,
        "message": record.get("message", ""),
        "name": record.get("name"),
        "function": record.get("function"),
        "line": record.get("line"),
        "exception": exception,
        "text": log.get("text"),
    }


def _lines_reversed(log_path: Path, chunk_size: int = 64 * 1024) -> Iterator[str]:
    """Lines of a file from the last to the first, read in chunks."""
    with log_path.open("rb") as f:
        f.seek(0, 2)
        pointer = f.tell()
        rest = b""
        while pointer > 0:
            step = min(chunk_size, pointer)
            pointer -= step
            f.seek(pointer)
            block = f.read(step) + rest
            parts = block.split(b"\n")
            rest = parts[0]
            for part in reversed(parts[1:]):
                if part.strip():
                    yield part.decode("utf-8", errors="ignore")
        if rest.strip():
            yield rest.decode("utf-8", errors="ignore")


def read_file_log(
    log_path: Path,
    limit: int = 100,
    level: Optional[str] = None,
    contains: Optional[str] = None,
    regex: Optional[str] = None,
    from_time: Optional[str] = None,
    to_time: Optional[str] = None,
    tail: bool = False,
    max_scan_lines: int = 500_000,
) -> List[dict]:
    """Read and filter structured log entries from a JSON-formatted log file.

    Args:
        log_path (Path): Path to the JSON-formatted log file.
        limit (int, optional): Maximum number of log entries to return. Defaults to 100.
        level (Optional[str], optional): Filter logs by log level (e.g., "INFO", "ERROR"). Defaults to None.
        contains (Optional[str], optional): Filter logs that contain this substring in their message. Case-insensitive. Defaults to None.
        regex (Optional[str], optional): Filter logs whose message matches this regular expression. Defaults to None.
        from_time (Optional[str], optional): ISO 8601 datetime string to filter logs not earlier than this time. Defaults to None.
        to_time (Optional[str], optional): ISO 8601 datetime string to filter logs not later than this time. Defaults to None.
        tail (bool, optional): If True, return the last `limit` matching entries (like `tail -n`
            after a filter), oldest first. Defaults to False.
        max_scan_lines (int, optional): In tail mode, stop after this many lines. Defaults to 500000.

    Returns:
        List[dict]: A list of filtered log entries as dictionaries.

    Raises:
        FileNotFoundError: If the log file does not exist.
        ValueError: If the datetime strings are invalid or improperly formatted.
        Exception: For other unforeseen I/O or parsing errors.
    """
    if not log_path.exists():
        raise FileNotFoundError("Log file not found")

    try:
        from_dt = cast(pendulum.DateTime, pendulum.parse(from_time)) if from_time else None
        to_dt = cast(pendulum.DateTime, pendulum.parse(to_time)) if to_time else None
    except Exception as e:
        raise ValueError(f"Invalid date/time format: {e}")

    regex_pattern = re.compile(regex) if regex else None

    def matches_filters(log: dict) -> bool:
        if level and log.get("level", {}).get("name") != level.upper():
            return False
        if contains and contains.lower() not in log.get("message", "").lower():
            return False
        if regex_pattern and not regex_pattern.search(log.get("message", "")):
            return False
        if from_dt or to_dt:
            try:
                log_time = cast(pendulum.DateTime, pendulum.parse(log["time"]))
            except Exception:
                return False
            if from_dt and log_time < from_dt:
                return False
            if to_dt and log_time > to_dt:
                return False
        return True

    def parse(line: str) -> Optional[dict]:
        if not line.strip():
            return None
        try:
            log = json.loads(line)
        except json.JSONDecodeError:
            return None
        return normalize_log_entry(log) if isinstance(log, dict) else None

    matched_logs: list[dict] = []
    if tail:
        # RL29 fork: walk back from the end until `limit` entries match (the
        # upstream version filtered only the last 5 * limit lines, so a filter
        # for a rare message found nothing). Older than `from_time` ends the walk.
        for scanned, line in enumerate(_lines_reversed(log_path)):
            if scanned >= max_scan_lines:
                break
            log = parse(line)
            if log is None:
                continue
            if from_dt is not None:
                try:
                    if cast(pendulum.DateTime, pendulum.parse(log["time"])) < from_dt:
                        break
                except Exception:  # noqa: S110 - unparseable time: let the filter decide
                    pass
            if matches_filters(log):
                matched_logs.append(log)
                if len(matched_logs) >= limit:
                    break
        matched_logs.reverse()
        return matched_logs

    with log_path.open("r", encoding="utf-8", newline=None) as f_txt:
        for line in f_txt:
            log = parse(line)
            if log is not None and matches_filters(log):
                matched_logs.append(log)
                if len(matched_logs) >= limit:
                    break

    return matched_logs
