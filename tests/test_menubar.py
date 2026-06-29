"""Tests for the menu bar module.

These tests never import or run rumps/AppKit. They exercise the pure helpers
(settings store, title/label formatting, plist rendering) only.
"""

from __future__ import annotations

import json
import plistlib
from pathlib import Path

import pytest

from claude_swap import menubar


def test_settings_defaults_when_file_missing(tmp_path: Path):
    s = menubar.MenuBarSettings.load(tmp_path / "nope.json")
    assert s.show_account_name is True
    assert s.title_pct == "both"
    assert s.refresh_interval == 60


def test_settings_round_trip(tmp_path: Path):
    path = tmp_path / "menubar_settings.json"
    original = menubar.MenuBarSettings(
        show_account_name=False,
        title_pct="5h",
        refresh_interval=300,
    )
    original.save(path)
    loaded = menubar.MenuBarSettings.load(path)
    assert loaded == original


def test_settings_corrupt_file_falls_back_to_defaults(tmp_path: Path):
    path = tmp_path / "menubar_settings.json"
    path.write_text("{ this is not json", encoding="utf-8")
    s = menubar.MenuBarSettings.load(path)
    assert s == menubar.MenuBarSettings()


def test_settings_ignores_unknown_and_bad_types(tmp_path: Path):
    path = tmp_path / "menubar_settings.json"
    path.write_text(
        json.dumps(
            {"refresh_interval": "fast", "bogus": 1, "show_account_name": False}
        ),
        encoding="utf-8",
    )
    s = menubar.MenuBarSettings.load(path)
    # bad-typed refresh_interval falls back to default; valid bool is kept
    assert s.refresh_interval == 60
    assert s.show_account_name is False


_USAGE = {
    "five_hour": {"pct": 42.0},
    "seven_day": {"pct": 18.0},
    "spend": {"pct": 30.0, "used": 3.0, "limit": 10.0},
}


def test_tightest_pct_uses_max_window():
    assert menubar.tightest_pct(_USAGE) == 42.0


def test_tightest_pct_none_for_non_dict_or_empty():
    assert menubar.tightest_pct("no credentials") is None
    assert menubar.tightest_pct(None) is None
    assert menubar.tightest_pct({"spend": {"pct": 90.0}}) is None  # no 5h/7d


def test_usage_summary_dict():
    assert menubar.usage_summary(_USAGE) == "5h 42% · 7d 18% · $ 30%"


def test_usage_summary_partial_windows():
    assert menubar.usage_summary({"five_hour": {"pct": 5.0}}) == "5h 5%"


def test_usage_summary_string_sentinel_passthrough():
    assert menubar.usage_summary("no credentials") == "no credentials"


def test_usage_summary_none():
    assert menubar.usage_summary(None) == "usage unavailable"


def test_format_account_label():
    label = menubar.format_account_label(2, "loc@papaya.asia", _USAGE)
    assert label == "2  loc@papaya.asia  5h 42% · 7d 18% · $ 30%"


def test_format_title_name_and_5h():
    s = menubar.MenuBarSettings(show_account_name=True, title_pct="5h")
    assert menubar.format_title("loc@papaya.asia", _USAGE, s) == "⇄ loc · 42%"


def test_format_title_name_only_when_pct_off():
    s = menubar.MenuBarSettings(show_account_name=True, title_pct="off")
    assert menubar.format_title("loc@papaya.asia", _USAGE, s) == "⇄ loc"


def test_format_title_5h_only():
    s = menubar.MenuBarSettings(show_account_name=False, title_pct="5h")
    assert menubar.format_title("loc@papaya.asia", _USAGE, s) == "⇄ 42%"


def test_format_title_7d_only():
    s = menubar.MenuBarSettings(show_account_name=False, title_pct="7d")
    assert menubar.format_title("loc@papaya.asia", _USAGE, s) == "⇄ 18%"


def test_format_title_both_windows():
    s = menubar.MenuBarSettings(show_account_name=False, title_pct="both")
    assert menubar.format_title("loc@papaya.asia", _USAGE, s) == "⇄ 42% · 18%"


def test_format_title_both_windows_with_name():
    s = menubar.MenuBarSettings(show_account_name=True, title_pct="both")
    assert menubar.format_title("loc@papaya.asia", _USAGE, s) == "⇄ loc · 42% · 18%"


def test_format_title_icon_only_when_off():
    s = menubar.MenuBarSettings(show_account_name=False, title_pct="off")
    assert menubar.format_title("loc@papaya.asia", _USAGE, s) == "⇄"


def test_format_title_icon_only_when_no_active_account():
    s = menubar.MenuBarSettings(show_account_name=True, title_pct="both")
    assert menubar.format_title(None, None, s) == "⇄"


def test_format_title_truncates_long_local_part():
    s = menubar.MenuBarSettings(show_account_name=True, title_pct="off")
    title = menubar.format_title("averylonglocalpart@example.com", None, s)
    assert title == "⇄ averylonglo*"  # 12 chars: 11 letters + asterisk marker


def test_format_title_both_drops_unavailable_windows():
    s = menubar.MenuBarSettings(show_account_name=False, title_pct="both")
    assert menubar.format_title("loc@x.com", "no credentials", s) == "⇄"


def test_format_title_both_keeps_available_window():
    s = menubar.MenuBarSettings(show_account_name=False, title_pct="both")
    # only 5h present -> 7d dropped, no trailing separator
    assert menubar.format_title("loc@x.com", {"five_hour": {"pct": 9.0}}, s) == "⇄ 9%"


