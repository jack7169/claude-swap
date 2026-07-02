"""Tests for the SMAppService Login Item wrapper.

Never imports the ServiceManagement framework: the pure helpers are tested
directly and the service-backed functions are tested with _main_service mocked.
"""

from __future__ import annotations

from claude_swap import login_item


def test_status_name_maps_known_values():
    assert login_item._status_name(0) == "not-registered"
    assert login_item._status_name(1) == "enabled"
    assert login_item._status_name(2) == "requires-approval"
    assert login_item._status_name(3) == "not-found"


def test_status_name_unknown_value():
    assert login_item._status_name(99) == "unknown"


def test_is_bundled_true_when_frozen(monkeypatch):
    monkeypatch.setattr(login_item.sys, "frozen", "macosx_app", raising=False)
    assert login_item.is_bundled() is True


def test_is_bundled_true_for_app_bundle_path(monkeypatch):
    monkeypatch.delattr(login_item.sys, "frozen", raising=False)
    monkeypatch.setattr(
        login_item.sys, "executable",
        "/Applications/claude-swap.app/Contents/MacOS/python",
    )
    assert login_item.is_bundled() is True


def test_is_bundled_false_for_plain_interpreter(monkeypatch):
    monkeypatch.delattr(login_item.sys, "frozen", raising=False)
    monkeypatch.setattr(login_item.sys, "executable", "/opt/homebrew/bin/python3.12")
    assert login_item.is_bundled() is False


class _FakeService:
    def __init__(self, status_val=1, register_ok=True, err="boom"):
        self._status = status_val
        self._register_ok = register_ok
        self._err = err
        self.calls = []

    def status(self):
        return self._status

    def registerAndReturnError_(self, _none):
        self.calls.append("register")
        return (self._register_ok, None if self._register_ok else self._err)

    def unregisterAndReturnError_(self, _none):
        self.calls.append("unregister")
        return (self._register_ok, None if self._register_ok else self._err)


def test_status_reads_service(monkeypatch):
    monkeypatch.setattr(login_item, "_main_service", lambda: _FakeService(status_val=1))
    assert login_item.status() == "enabled"


def test_status_unavailable_when_no_service(monkeypatch):
    monkeypatch.setattr(login_item, "_main_service", lambda: None)
    assert login_item.status() == "unavailable"


def test_enable_calls_register(monkeypatch):
    svc = _FakeService(register_ok=True)
    monkeypatch.setattr(login_item, "_main_service", lambda: svc)
    assert login_item.enable() == (True, None)
    assert svc.calls == ["register"]


def test_enable_reports_error(monkeypatch):
    svc = _FakeService(register_ok=False, err="denied")
    monkeypatch.setattr(login_item, "_main_service", lambda: svc)
    ok, err = login_item.enable()
    assert ok is False and "denied" in err


def test_disable_calls_unregister(monkeypatch):
    svc = _FakeService(register_ok=True)
    monkeypatch.setattr(login_item, "_main_service", lambda: svc)
    assert login_item.disable() == (True, None)
    assert svc.calls == ["unregister"]


def test_toggle_enables_when_not_enabled(monkeypatch):
    svc = _FakeService(status_val=0)  # not-registered
    monkeypatch.setattr(login_item, "_main_service", lambda: svc)
    login_item.toggle()
    assert svc.calls == ["register"]


def test_toggle_disables_when_enabled(monkeypatch):
    svc = _FakeService(status_val=1)  # enabled
    monkeypatch.setattr(login_item, "_main_service", lambda: svc)
    login_item.toggle()
    assert svc.calls == ["unregister"]


def test_functions_unavailable_without_service(monkeypatch):
    monkeypatch.setattr(login_item, "_main_service", lambda: None)
    assert login_item.enable()[0] is False
    assert login_item.disable()[0] is False
