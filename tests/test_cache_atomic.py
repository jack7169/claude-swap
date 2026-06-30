"""Tests for atomic, crash-safe behavior of write_cache (fix 2.4).

write_cache must write via a temp file + os.replace so a crash or concurrent
writer can never leave a truncated/corrupt cache file, and it must never raise
on the usage-display hot path — a write hiccup degrades silently instead of
turning a usage display into a traceback.
"""

from __future__ import annotations

import json
import os
import time

from claude_swap import cache
from claude_swap.cache import read_cache, write_cache


class TestWriteCacheRoundtrip:
    def test_roundtrips_within_ttl(self, tmp_path):
        cache_file = tmp_path / "test.json"
        data = {"accounts": [1, 2, 3], "nested": {"a": True}}

        write_cache(cache_file, data)
        result = read_cache(cache_file, ttl=60)

        assert result == data


class TestWriteCacheAtomic:
    def test_no_tmp_file_remains_after_success(self, tmp_path):
        cache_file = tmp_path / "test.json"
        write_cache(cache_file, {"key": "value"})

        assert cache_file.exists()
        leftovers = [p.name for p in tmp_path.iterdir() if p != cache_file]
        assert leftovers == [], f"unexpected leftover files: {leftovers}"

    def test_failed_replace_leaves_original_intact_and_does_not_raise(
        self, tmp_path, monkeypatch
    ):
        cache_file = tmp_path / "test.json"
        # Seed an existing, valid cache entry.
        write_cache(cache_file, {"original": True})
        original_bytes = cache_file.read_bytes()

        def boom(*args, **kwargs):
            raise OSError("simulated mid-write failure")

        monkeypatch.setattr(os, "replace", boom)

        # Must not raise even though the rename fails mid-write.
        write_cache(cache_file, {"replacement": True})

        # The original file must be untouched (not truncated/corrupted).
        assert cache_file.read_bytes() == original_bytes
        assert read_cache(cache_file, ttl=60) == {"original": True}

    def test_failed_replace_leaves_no_tmp_file(self, tmp_path, monkeypatch):
        cache_file = tmp_path / "test.json"
        write_cache(cache_file, {"original": True})

        def boom(*args, **kwargs):
            raise OSError("simulated mid-write failure")

        monkeypatch.setattr(os, "replace", boom)
        write_cache(cache_file, {"replacement": True})

        leftovers = [p.name for p in tmp_path.iterdir() if p != cache_file]
        assert leftovers == [], f"unexpected leftover files: {leftovers}"


class TestWriteCacheNeverRaises:
    def test_unwritable_mkstemp_path_does_not_raise(self, tmp_path, monkeypatch):
        cache_file = tmp_path / "test.json"

        def boom(*args, **kwargs):
            raise OSError("read-only filesystem")

        # Even if creating the temp file fails, the hot path must stay quiet.
        monkeypatch.setattr(cache.tempfile, "mkstemp", boom)
        write_cache(cache_file, {"key": "value"})

    def test_unserializable_data_does_not_raise(self, tmp_path):
        cache_file = tmp_path / "test.json"

        # An object json cannot serialize must not propagate a TypeError.
        write_cache(cache_file, {"bad": object()})

        # Nothing usable was written, but no traceback escaped.
        assert read_cache(cache_file, ttl=60, default="miss") == "miss"

    def test_mkdir_failure_does_not_raise(self, tmp_path, monkeypatch):
        cache_file = tmp_path / "sub" / "test.json"

        import pathlib

        def boom(self, *args, **kwargs):
            raise OSError("cannot create directory")

        monkeypatch.setattr(pathlib.Path, "mkdir", boom)
        write_cache(cache_file, {"key": "value"})


class TestReadCacheUnchanged:
    def test_still_tolerates_corrupt_file(self, tmp_path):
        cache_file = tmp_path / "test.json"
        cache_file.write_text("not valid json{{{")
        assert read_cache(cache_file, ttl=60, default="miss") == "miss"

    def test_still_returns_data_within_ttl(self, tmp_path):
        cache_file = tmp_path / "test.json"
        cache_file.write_text(
            json.dumps({"timestamp": time.time(), "data": {"k": "v"}})
        )
        assert read_cache(cache_file, ttl=60) == {"k": "v"}