def test_settings_auto_switch_defaults(tmp_path: Path):
    s = menubar.MenuBarSettings.load(tmp_path / "missing.json")
    assert s.auto_switch_enabled is False
    assert s.auto_switch_threshold == 95
    assert s.auto_switch_cooldown == 600
    assert s.auto_switch_interval == 0


def test_settings_auto_switch_round_trip(tmp_path: Path):
    path = tmp_path / "settings.json"
    orig = menubar.MenuBarSettings(
        auto_switch_enabled=True,
        auto_switch_threshold=80,
        auto_switch_cooldown=300,
        auto_switch_interval=180,
    )
    orig.save(path)
    assert menubar.MenuBarSettings.load(path) == orig


def test_state_defaults(tmp_path: Path):
    st = menubar.MenuBarState.load(tmp_path / "missing.json")
    assert st.last_switch_at == 0.0
    assert st.last_noswap_notify_at == 0.0


def test_state_round_trip(tmp_path: Path):
    path = tmp_path / "state.json"
    st = menubar.MenuBarState(last_switch_at=1750000000.5, last_noswap_notify_at=1750000123.0)
    st.save(path)
    assert menubar.MenuBarState.load(path) == st


def test_state_corrupt_falls_back(tmp_path: Path):
    path = tmp_path / "state.json"
    path.write_text("not json {", encoding="utf-8")
    assert menubar.MenuBarState.load(path) == menubar.MenuBarState()


def test_state_accepts_int_timestamps(tmp_path: Path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"last_switch_at": 1750000000, "last_noswap_notify_at": 0}),
                    encoding="utf-8")
    st = menubar.MenuBarState.load(path)
    assert st.last_switch_at == 1750000000.0
    assert isinstance(st.last_switch_at, float)


def _acct(num, pct5, pct7, active=False):
    return (num, f"a{num}@x.com", active,
            {"five_hour": {"pct": pct5}, "seven_day": {"pct": pct7}})


def test_decide_active_has_headroom():
    accts = [_acct(1, 50, 10, active=True), _acct(2, 5, 5)]
    assert menubar.decide_auto_switch(accts, 95) == ("none", None)


def test_decide_active_over_5h_picks_best():
    accts = [_acct(1, 96, 10, active=True), _acct(2, 40, 30), _acct(3, 10, 80)]
    assert menubar.decide_auto_switch(accts, 95) == ("switch", 2)


def test_decide_active_over_7d():
    accts = [_acct(1, 10, 97, active=True), _acct(2, 50, 20)]
    assert menubar.decide_auto_switch(accts, 95) == ("switch", 2)


def test_decide_skips_saturated_candidates():
    accts = [_acct(1, 99, 10, active=True), _acct(2, 96, 5), _acct(3, 97, 99)]
    assert menubar.decide_auto_switch(accts, 95) == ("no_candidate", None)


def test_decide_tie_break_by_7d_then_5h():
    # both candidates worst=40; lower 7d wins -> acct 2 (7d 30 < 7d 40)
    accts = [_acct(1, 99, 10, active=True), _acct(2, 40, 30), _acct(3, 20, 40)]
    assert menubar.decide_auto_switch(accts, 95) == ("switch", 2)


def test_decide_tie_break_by_5h_when_worst_and_7d_equal():
    # Both candidates: worst=40, 7d=40; differ only on 5h -> lower 5h wins.
    accts = [_acct(1, 99, 10, active=True), _acct(2, 30, 40), _acct(3, 20, 40)]
    # acct2 key=(40,40,30), acct3 key=(40,40,20) -> acct3 (lower 5h)
    assert menubar.decide_auto_switch(accts, 95) == ("switch", 3)


def test_decide_unknown_active():
    accts = [(1, "a@x", True, "no credentials"), _acct(2, 5, 5)]
    assert menubar.decide_auto_switch(accts, 95) == ("unknown_active", None)


def test_decide_active_missing_one_window_is_unknown():
    accts = [(1, "a@x", True, {"five_hour": {"pct": 99}}), _acct(2, 5, 5)]
    assert menubar.decide_auto_switch(accts, 95) == ("unknown_active", None)


def test_decide_excludes_unknown_candidate():
    accts = [_acct(1, 99, 10, active=True), (2, "b@x", False, None), _acct(3, 50, 50)]
    assert menubar.decide_auto_switch(accts, 95) == ("switch", 3)


def test_decide_no_other_accounts():
    accts = [_acct(1, 99, 10, active=True)]
    assert menubar.decide_auto_switch(accts, 95) == ("no_candidate", None)


def test_decide_no_active_account():
    accts = [_acct(1, 50, 10), _acct(2, 5, 5)]
    assert menubar.decide_auto_switch(accts, 95) == ("none", None)


def test_plan_switch_outside_cooldown():
    st = menubar.MenuBarState(last_switch_at=0.0)
    s = menubar.MenuBarSettings(auto_switch_cooldown=600)
    assert menubar.plan_auto_switch(("switch", 2), st, s, 1000.0) == ("switch", 2)


def test_plan_switch_within_cooldown():
    st = menubar.MenuBarState(last_switch_at=900.0)
    s = menubar.MenuBarSettings(auto_switch_cooldown=600)
    assert menubar.plan_auto_switch(("switch", 2), st, s, 1000.0) == ("cooldown", None)


