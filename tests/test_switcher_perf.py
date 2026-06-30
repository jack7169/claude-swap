"""Phase 10 performance/maintainability polish for switcher.py (F81, F78, F79, F11).

These tests pin down behavior-preserving optimizations:

* **F81** — ``_get_sequence_data_migrated`` must not re-scan every account for a
  missing ``organizationUuid`` on every call. After the org-fields migration has
  run (or been determined unnecessary) for a switcher instance, a second call
  short-circuits the scan and never re-invokes ``_migrate_org_fields``. A *fresh*
  switcher still migrates a sequence.json that lacks org fields (a restored old
  file re-triggers migration in a new process).

* **F78 / F79** — the usage-aware switch strategies (``best`` / ``next-available``)
  must read each account's backup credentials/config at most once per switch
  (no duplicate reads between the switchability check and the usage computation),
  and must select the SAME target as the un-optimized code.

* **F11** — the renamed local (``oauth`` → ``oauth_data``) paths still extract the
  access token / org fields correctly (no module shadowing regression).
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from claude_swap import oauth as _oauth
from claude_swap.models import Platform
from claude_swap.switcher import ClaudeAccountSwitcher


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _make_switcher() -> ClaudeAccountSwitcher:
    s = ClaudeAccountSwitcher()
    s.platform = Platform.LINUX
    s._setup_directories()
    s._init_sequence_file()
    return s


_OAUTH_CREDS = json.dumps({"claudeAiOauth": {"accessToken": "sk-x"}})


def _backup_config(email: str) -> str:
    return json.dumps({"oauthAccount": {"emailAddress": email}})


def _seed_three_accounts(s: ClaudeAccountSwitcher) -> None:
    """Three managed accounts with org fields already present, plus on-disk
    backup creds + config for each (so _account_is_switchable / _build_accounts_info
    actually read the backend)."""
    data = {
        "activeAccountNumber": 1,
        "lastUpdated": "2024-01-01T00:00:00Z",
        "sequence": [1, 2, 3],
        "accounts": {
            "1": {"email": "a@x.com", "uuid": "u1", "organizationUuid": "",
                  "organizationName": "", "added": "2024-01-01T00:00:00Z"},
            "2": {"email": "b@x.com", "uuid": "u2", "organizationUuid": "",
                  "organizationName": "", "added": "2024-01-02T00:00:00Z"},
            "3": {"email": "c@x.com", "uuid": "u3", "organizationUuid": "",
                  "organizationName": "", "added": "2024-01-03T00:00:00Z"},
        },
    }
    s._write_json(s.sequence_file, data)
    for num, email in (("1", "a@x.com"), ("2", "b@x.com"), ("3", "c@x.com")):
        s._write_account_credentials(num, email, _OAUTH_CREDS)
        s._write_account_config(num, email, _backup_config(email))


def _set_active(temp_home: Path, email: str) -> None:
    (temp_home / ".claude.json").write_text(
        json.dumps({"oauthAccount": {"emailAddress": email}})
    )
    # Live credentials for the active account (Linux file backend) so the active
    # slot resolves to a usable token rather than USAGE_NO_CREDENTIALS.
    (temp_home / ".claude" / ".credentials.json").write_text(_OAUTH_CREDS)


# --------------------------------------------------------------------------
# F81 — memoize the org-fields migration scan
# --------------------------------------------------------------------------

class TestSequenceMigrationMemo:
    def test_second_call_does_not_rescan_or_remigrate(self, temp_home, monkeypatch):
        """After the first _get_sequence_data_migrated call, a second call must
        NOT re-scan accounts nor re-invoke _migrate_org_fields."""
        s = _make_switcher()
        _seed_three_accounts(s)  # all accounts already carry organizationUuid

        migrate_calls = Counter()
        real_migrate = s._migrate_org_fields

        def spy_migrate():
            migrate_calls["n"] += 1
            return real_migrate()

        monkeypatch.setattr(s, "_migrate_org_fields", spy_migrate)

        # First call evaluates (no migration needed since org fields present).
        s._get_sequence_data_migrated()
        assert migrate_calls["n"] == 0

        # Now corrupt the on-disk sequence so an account LACKS organizationUuid.
        # The naive (un-memoized) implementation would re-scan, see the missing
        # field, and re-run the migration. The memo must short-circuit instead:
        # _migrate_org_fields stays at zero calls.
        data = s._get_sequence_data()
        del data["accounts"]["2"]["organizationUuid"]
        s._write_json(s.sequence_file, data)

        out = s._get_sequence_data_migrated()
        assert out is not None
        assert migrate_calls["n"] == 0, "memo must short-circuit the re-scan"

    def test_first_call_migrates_when_org_fields_missing(self, temp_home, monkeypatch):
        """A switcher whose sequence.json lacks organizationUuid migrates on the
        first call (the scan fires and _migrate_org_fields runs)."""
        s = _make_switcher()
        data = {
            "activeAccountNumber": 1,
            "lastUpdated": "2024-01-01T00:00:00Z",
            "sequence": [1, 2],
            "accounts": {
                "1": {"email": "a@x.com", "uuid": "u1",
                      "added": "2024-01-01T00:00:00Z"},
                "2": {"email": "b@x.com", "uuid": "u2",
                      "added": "2024-01-02T00:00:00Z"},
            },
        }
        s._write_json(s.sequence_file, data)

        migrate_calls = Counter()
        real_migrate = s._migrate_org_fields

        def spy_migrate():
            migrate_calls["n"] += 1
            return real_migrate()

        monkeypatch.setattr(s, "_migrate_org_fields", spy_migrate)

        out = s._get_sequence_data_migrated()
        assert migrate_calls["n"] == 1
        # Backfilled — every account now carries organizationUuid.
        for acc in out["accounts"].values():
            assert "organizationUuid" in acc

    def test_fresh_switcher_remigrates_restored_old_sequence(self, temp_home):
        """A restored old-format sequence.json re-triggers migration in a FRESH
        process/instance — the memo never persists across instances."""
        s1 = _make_switcher()
        old = {
            "activeAccountNumber": 1,
            "lastUpdated": "2024-01-01T00:00:00Z",
            "sequence": [1],
            "accounts": {
                "1": {"email": "a@x.com", "uuid": "u1",
                      "added": "2024-01-01T00:00:00Z"},
            },
        }
        s1._write_json(s1.sequence_file, old)
        out1 = s1._get_sequence_data_migrated()
        assert "organizationUuid" in out1["accounts"]["1"]

        # Simulate restoring the OLD file under a new process.
        s1._write_json(s1.sequence_file, old)
        s2 = ClaudeAccountSwitcher()
        s2.platform = Platform.LINUX
        out2 = s2._get_sequence_data_migrated()
        assert "organizationUuid" in out2["accounts"]["1"]


# --------------------------------------------------------------------------
# F78 / F79 — de-duplicate backup reads in usage-aware switch strategies
# --------------------------------------------------------------------------

def _install_read_spies(s, monkeypatch):
    """Count actual backend reads (the work the dedup eliminates).

    Spies on the store/disk read primitives — NOT the switcher delegators — so a
    cache HIT (delegator called twice but served from memory) is correctly NOT
    counted as a duplicate read.
    """
    creds_reads = Counter()
    config_reads = Counter()

    real_creds = s._store._read_account_credentials
    real_config = s._read_account_config_uncached

    def spy_creds(num, email):
        creds_reads[str(num)] += 1
        return real_creds(num, email)

    def spy_config(num, email):
        config_reads[str(num)] += 1
        return real_config(num, email)

    monkeypatch.setattr(s._store, "_read_account_credentials", spy_creds)
    monkeypatch.setattr(s, "_read_account_config_uncached", spy_config)
    return creds_reads, config_reads


def _patch_usage(monkeypatch, mapping):
    """fetch_usage_for_account returns a five_hour pct from mapping (by num)."""
    def fake(num, email, creds, is_active=False, persist_credentials=None):
        pct = mapping.get(str(num))
        if pct is None:
            return None
        return {"five_hour": {"pct": pct}}
    monkeypatch.setattr(_oauth, "fetch_usage_for_account", fake)


class TestBestStrategyReadDedup:
    def test_best_reads_each_backup_once_and_selects_same_target(
        self, temp_home, monkeypatch
    ):
        s = _make_switcher()
        _seed_three_accounts(s)
        _set_active(temp_home, "a@x.com")  # active = slot 1
        monkeypatch.setattr(s, "_live_session_pids", lambda *a: [])
        monkeypatch.setattr(s, "_active_cc_running", lambda: True)
        # Slot 1 at 50% (headroom 50), slot 2 at 10% (headroom 90 -> best),
        # slot 3 at 80% (headroom 20).
        _patch_usage(monkeypatch, {"1": 50.0, "2": 10.0, "3": 80.0})

        captured = {}

        def fake_perform(target, emit_output=True):
            captured["target"] = target
            return {"to": {"number": int(target)}, "from": {}}

        monkeypatch.setattr(s, "_perform_switch", fake_perform)

        creds_reads, config_reads = _install_read_spies(s, monkeypatch)

        s.switch(strategy="best")

        # Best headroom is slot 2.
        assert captured["target"] == "2"
        # No backup credential/config is read more than once for the same slot.
        assert all(c <= 1 for c in creds_reads.values()), dict(creds_reads)
        assert all(c <= 1 for c in config_reads.values()), dict(config_reads)


class TestNextAvailableReadDedup:
    def test_next_available_reads_each_backup_once_and_selects_same_target(
        self, temp_home, monkeypatch
    ):
        s = _make_switcher()
        _seed_three_accounts(s)
        _set_active(temp_home, "a@x.com")  # active = slot 1
        monkeypatch.setattr(s, "_live_session_pids", lambda *a: [])
        monkeypatch.setattr(s, "_active_cc_running", lambda: True)
        # Slot 2 exhausted (100% -> headroom 0, skipped); slot 3 has headroom.
        _patch_usage(monkeypatch, {"1": 50.0, "2": 100.0, "3": 30.0})

        captured = {}

        def fake_perform(target, emit_output=True):
            captured["target"] = target
            return {"to": {"number": int(target)}, "from": {}}

        monkeypatch.setattr(s, "_perform_switch", fake_perform)

        creds_reads, config_reads = _install_read_spies(s, monkeypatch)

        s.switch(strategy="next-available")

        # Rotation from slot 1 -> slot 2 (exhausted, skipped) -> slot 3.
        assert captured["target"] == "3"
        assert all(c <= 1 for c in creds_reads.values()), dict(creds_reads)
        assert all(c <= 1 for c in config_reads.values()), dict(config_reads)

    def test_next_available_target_matches_reference(self, temp_home, monkeypatch):
        """The deduped path lands on the same slot a plain rotation-with-skip
        would: slot 2 exhausted is skipped, slot 3 chosen."""
        s = _make_switcher()
        _seed_three_accounts(s)
        _set_active(temp_home, "b@x.com")  # active = slot 2
        monkeypatch.setattr(s, "_live_session_pids", lambda *a: [])
        monkeypatch.setattr(s, "_active_cc_running", lambda: True)
        # From slot 2 rotate -> slot 3 (exhausted, skip) -> slot 1 (ok).
        _patch_usage(monkeypatch, {"1": 20.0, "2": 40.0, "3": 100.0})

        captured = {}

        def fake_perform(target, emit_output=True):
            captured["target"] = target
            return {"to": {"number": int(target)}, "from": {}}

        monkeypatch.setattr(s, "_perform_switch", fake_perform)
        s.switch(strategy="next-available")
        assert captured["target"] == "1"


# --------------------------------------------------------------------------
# F11 — renamed local must still extract correctly
# --------------------------------------------------------------------------

class TestOAuthLocalRename:
    def test_get_current_account_extracts_email_and_org(self, temp_home):
        s = _make_switcher()
        (temp_home / ".claude.json").write_text(json.dumps({
            "oauthAccount": {
                "emailAddress": "person@x.com",
                "organizationUuid": "org-123",
            }
        }))
        identity = s._get_current_account()
        assert identity == ("person@x.com", "org-123")

    def test_migrate_org_fields_backfills_from_live_and_backup(
        self, temp_home
    ):
        s = _make_switcher()
        # Active account = slot 1 (live config carries org info).
        (temp_home / ".claude.json").write_text(json.dumps({
            "oauthAccount": {
                "emailAddress": "a@x.com",
                "organizationUuid": "live-org",
                "organizationName": "Live Org",
            }
        }))
        data = {
            "activeAccountNumber": 1,
            "lastUpdated": "2024-01-01T00:00:00Z",
            "sequence": [1, 2],
            "accounts": {
                "1": {"email": "a@x.com", "uuid": "u1",
                      "added": "2024-01-01T00:00:00Z"},
                "2": {"email": "b@x.com", "uuid": "u2",
                      "added": "2024-01-02T00:00:00Z"},
            },
        }
        s._write_json(s.sequence_file, data)
        # Backup config for inactive slot 2 carries its own org info.
        s._write_account_config("2", "b@x.com", json.dumps({
            "oauthAccount": {
                "emailAddress": "b@x.com",
                "organizationUuid": "backup-org",
                "organizationName": "Backup Org",
            }
        }))

        s._migrate_org_fields()
        out = s._get_sequence_data()
        assert out["accounts"]["1"]["organizationUuid"] == "live-org"
        assert out["accounts"]["1"]["organizationName"] == "Live Org"
        assert out["accounts"]["2"]["organizationUuid"] == "backup-org"
        assert out["accounts"]["2"]["organizationName"] == "Backup Org"
