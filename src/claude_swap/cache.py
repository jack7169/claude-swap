"""Simple file-based cache utilities for claude-swap."""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from pathlib import Path

from claude_swap.paths import get_backup_root

CACHE_DIR = get_backup_root() / "cache"

MISSING = object()

logger = logging.getLogger(__name__)


def read_cache(path: Path, ttl: float, default=MISSING):
    """Read cached JSON data if the file exists and is within TTL.

    Returns the stored 'data' value, or *default* if missing/expired/invalid.
    When *default* is not provided, returns the ``MISSING`` sentinel so
    callers can distinguish "no cache" from a cached ``None`` value.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if time.time() - raw["timestamp"] < ttl:
            return raw["data"]
    except (
        OSError,
        json.JSONDecodeError,
        UnicodeDecodeError,
        KeyError,
        TypeError,
    ):
        pass
    return default


def write_cache(path: Path, data) -> None:
    """Atomically write data to a cache file with a timestamp.

    Writes to a temp file in the target directory and ``os.replace``\\s it onto
    *path*, so a crash or concurrent writer can never observe a truncated/corrupt
    cache file (mirrors ``credentials.py._write_active_credentials_file``). This is
    on the usage-display hot path, so any failure — a serialization error or an
    OSError from the temp-file write/rename — is logged and swallowed rather than
    propagated: a cache write hiccup must never turn a usage display into a
    traceback. ``read_cache`` already tolerates a missing/stale entry.
    """
    try:
        encoded = json.dumps({"timestamp": time.time(), "data": data}).encode(
            "utf-8"
        )
    except (TypeError, ValueError) as e:
        logger.warning(f"Failed to serialize cache data for {path}: {e}")
        return

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            os.write(fd, encoded)
            os.close(fd)
            fd = -1
            os.replace(tmp_path, str(path))
        except BaseException:
            if fd >= 0:
                os.close(fd)
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
    except OSError as e:
        # Degradation path: a cache write failure must not surface to the caller.
        logger.warning(f"Failed to write cache file {path}: {e}")