def test_plan_no_candidate_past_rate_limit():
    st = menubar.MenuBarState(last_noswap_notify_at=0.0)
    s = menubar.MenuBarSettings()
    assert menubar.plan_auto_switch(("no_candidate", None), st, s, 5000.0) == ("notify_noswap", None)


def test_plan_no_candidate_within_rate_limit():
    st = menubar.MenuBarState(last_noswap_notify_at=4000.0)
    s = menubar.MenuBarSettings()
    assert menubar.plan_auto_switch(("no_candidate", None), st, s, 5000.0) == ("noop", None)


def test_plan_none_and_unknown_are_noop():
    st, s = menubar.MenuBarState(), menubar.MenuBarSettings()
    assert menubar.plan_auto_switch(("none", None), st, s, 1e9) == ("noop", None)
    assert menubar.plan_auto_switch(("unknown_active", None), st, s, 1e9) == ("noop", None)


def test_snapshot_full_fetches_all(monkeypatch):
    seen = {}
    class _SW:
        _logger = type("L", (), {"debug": staticmethod(lambda *a, **k: None)})()
        def _build_accounts_info(self):
            creds = ""
            return [(1, "a@x", "", "", True, creds), (2, "b@x", "", "", False, creds)]
        def _collect_usage(self, info, only=None):
            seen["only"] = only
            return [None, None]
    menubar._snapshot(_SW(), full=True)
    assert seen["only"] is None  # full -> all accounts


def test_snapshot_incremental_fetches_active_only():
    seen = {}
    class _SW:
        _logger = type("L", (), {"debug": staticmethod(lambda *a, **k: None)})()
        def _build_accounts_info(self):
            return [(1, "a@x", "", "", False, ""), (2, "b@x", "", "", True, "")]
        def _collect_usage(self, info, only=None):
            seen["only"] = only
            return [None, None]
    menubar._snapshot(_SW(), full=False)
    assert seen["only"] == {"2"}  # incremental -> only the active account


def test_settings_strategy_default():
    assert menubar.MenuBarSettings().auto_switch_strategy == "reactive"


def test_settings_strategy_round_trip(tmp_path: Path):
    path = tmp_path / "s.json"
    s = menubar.MenuBarSettings(auto_switch_strategy="consume-first")
    s.save(path)
    assert menubar.MenuBarSettings.load(path).auto_switch_strategy == "consume-first"


def test_state_blocked_round_trip(tmp_path: Path):
    path = tmp_path / "state.json"
    st = menubar.MenuBarState(last_switch_at=1.0, blocked=["2", "3"])
    st.save(path)
    loaded = menubar.MenuBarState.load(path)
    assert loaded.blocked == ["2", "3"]
    assert loaded.last_switch_at == 1.0


def test_state_blocked_defaults_when_malformed(tmp_path: Path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"blocked": [1, 2]}), encoding="utf-8")  # non-str elems
    assert menubar.MenuBarState.load(path).blocked == []
    path.write_text(json.dumps({"blocked": "nope"}), encoding="utf-8")
    assert menubar.MenuBarState.load(path).blocked == []


def test_next_blocked_enter_stay_exit():
    prev = frozenset()
    # enter at >= threshold
    assert menubar.next_blocked({"1": 96.0}, 95, 5, prev) == frozenset({"1"})
    # stay blocked within the dead band (95-5=90 .. 95)
    assert menubar.next_blocked({"1": 92.0}, 95, 5, frozenset({"1"})) == frozenset({"1"})
    # exit only below threshold - hysteresis
    assert menubar.next_blocked({"1": 89.0}, 95, 5, frozenset({"1"})) == frozenset()
    # not blocked and below threshold -> stays out
    assert menubar.next_blocked({"1": 92.0}, 95, 5, frozenset()) == frozenset()


def test_next_blocked_unknown_carries_prev():
    assert menubar.next_blocked({"1": None}, 95, 5, frozenset({"1"})) == frozenset({"1"})
    assert menubar.next_blocked({"1": None}, 95, 5, frozenset()) == frozenset()


def test_resets_at_ts_orders_and_handles_missing():
    early = {"resets_at": "2026-06-24T07:00:00+00:00"}
    late = {"resets_at": "2026-06-26T07:00:00+00:00"}
    assert menubar._resets_at_ts(early) < menubar._resets_at_ts(late)
    assert menubar._resets_at_ts({"pct": 5.0}) == float("inf")   # no resets_at
    assert menubar._resets_at_ts({"resets_at": "garbage"}) == float("inf")
    assert menubar._resets_at_ts(None) == float("inf")


def _ra(num, pct5, pct7, active=False):
    return (num, f"a{num}@x.com", active,
            {"five_hour": {"pct": pct5}, "seven_day": {"pct": pct7}})


def test_decide_reactive_hysteresis_excludes_blocked_candidate():
    # active over limit; only peer (#2) is at 92 — within the 90..95 dead band.
    accts = [_ra(1, 99, 10, active=True), _ra(2, 92, 20)]
    # not blocked -> 92 < 95 -> eligible -> switch
    assert menubar.decide_auto_switch(accts, 95, frozenset()) == ("switch", 2)
    # blocked -> must clear 90 -> 92 >= 90 -> ineligible -> no candidate
    assert menubar.decide_auto_switch(accts, 95, frozenset({"2"})) == ("no_candidate", None)


def test_decide_reactive_unverifiable_when_only_peer_unreadable():
    accts = [_ra(1, 99, 10, active=True), (2, "b@x", False, "no credentials")]
    assert menubar.decide_auto_switch(accts, 95, frozenset()) == ("no_candidate_unverifiable", None)


