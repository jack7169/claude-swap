"""Tests for printer path abbreviation and age formatting edge cases.

Covers Phase 10 fixes:
- F46: ``abbreviate_path`` must not mangle a sibling directory that merely
  shares the home directory's prefix string (e.g. home ``/Users/jo`` and a
  path ``/Users/jones/x`` must stay ``/Users/jones/x``, not ``~nes/x``).
- F49: ``format_age`` must return a sensible placeholder when ``startedAt`` is
  missing/zero/unparseable instead of a nonsensical "decades ago" duration.
"""

from __future__ import annotations

import time

import pytest

from claude_swap import printer


class TestAbbreviatePathSibling:
    """F46: only abbreviate true children of home, not lexical siblings."""

    def test_abbreviates_child(self, monkeypatch):
        monkeypatch.setattr(printer.Path, "home", classmethod(lambda cls: printer.Path("/Users/jo")))
        assert printer.abbreviate_path("/Users/jo/x") == "~/x"

    def test_abbreviates_home_itself(self, monkeypatch):
        monkeypatch.setattr(printer.Path, "home", classmethod(lambda cls: printer.Path("/Users/jo")))
        assert printer.abbreviate_path("/Users/jo") == "~"

    def test_sibling_sharing_prefix_unchanged(self, monkeypatch):
        # /Users/jones starts with the string "/Users/jo" but is NOT under it.
        monkeypatch.setattr(printer.Path, "home", classmethod(lambda cls: printer.Path("/Users/jo")))
        assert printer.abbreviate_path("/Users/jones/x") == "/Users/jones/x"

    def test_sibling_no_separator_unchanged(self, monkeypatch):
        monkeypatch.setattr(printer.Path, "home", classmethod(lambda cls: printer.Path("/Users/jo")))
        assert printer.abbreviate_path("/Users/jones") == "/Users/jones"

    def test_unrelated_path_unchanged(self, monkeypatch):
        monkeypatch.setattr(printer.Path, "home", classmethod(lambda cls: printer.Path("/Users/jo")))
        assert printer.abbreviate_path("/opt/data/bar") == "/opt/data/bar"


class TestFormatAgeMissing:
    """F49: placeholder for missing/zero/unparseable timestamps."""

    PLACEHOLDER = "unknown"

    def test_zero_is_placeholder(self):
        assert printer.format_age(0) == self.PLACEHOLDER

    def test_none_is_placeholder(self):
        assert printer.format_age(None) == self.PLACEHOLDER

    def test_garbage_string_is_placeholder(self):
        assert printer.format_age("not-a-number") == self.PLACEHOLDER

    def test_negative_is_placeholder(self):
        assert printer.format_age(-5) == self.PLACEHOLDER

    def test_valid_recent_timestamp_is_age(self):
        now_ms = int(time.time() * 1000)
        assert printer.format_age(now_ms) == "just now"

    def test_valid_minutes_timestamp(self):
        ms = int((time.time() - 300) * 1000)  # 5 minutes ago
        assert printer.format_age(ms) == "5m ago"
