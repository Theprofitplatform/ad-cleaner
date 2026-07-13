from pathlib import Path

import pytest

import actions
from actions import (
    ActionLog, ProtectedAppError, can_undo, clean_risky, pause, resume, stop_all,
    undo, uninstall,
)
from scanner import App


class FakeAdb:
    serial = "TEST"

    def __init__(self, admin_blocks=False):
        self.disabled = set()
        self.installed = {"com.random.adware", "com.evil.admin", "com.google.android.gms"}
        self.admin_active = {"com.evil.admin"} if admin_blocks else set()
        self.calls = []

    def shell_text(self, args, timeout=10):
        self.calls.append(args)
        if args[:3] == ["pm", "disable-user", "--user"]:
            self.disabled.add(args[-1]); return "disabled"
        if args[:2] == ["pm", "enable"]:
            self.disabled.discard(args[-1]); return "enabled"
        if args[:3] == ["pm", "uninstall", "--user"]:
            pkg = args[-1]
            if pkg in self.admin_active:
                from adb import AdbError
                raise AdbError("Cannot delete active device admin")
            self.installed.discard(pkg); return "Success"
        if args[:2] == ["dpm", "remove-active-admin"]:
            self.admin_active.clear(); return "Success"
        if args[:3] == ["cmd", "package", "install-existing"]:
            self.installed.add(args[-1]); return "installed"
        if args[:2] == ["am", "force-stop"]:
            return ""
        if args[:2] == ["appops", "set"]:
            return ""
        if args == ["pm", "list", "packages", "-d"]:
            return "".join(f"package:{p}\n" for p in self.disabled)
        if args == ["pm", "list", "packages"]:
            return "".join(f"package:{p}\n" for p in self.installed)
        return ""


@pytest.fixture
def log(tmp_path):
    return ActionLog(tmp_path / "action_log.json")


PROTECTED = App(package="com.google.android.gms", installer="com.android.vending")
ADWARE = App(package="com.random.adware", installer=None, overlay=True)


def test_pause_disables_and_verifies(log):
    adb = FakeAdb()
    app = App(package="com.random.adware", installer=None)
    assert pause(adb, app, log) is True
    assert app.enabled is False
    assert "com.random.adware" in adb.disabled


def test_resume_reenables(log):
    adb = FakeAdb()
    adb.disabled.add("com.random.adware")
    app = App(package="com.random.adware", installer=None, enabled=False)
    assert resume(adb, app, log) is True
    assert app.enabled is True


def test_pause_protected_raises_and_touches_nothing(log):
    adb = FakeAdb()
    with pytest.raises(ProtectedAppError):
        pause(adb, PROTECTED, log)
    assert adb.calls == []  # guard fires before any device command
    assert log.entries == []


def test_uninstall_protected_raises(log):
    adb = FakeAdb()
    with pytest.raises(ProtectedAppError):
        uninstall(adb, PROTECTED, log)
    assert "com.google.android.gms" in adb.installed


def test_uninstall_device_admin_auto_removes_admin(log):
    adb = FakeAdb(admin_blocks=True)
    app = App(package="com.evil.admin", installer=None, device_admin=True,
              admin_component="com.evil.admin/.Receiver")
    assert uninstall(adb, app, log) is True
    assert "com.evil.admin" not in adb.installed
    assert ["dpm", "remove-active-admin", "com.evil.admin/.Receiver"] in adb.calls


def test_stop_all_excludes_protected_and_paused(log):
    adb = FakeAdb()
    paused = App(package="com.some.paused", installer=None, enabled=False)
    stopped, attempted = stop_all(adb, [ADWARE, PROTECTED, paused], log)
    assert attempted == 1 and stopped == 1


def test_stop_all_block_popups_denies_overlay(log):
    adb = FakeAdb()
    stop_all(adb, [ADWARE], log, block_popups=True)
    assert ["appops", "set", "com.random.adware", "SYSTEM_ALERT_WINDOW", "deny"] in adb.calls


def test_undo_pause_reenables(log):
    adb = FakeAdb()
    app = App(package="com.random.adware", installer=None)
    pause(adb, app, log)
    entry = log.recent()[0]
    assert can_undo(entry)
    assert undo(adb, entry, log) is True
    assert "com.random.adware" not in adb.disabled


def test_undo_uninstall_restores(log):
    adb = FakeAdb()
    app = App(package="com.random.adware", installer=None)
    uninstall(adb, app, log)
    assert "com.random.adware" not in adb.installed
    undo(adb, log.recent()[0], log)
    assert "com.random.adware" in adb.installed


def test_force_stop_not_undoable(log):
    adb = FakeAdb()
    stop_all(adb, [ADWARE], log)
    entry = next(e for e in log.entries if e["action"] == "force-stop")
    assert not can_undo(entry)


def test_clean_risky_stops_all_and_pauses_suspicious(log):
    adb = FakeAdb()
    adware = App(package="com.random.adware", installer=None, overlay=True, risk="HIGH")
    booster = App(package="com.play.cleaner", installer="com.android.vending",
                  risk="Medium")  # Play-Store pop-up app -> Medium, must be paused too
    protected = App(package="com.google.android.gms", installer="com.android.vending",
                    risk="HIGH")  # protected -> must be left alone
    low = App(package="com.spotify.music", installer="com.android.vending", risk="Low")
    res = clean_risky(adb, [adware, booster, protected, low], log)
    # stop_all hits all non-protected enabled apps; protected excluded.
    assert res["stopped"] == 3
    # HIGH and Medium get paused; Low and protected do not.
    assert res["acted"] == 2 and res["removed"] is False
    assert "com.random.adware" in adb.disabled
    assert "com.play.cleaner" in adb.disabled
    assert "com.google.android.gms" not in adb.disabled


def test_clean_risky_remove_uninstalls_suspicious(log):
    adb = FakeAdb()
    adware = App(package="com.random.adware", installer=None, overlay=True, risk="HIGH")
    protected = App(package="com.google.android.gms", installer="com.android.vending",
                    risk="HIGH")  # protected -> never removed
    res = clean_risky(adb, [adware, protected], log, remove=True)
    assert res["removed"] is True and res["acted"] == 1
    assert res["packages"] == ["com.random.adware"]
    assert "com.random.adware" not in adb.installed   # actually uninstalled
    assert "com.google.android.gms" in adb.installed  # protected left alone


def test_log_is_appended_and_persisted(tmp_path):
    path = tmp_path / "action_log.json"
    log = ActionLog(path)
    adb = FakeAdb()
    pause(adb, App(package="com.random.adware", installer=None), log)
    # Re-load from disk: entry persisted.
    assert ActionLog(path).entries[0]["package"] == "com.random.adware"