def test_decide_reactive_exhausted_stays_no_candidate():
    accts = [_ra(1, 99, 10, active=True), _ra(2, 96, 50)]  # peer over limit, readable
    assert menubar.decide_auto_switch(accts, 95, frozenset()) == ("no_candidate", None)


def test_plan_silent_outcomes_are_noop():
    st, s = menubar.MenuBarState(), menubar.MenuBarSettings()
    assert menubar.plan_auto_switch(("no_candidate_unverifiable", None), st, s, 1e9) == ("noop", None)
    assert menubar.plan_auto_switch(("all_session_limited", None), st, s, 1e9) == ("noop", None)


def _cf(num, pct5, pct7, reset7, active=False):
    return (num, f"a{num}@x.com", active,
            {"five_hour": {"pct": pct5},
             "seven_day": {"pct": pct7, "resets_at": reset7}})

_R_EARLY = "2026-06-24T07:00:00+00:00"
_R_MID = "2026-06-25T07:00:00+00:00"
_R_LATE = "2026-06-26T07:00:00+00:00"


def test_consume_first_picks_soonest_weekly_reset():
    # active #1 resets late; #2 resets early -> switch to #2 (consume it first).
    accts = [_cf(1, 10, 20, _R_LATE, active=True), _cf(2, 10, 20, _R_EARLY)]
    assert menubar.decide_consume_first(accts, 95, frozenset()) == ("switch", 2)


def test_consume_first_stays_when_active_is_optimal():
    accts = [_cf(1, 10, 20, _R_EARLY, active=True), _cf(2, 10, 20, _R_LATE)]
    assert menubar.decide_consume_first(accts, 95, frozenset()) == ("none", None)


def test_consume_first_tie_break_headroom_then_rotation():
    # equal reset -> more headroom (lower worst) wins; then rotation order.
    accts = [_cf(1, 99, 99, _R_LATE, active=True),
             _cf(2, 40, 30, _R_EARLY), _cf(3, 10, 80, _R_EARLY)]
    # #2 worst=40, #3 worst=80 -> #2
    assert menubar.decide_consume_first(accts, 95, frozenset()) == ("switch", 2)


def test_consume_first_tie_break_by_rotation_index():
    # #2 and #3 share reset time AND worst pct -> lower snapshot index (#2) wins.
    accts = [_cf(1, 99, 99, _R_LATE, active=True),
             _cf(2, 40, 40, _R_EARLY), _cf(3, 40, 40, _R_EARLY)]
    assert menubar.decide_consume_first(accts, 95, frozenset()) == ("switch", 2)


def test_consume_first_all_session_limited_is_silent():
    # everyone 5h-saturated but weekly has room -> temporary, silent stay.
    accts = [_cf(1, 99, 10, _R_EARLY, active=True), _cf(2, 98, 20, _R_LATE)]
    assert menubar.decide_consume_first(accts, 95, frozenset()) == ("all_session_limited", None)


def test_consume_first_exhausted_notifies():
    accts = [_cf(1, 99, 99, _R_EARLY, active=True), _cf(2, 98, 99, _R_LATE)]
    assert menubar.decide_consume_first(accts, 95, frozenset()) == ("no_candidate", None)


def test_consume_first_unverifiable_is_silent():
    accts = [_cf(1, 99, 99, _R_EARLY, active=True), (2, "b@x", False, None)]
    assert menubar.decide_consume_first(accts, 95, frozenset()) == ("no_candidate_unverifiable", None)


def test_consume_first_unknown_active():
    accts = [(1, "a@x", True, "no credentials"), _cf(2, 10, 20, _R_EARLY)]
    assert menubar.decide_consume_first(accts, 95, frozenset()) == ("unknown_active", None)


def test_limiting_pct_by_account_per_strategy():
    accts = [_ra(1, 80, 50, active=True), (2, "b@x", False, None)]
    assert menubar.limiting_pct_by_account(accts, "reactive") == {"1": 80.0, "2": None}
    assert menubar.limiting_pct_by_account(accts, "consume-first") == {"1": 80.0, "2": None}


def test_evaluate_strategy_dispatch():
    accts = [_cf(1, 10, 20, _R_LATE, active=True), _cf(2, 10, 20, _R_EARLY)]
    assert menubar.evaluate_strategy("consume-first", accts, 95, frozenset()) == ("switch", 2)
    # reactive: active not over limit -> none
    assert menubar.evaluate_strategy("reactive", accts, 95, frozenset()) == ("none", None)



# --- reset countdown (computed LIVE from resets_at, not the frozen string) -----

import datetime as _dt

_NOW = 1_000_000.0


def _iso(delta_s):  # ISO-8601 for _NOW + delta_s, UTC
    return _dt.datetime.fromtimestamp(_NOW + delta_s, _dt.timezone.utc).isoformat()


def test_live_countdown_formats_from_resets_at():
    assert menubar._live_countdown({"resets_at": _iso(9 * 3600 + 5 * 60)}, _NOW) == "9h 5m"
    assert menubar._live_countdown({"resets_at": _iso(86400 + 19 * 3600)}, _NOW) == "1d 19h"
    assert menubar._live_countdown({"resets_at": _iso(34 * 60)}, _NOW) == "34m"


def test_live_countdown_none_when_passed_or_missing():
    assert menubar._live_countdown({"resets_at": _iso(-60)}, _NOW) is None   # already reset
    assert menubar._live_countdown({"pct": 5.0}, _NOW) is None               # no resets_at
    assert menubar._live_countdown("no credentials", _NOW) is None


def test_usage_summary_live_countdown_from_resets_at():
    usage = {
        "five_hour": {"pct": 42.0, "resets_at": _iso(2 * 3600 + 33 * 60)},
        "seven_day": {"pct": 18.0, "resets_at": _iso(86400 + 19 * 3600)},
        "spend": {"pct": 30.0},
    }
    assert menubar.usage_summary(usage, _NOW) == "5h 42% (2h 33m) · 7d 18% (1d 19h) · $ 30%"


def test_usage_summary_omits_countdown_when_passed_or_missing():
    # 5h reset already passed (stale data) -> omit; 7d has no resets_at -> omit
    usage = {"five_hour": {"pct": 53.0, "resets_at": _iso(-60)}, "seven_day": {"pct": 8.0}}
    assert menubar.usage_summary(usage, _NOW) == "5h 53% · 7d 8%"


# --- LaunchAgent plist rendering (cswap --install-startup) -------------------

def test_render_launch_agent_plist_has_core_keys():
    xml = menubar.render_launch_agent_plist(
        label="com.claude-swap.menubar",
        program_args=["/path/python", "-m", "claude_swap", "--menubar"],
    )
    parsed = plistlib.loads(xml.encode("utf-8"))
    assert parsed["Label"] == "com.claude-swap.menubar"
    assert parsed["ProgramArguments"] == [
        "/path/python", "-m", "claude_swap", "--menubar",
    ]
    # Start at login and whenever the agent is bootstrapped.
    assert parsed["RunAtLoad"] is True
    # Must load into the GUI (Aqua) session: a menu-bar icon needs WindowServer,
    # and Keychain access only works from the user's unlocked GUI session.
    assert parsed["LimitLoadToSessionType"] == "Aqua"


def test_guard_against_terminal_suspend_ignores_sigtstp():
    """Ctrl+Z must not be able to suspend (freeze) the menu bar: SIGTSTP is set
    to SIG_IGN. Save/restore the disposition so we don't disable job control for
    the rest of the test session."""
    import signal

    prev = signal.getsignal(signal.SIGTSTP)
    try:
        menubar._guard_against_terminal_suspend()
        assert signal.getsignal(signal.SIGTSTP) == signal.SIG_IGN
    finally:
        signal.signal(signal.SIGTSTP, prev)


def test_render_launch_agent_plist_keepalive_respects_clean_quit():
    # Restart on crash, but a clean Quit from the menu must stay quit
    # (KeepAlive only when the process exited non-zero).
    xml = menubar.render_launch_agent_plist(
        label="x", program_args=["cswap", "--menubar"],
    )
    parsed = plistlib.loads(xml.encode("utf-8"))
    assert parsed["KeepAlive"] == {"SuccessfulExit": False}


def test_render_launch_agent_plist_includes_log_paths_when_given():
    xml = menubar.render_launch_agent_plist(
        label="x",
        program_args=["cswap", "--menubar"],
        stdout_path="/tmp/out.log",
        stderr_path="/tmp/err.log",
    )
    parsed = plistlib.loads(xml.encode("utf-8"))
    assert parsed["StandardOutPath"] == "/tmp/out.log"
    assert parsed["StandardErrorPath"] == "/tmp/err.log"


def test_render_launch_agent_plist_omits_log_paths_when_absent():
    xml = menubar.render_launch_agent_plist(
        label="x", program_args=["cswap", "--menubar"],
    )
    parsed = plistlib.loads(xml.encode("utf-8"))
    assert "StandardOutPath" not in parsed
    assert "StandardErrorPath" not in parsed


def _fake_home(monkeypatch, tmp_path):
    monkeypatch.setattr(menubar.Path, "home", classmethod(lambda cls: tmp_path))


def _fake_completed(returncode=0, stderr=b""):
    """Minimal stand-in for subprocess.CompletedProcess (returncode + stderr)."""
    return type("CP", (), {"returncode": returncode, "stderr": stderr})()


def _capture_launchctl(monkeypatch, returncode=0, stderr=b""):
    """Stub subprocess.run so tests never load a real launchd agent.

    Returns a fake CompletedProcess so install_startup can inspect the bootstrap
    returncode without spawning launchctl.
    """
    calls: list[list[str]] = []

    def fake_run(*a, **k):
        calls.append(list(a[0]))
        return _fake_completed(returncode=returncode, stderr=stderr)

    monkeypatch.setattr(menubar.subprocess, "run", fake_run)
    return calls


def test_install_startup_writes_valid_plist_and_bootstraps(tmp_path, monkeypatch):
    _fake_home(monkeypatch, tmp_path)
    calls = _capture_launchctl(monkeypatch)

    path = menubar.install_startup()

    assert path == tmp_path / "Library/LaunchAgents/com.claude-swap.menubar.plist"
    parsed = plistlib.loads(path.read_bytes())
    assert parsed["Label"] == menubar.LAUNCH_AGENT_LABEL
    assert parsed["ProgramArguments"][-1] == "--menubar"
    # It must (re)load into the user's GUI domain.
    assert any("bootstrap" in c and any("gui/" in a for a in c) for c in calls)


def test_uninstall_startup_unloads_and_removes_plist(tmp_path, monkeypatch):
    _fake_home(monkeypatch, tmp_path)
    calls = _capture_launchctl(monkeypatch)

    path = menubar.install_startup()
    assert path.exists()

    existed = menubar.uninstall_startup()

    assert existed is True
    assert not path.exists()
    assert any("bootout" in c for c in calls)


def test_uninstall_startup_returns_false_when_not_installed(tmp_path, monkeypatch):
    _fake_home(monkeypatch, tmp_path)
    _capture_launchctl(monkeypatch)
    assert menubar.uninstall_startup() is False


# --- Finding 1: install_startup must surface a launchctl bootstrap failure -----

def test_install_startup_raises_when_bootstrap_fails(tmp_path, monkeypatch):
    # Over SSH/headless or under an MDM/SIP policy, `launchctl bootstrap gui/$UID`
    # returns non-zero and nothing is loaded. install_startup must NOT report
    # success: it raises ClaudeSwitchError so the CLI can report the real failure
    # instead of claiming the agent is installed and running.
    _fake_home(monkeypatch, tmp_path)

    def fake_run(*a, **k):
        argv = list(a[0])
        if "bootstrap" in argv:
            return _fake_completed(returncode=5, stderr=b"Could not find domain for: gui/501")
        return _fake_completed(returncode=0)

    monkeypatch.setattr(menubar.subprocess, "run", fake_run)

    with pytest.raises(menubar.ClaudeSwitchError) as exc:
        menubar.install_startup()
    # The captured launchctl stderr is surfaced for diagnosis.
    assert "Could not find domain" in str(exc.value)


def test_install_startup_retries_transient_bootstrap_failure(tmp_path, monkeypatch):
    # Re-installing over a running agent, the async bootout can leave the first
    # bootstrap racing ("already bootstrapped"). A transient failure that clears on
    # retry must NOT raise — the agent loads on a subsequent attempt.
    _fake_home(monkeypatch, tmp_path)
    monkeypatch.setattr(menubar.time, "sleep", lambda *_a, **_k: None)
    seq = {"n": 0}

    def fake_run(*a, **k):
        argv = list(a[0])
        if "bootstrap" in argv:
            seq["n"] += 1
            # First bootstrap fails (race), second succeeds.
            return _fake_completed(returncode=5 if seq["n"] == 1 else 0,
                                   stderr=b"Bootstrap failed: 5: Input/output error")
        return _fake_completed(returncode=0)

    monkeypatch.setattr(menubar.subprocess, "run", fake_run)
    path = menubar.install_startup()  # must not raise
    assert path.exists()
    assert seq["n"] >= 2  # retried past the transient failure


def test_install_startup_succeeds_when_bootstrap_ok(tmp_path, monkeypatch):
    # The common interactive-GUI path: bootstrap returns 0 -> no exception, and
    # the kickstart call still runs (install also launches the app immediately).
    _fake_home(monkeypatch, tmp_path)
    calls = _capture_launchctl(monkeypatch)  # returncode 0
    path = menubar.install_startup()
    assert path.exists()
    assert any("kickstart" in c for c in calls)


# --- Finding 2: uninstall_startup must survive a unlink TOCTOU race ------------

def test_uninstall_startup_tolerates_plist_vanishing_after_exists(tmp_path, monkeypatch):
    # Simulate the TOCTOU window: exists() returns True, then the plist is gone
    # before unlink() (a concurrent uninstall, manual rm, or external cleanup).
    # uninstall must not raise FileNotFoundError.
    _fake_home(monkeypatch, tmp_path)
    _capture_launchctl(monkeypatch)
    path = menubar.install_startup()
    assert path.exists()

    real_exists = menubar.Path.exists

    def exists_then_delete(self):
        result = real_exists(self)
        if self == path and result:
            # Delete the file in the window between exists() and unlink().
            real_unlink = type(self).unlink
            real_unlink(self)
        return result

    monkeypatch.setattr(menubar.Path, "exists", exists_then_delete)

    # Without missing_ok=True this raises FileNotFoundError.
    assert menubar.uninstall_startup() is True
    assert not path.exists()


# --- Findings 3/4/5: menu-bar banner notifications must route through ----------
# notify.notify (osascript, works from the non-bundled LaunchAgent) and never
# through rumps.notification (raises RuntimeError without a .app bundle).

import ast as _ast


def _menubar_source_tree():
    src_path = Path(menubar.__file__)
    return _ast.parse(src_path.read_text(encoding="utf-8"))


def _find_function(tree, name):
    for node in _ast.walk(tree):
        if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"function {name!r} not found")


def _attr_calls(node, attr_path):
    """Yield Call nodes whose func is the dotted ``attr_path`` (e.g. 'rumps.notification')."""
    obj, attr = attr_path.split(".")
    for n in _ast.walk(node):
        if (
            isinstance(n, _ast.Call)
            and isinstance(n.func, _ast.Attribute)
            and n.func.attr == attr
            and isinstance(n.func.value, _ast.Name)
            and n.func.value.id == obj
        ):
            yield n


def test_no_rumps_notification_calls_remain():
    # rumps.notification raises under the LaunchAgent (no bundle id); every
    # banner must go through notify.notify instead. Comments/docstrings are
    # ignored because we inspect the AST, not the text.
    tree = _menubar_source_tree()
    calls = list(_attr_calls(tree, "rumps.notification"))
    assert calls == [], f"rumps.notification still called at lines {[c.lineno for c in calls]}"


def test_menubar_imports_notify():
    # The module must use the osascript notifier.
    assert hasattr(menubar, "notify")
    assert menubar.notify.notify.__module__ == "claude_swap.notify"


def test_notify_noswap_persists_timestamp_after_notifying():
    # Finding 4: the rate-limit timestamp (last_noswap_notify_at) must be written
    # AFTER the notification is dispatched, or a failed notification would burn the
    # NOSWAP_NOTIFY_EVERY budget and suppress retries for an hour.
    run_fn = _find_function(_menubar_source_tree(), "_maybe_auto_switch")
    notify_line = None
    persist_line = None
    for n in _ast.walk(run_fn):
        # The notify.notify call for the no-swap banner.
        if (
            isinstance(n, _ast.Call)
            and isinstance(n.func, _ast.Attribute)
            and n.func.attr == "notify"
            and isinstance(n.func.value, _ast.Name)
            and n.func.value.id == "notify"
        ):
            notify_line = n.lineno
        # The assignment self.state.last_noswap_notify_at = now
        if isinstance(n, _ast.Assign):
            for tgt in n.targets:
                if isinstance(tgt, _ast.Attribute) and tgt.attr == "last_noswap_notify_at":
                    persist_line = n.lineno
    assert notify_line is not None and persist_line is not None
    assert notify_line < persist_line, "must notify before persisting the rate-limit timestamp"


def test_browser_signin_refreshes_before_success_notification():
    # Finding 5: refresh_async(full=True) must run BEFORE the "Account added"
    # notification so a notification failure can never skip the UI refresh and
    # leave the just-added account missing from the menu.
    tree = _menubar_source_tree()
    worker = _find_function(tree, "worker")
    refresh_lines = []
    notify_lines = []
    for n in _ast.walk(worker):
        if (
            isinstance(n, _ast.Call)
            and isinstance(n.func, _ast.Attribute)
            and n.func.attr == "refresh_async"
        ):
            refresh_lines.append(n.lineno)
        if (
            isinstance(n, _ast.Call)
            and isinstance(n.func, _ast.Attribute)
            and n.func.attr == "notify"
            and isinstance(n.func.value, _ast.Name)
            and n.func.value.id == "notify"
        ):
            notify_lines.append(n.lineno)
    assert refresh_lines and notify_lines
    # The success-path refresh must precede the success-path "Account added" notify
    # (the earliest notify in source order).
    assert min(refresh_lines) < min(notify_lines), \
        "must refresh before the success notification"


# --- Finding 6: consume-first must not switch off an equally-optimal active ----

def test_consume_first_stays_on_tie_when_active_listed_after_peer():
    # Active account ties a peer on BOTH the 7d reset time AND worst-pct, and the
    # active account is listed AFTER the equally-good peer. The active account is
    # already optimal -> stay, not a pointless switch to the peer.
    accts = [_cf(1, 10, 10, _R_EARLY), _cf(2, 10, 10, _R_EARLY, active=True)]
    assert menubar.decide_consume_first(accts, 95, frozenset()) == ("none", None)


# --- Finding 7: consume-first must not churn off a healthy active account whose
# 7d reset time is missing/unparseable (API omitted resets_at). ----------------

def _cf_no_reset(num, pct5, pct7, active=False):
    return (num, f"a{num}@x.com", active,
            {"five_hour": {"pct": pct5}, "seven_day": {"pct": pct7}})  # no resets_at


def test_consume_first_stays_when_active_reset_missing():
    # Active #2 has a healthy 7d pct but no resets_at; peer #1 has a parseable
    # reset. The active must not be demoted below the peer (an unknown reset is
    # "no information", not "resets last").
    accts = [_cf(1, 10, 10, _R_EARLY), _cf_no_reset(2, 10, 10, active=True)]
    assert menubar.decide_consume_first(accts, 95, frozenset()) == ("none", None)


def test_consume_first_missing_active_reset_does_not_switch_to_worse_peer():
    # Active #2 has huge headroom (5%/5%) but no resets_at; peer #1 is far worse
    # (50%/50%) with a parseable reset. Switching to the strictly worse account
    # would be wrong.
    accts = [_cf(1, 50, 50, _R_EARLY), _cf_no_reset(2, 5, 5, active=True)]
    assert menubar.decide_consume_first(accts, 95, frozenset()) == ("none", None)


# --- account_detail_lines (Feature 1: flat indented status view) -------------
# Mirrors the CLI's per-window tree rows in the dropdown. format_reset is mocked
# so the clock/countdown are deterministic and free of tz/now coupling.

def test_account_detail_lines_both_windows(monkeypatch):
    resets = {"r5": ("4h 46m", "18:59"), "r7": ("2d 3h", "Jul 1 09:00")}
    monkeypatch.setattr(menubar.oauth, "format_reset", lambda ra: resets[ra])
    usage = {
        "five_hour": {"pct": 5, "resets_at": "r5"},
        "seven_day": {"pct": 42, "resets_at": "r7"},
    }
    assert menubar.account_detail_lines(usage) == [
        "5h:  5%   resets 18:59   in 4h 46m",
        "7d: 42%   resets Jul 1 09:00   in 2d 3h",
    ]


def test_account_detail_lines_omits_window_with_unknown_pct(monkeypatch):
    monkeypatch.setattr(menubar.oauth, "format_reset", lambda ra: ("1h", "10:00"))
    usage = {
        "five_hour": {"resets_at": "x"},          # no pct -> omitted
        "seven_day": {"pct": 30, "resets_at": "x"},
    }
    assert menubar.account_detail_lines(usage) == ["7d: 30%   resets 10:00   in 1h"]


def test_account_detail_lines_omits_reset_segment_when_no_resets_at():
    # No resets_at -> just the percent, and format_reset is never called.
    assert menubar.account_detail_lines({"five_hour": {"pct": 12}}) == ["5h: 12%"]


def test_account_detail_lines_omits_reset_segment_when_unparseable(monkeypatch):
    def boom(_ra):
        raise ValueError("bad date")
    monkeypatch.setattr(menubar.oauth, "format_reset", boom)
    usage = {"five_hour": {"pct": 12, "resets_at": "not-a-date"}}
    assert menubar.account_detail_lines(usage) == ["5h: 12%"]


def test_account_detail_lines_non_dict_returns_empty():
    assert menubar.account_detail_lines("rate limited") == []
    assert menubar.account_detail_lines(None) == []
    assert menubar.account_detail_lines({}) == []


def test_account_detail_lines_ignores_spend_window():
    # Only the 5h/7d rate-limit windows are rendered; the $ spend axis is omitted.
    usage = {"five_hour": {"pct": 5}, "spend": {"pct": 30}}
    assert menubar.account_detail_lines(usage) == ["5h:  5%"]


# --- group_running_instances (Feature 1: "Running instances" section) --------
# Grouping mirrors switcher.status exactly, reusing the same printer helpers.

from claude_swap.process_detection import ClaudeSession, IdeInstance  # noqa: E402


def _session(entrypoint, cwd, pid=1234):
    return ClaudeSession(
        pid=pid, session_id="s", cwd=cwd, started_at=0,
        kind="interactive", entrypoint=entrypoint,
    )


def _ide(ide_name, folders, pid=4321, port=5000):
    return IdeInstance(
        port=port, pid=pid, ide_name=ide_name, workspace_folders=list(folders),
    )


def test_group_running_instances_counts_sessions_per_group():
    sessions = [
        _session("cli", "/work/a"),
        _session("cli", "/work/a"),
        _session("claude-vscode", "/work/b"),
    ]
    groups = menubar.group_running_instances(sessions, [])
    assert ("CLI", "/work/a", 2, False) in groups
    assert ("VS Code", "/work/b", 1, False) in groups
    assert len(groups) == 2


def test_group_running_instances_merges_ide_into_session_group():
    # A session and an IDE lockfile on the same folder collapse into one group.
    sessions = [_session("claude-vscode", "/work/proj")]
    ides = [_ide("Visual Studio Code", ["/work/proj"])]
    assert menubar.group_running_instances(sessions, ides) == [
        ("VS Code", "/work/proj", 1, True),
    ]


def test_group_running_instances_ide_only_group_per_folder():
    ides = [_ide("Cursor", ["/work/x", "/work/y"])]
    assert menubar.group_running_instances([], ides) == [
        ("Cursor", "/work/x", 0, True),
        ("Cursor", "/work/y", 0, True),
    ]


def test_group_running_instances_empty():
    assert menubar.group_running_instances([], []) == []


def test_group_running_instances_abbreviates_home(monkeypatch, tmp_path):
    _fake_home(monkeypatch, tmp_path)
    sess = _session("cli", str(tmp_path / "Dev" / "proj"))
    assert menubar.group_running_instances([sess], []) == [
        ("CLI", "~/Dev/proj", 1, False),
    ]


# --- format_instance_row (Feature 1) -----------------------------------------

def test_format_instance_row_sessions_and_ide():
    assert menubar.format_instance_row(("VS Code", "~/Dev/TL-Starnav", 2, True)) == (
        "VS Code   ~/Dev/TL-Starnav  (2 sessions, IDE)"
    )


def test_format_instance_row_single_session_is_singular():
    assert menubar.format_instance_row(("CLI", "~/x", 1, False)) == "CLI   ~/x  (1 session)"


def test_format_instance_row_ide_only():
    assert menubar.format_instance_row(("Cursor", "~/y", 0, True)) == "Cursor   ~/y  (IDE)"


# --- _snapshot grows a grouped "instances" list (Feature 1) ------------------

class _SnapSW:
    _logger = type("L", (), {"debug": staticmethod(lambda *a, **k: None)})()

    def _build_accounts_info(self):
        return [(1, "a@x", "", "", True, "")]

    def _collect_usage(self, info, only=None):
        return [None]


def test_snapshot_includes_grouped_instances(monkeypatch):
    monkeypatch.setattr(
        menubar, "get_running_instances",
        lambda: ([_session("claude-vscode", "/work/a")],
                 [_ide("Visual Studio Code", ["/work/a"])]),
    )
    snap = menubar._snapshot(_SnapSW(), full=True)
    assert snap["instances"] == [("VS Code", "/work/a", 1, True)]


def test_snapshot_instances_degrade_to_empty_on_failure(monkeypatch):
    def boom():
        raise OSError("filesystem unavailable")
    monkeypatch.setattr(menubar, "get_running_instances", boom)
    snap = menubar._snapshot(_SnapSW(), full=True)
    assert snap["instances"] == []


def test_snapshot_degraded_path_includes_empty_instances(monkeypatch):
    class _BrokenSW:
        _logger = type("L", (), {"debug": staticmethod(lambda *a, **k: None)})()

        def _build_accounts_info(self):
            raise RuntimeError("boom")

    snap = menubar._snapshot(_BrokenSW(), full=True)
    assert snap["instances"] == []
    assert snap["accounts"] == []
