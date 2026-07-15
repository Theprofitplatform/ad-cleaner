from pathlib import Path

import pytest

import actions
import playstore
from actions import (
    ActionLog, DNS_PROVIDERS, ProtectedAppError, backup_apk, block_notifications, can_undo,
    clean_risky, clear_caches, clear_private_dns, disable_accessibility, fix_role, pause,
    read_private_dns, reboot, reset_app_data, restrict_background, resume, set_private_dns,
    stop_all, undo, uninstall, will_clean,
)
from adb import AdbError
from scanner import App, REASONS, STALKER_REASON


class FakeAdb:
    serial = "TEST"

    def __init__(self, admin_blocks=False):
        self.disabled = set()
        self.installed = {"com.random.adware", "com.evil.admin", "com.google.android.gms"}
        self.admin_active = {"com.evil.admin"} if admin_blocks else set()
        self.a11y = ""
        self.pulled = []
        self.rebooted = False
        self.calls = []
        self.commands = []
        self.globals = {}

    def pull(self, remote, local, timeout=120):
        self.pulled.append((remote, local))
        Path(local).write_text("apk")
        return "pulled"

    def reboot(self, timeout=10):
        self.rebooted = True
        return ""

    def shell_text(self, args, timeout=10):
        self.calls.append(args)
        self.commands.append(" ".join(args))
        if args[:4] == ["settings", "get", "secure", "enabled_accessibility_services"]:
            return self.a11y
        if args[:4] == ["settings", "put", "secure", "enabled_accessibility_services"]:
            self.a11y = args[4]; return ""
        if args[:4] == ["settings", "put", "secure", "accessibility_enabled"]:
            return ""
        if args[:3] == ["pm", "clear", "--user"]:
            return "Success"
        if args[:2] == ["pm", "path"]:
            return f"package:/data/app/{args[-1]}/base.apk" if args[-1] in self.installed else ""
        if args[:3] == ["cmd", "role", "add-role-holder"]:
            self.role_holder = args[-1]; return ""
        if args[:3] == ["cmd", "role", "get-role-holders"]:
            return getattr(self, "role_holder", "")
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
        if args[:3] == ["settings", "get", "global"]:
            return self.globals.get(args[3], "null")
        if args[:3] == ["settings", "put", "global"]:
            self.globals[args[3]] = args[4]; return ""
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


def test_clean_risky_skips_nuisance_name_only_medium(log):
    # A Medium whose ONLY evidence is a junk-looking name (AVG's real package id
    # is com.antivirus) must never be auto-cleaned -- it stays flagged for a
    # human. A Medium with a corroborating signal is still cleaned.
    adb = FakeAdb()
    name_only = App(package="com.phone.cleaner", installer="com.android.vending",
                    risk="Medium", reasons=[REASONS["nuisance"]])
    corroborated = App(package="com.junk.cleaner", installer="com.android.vending",
                       overlay=True, risk="Medium",
                       reasons=[REASONS["nuisance"], REASONS["overlay"]])
    assert not will_clean(name_only)
    assert will_clean(corroborated)
    res = clean_risky(adb, [name_only, corroborated], log)
    assert res["acted"] == 1
    assert "com.junk.cleaner" in adb.disabled
    assert "com.phone.cleaner" not in adb.disabled


def test_will_clean_ignores_play_not_listed_reason_in_nuisance_fence():
    # The Play-lookup feature appends NOT_LISTED_REASON to a.reasons for
    # display. That must not defeat the nuisance-only fence (Fix 1).
    name_only = App(package="com.phone.cleaner", installer="com.android.vending",
                    risk="Medium", reasons=[REASONS["nuisance"]])
    assert not will_clean(name_only)

    name_only_not_listed = App(package="com.phone.cleaner2", installer="com.android.vending",
                               risk="Medium",
                               reasons=[REASONS["nuisance"], playstore.NOT_LISTED_REASON])
    assert not will_clean(name_only_not_listed)  # still fenced

    corroborated = App(package="com.junk.cleaner", installer="com.android.vending",
                       overlay=True, risk="Medium",
                       reasons=[REASONS["nuisance"], REASONS["overlay"]])
    assert will_clean(corroborated)  # real signal present -> not fenced


def test_will_clean_excludes_stalkerware(log):
    # Victim safety: auto-pausing hidden tracking apps can alert the abuser
    # who installed them, so shop mode / one-click clean must never touch
    # them unattended (Fix 2).
    adb = FakeAdb()
    stalker = App(package="com.hidden.tracker", installer=None, risk="HIGH",
                  reasons=[STALKER_REASON])
    assert not will_clean(stalker)
    res = clean_risky(adb, [stalker], log)
    assert res["acted"] == 0
    assert "com.hidden.tracker" not in adb.disabled


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


def test_disable_accessibility_removes_only_target(log):
    adb = FakeAdb()
    adb.a11y = "com.evil.admin/.Svc:com.good.reader/.Svc"
    assert disable_accessibility(adb, "com.evil.admin", log) is True
    assert adb.a11y == "com.good.reader/.Svc"


def test_disable_accessibility_last_one_sets_null(log):
    adb = FakeAdb()
    adb.a11y = "com.evil.admin/.Svc"
    disable_accessibility(adb, "com.evil.admin", log)
    assert adb.a11y == "null"


def test_uninstall_neutralises_accessibility_first(log):
    adb = FakeAdb()
    adb.a11y = "com.evil.admin/.Svc"
    app = App(package="com.evil.admin", installer=None, active_accessibility=True)
    assert uninstall(adb, app, log) is True
    assert adb.a11y == "null"                      # turned off before removal
    assert "com.evil.admin" not in adb.installed


def test_reset_app_data(log):
    adb = FakeAdb()
    app = App(package="com.random.adware", installer=None)
    assert reset_app_data(adb, app, log) is True
    assert ["pm", "clear", "--user", "0", "com.random.adware"] in adb.calls


def test_reset_app_data_protected_raises(log):
    adb = FakeAdb()
    with pytest.raises(ProtectedAppError):
        reset_app_data(adb, PROTECTED, log)


def test_backup_apk_pulls_to_dest(tmp_path):
    adb = FakeAdb()
    app = App(package="com.random.adware", installer=None)
    saved = backup_apk(adb, app, tmp_path)
    assert len(saved) == 1 and saved[0].endswith("com.random.adware.apk")
    assert adb.pulled and Path(saved[0]).exists()


def test_reboot(log):
    adb = FakeAdb()
    assert reboot(adb, log) is True
    assert adb.rebooted


def test_clear_caches_runs_and_logs(log):
    adb = FakeAdb()
    assert clear_caches(adb, log) is True
    assert ["pm", "trim-caches", "9999999999999"] in adb.calls
    assert log.entries[-1]["action"] == "clear-cache"


def test_log_is_appended_and_persisted(tmp_path):
    path = tmp_path / "action_log.json"
    log = ActionLog(path)
    adb = FakeAdb()
    pause(adb, App(package="com.random.adware", installer=None), log)
    # Re-load from disk: entry persisted.
    assert ActionLog(path).entries[0]["package"] == "com.random.adware"


def test_read_private_dns_defaults_to_off(log):
    adb = FakeAdb()
    assert read_private_dns(adb) == ("off", "")


def test_set_private_dns_writes_verifies_and_logs(log):
    adb = FakeAdb()
    host = DNS_PROVIDERS["AdGuard — blocks ads + trackers"]
    assert set_private_dns(adb, host, log) is True
    assert adb.globals["private_dns_mode"] == "hostname"
    assert adb.globals["private_dns_specifier"] == host
    assert read_private_dns(adb) == ("hostname", host)
    assert log.entries[-1]["action"] == "set-dns"


def test_set_private_dns_rejects_bad_hostname(log):
    adb = FakeAdb()
    with pytest.raises(ValueError):
        set_private_dns(adb, "not a host!", log)
    assert "private_dns_mode" not in adb.globals   # nothing written


def test_clear_private_dns_turns_off(log):
    adb = FakeAdb()
    set_private_dns(adb, "dns.adguard.com", log)
    assert clear_private_dns(adb, log) is True
    assert read_private_dns(adb) == ("off", "")
    assert log.entries[-1]["action"] == "clear-dns"


def test_clean_risky_reports_popups_blocked(log):
    adb = FakeAdb()
    adware = App(package="com.random.adware", installer=None, overlay=True, risk="HIGH")
    quiet = App(package="com.play.cleaner", installer="com.android.vending",
                overlay=False, risk="Medium")
    res = clean_risky(adb, [adware, quiet], log)
    assert res["popups_blocked"] == 1     # only the overlay app is denied


def test_fix_role_hands_role_to_first_installed_stock_app(log):
    adb = FakeAdb()
    adb.installed |= {"com.android.chrome"}
    restored = fix_role(adb, "android.app.role.BROWSER", "com.random.freegift", log)
    assert restored == "com.android.chrome"
    assert ("cmd role add-role-holder --user 0 android.app.role.BROWSER "
            "com.android.chrome") in adb.commands
    entry = log.recent()[0]
    assert entry["action"] == "fix-role" and entry["previous"] == "com.random.freegift"
    assert can_undo(entry)


def test_fix_role_returns_none_when_no_stock_candidate(log):
    adb = FakeAdb()   # no browser installed
    assert fix_role(adb, "android.app.role.BROWSER", "com.random.freegift", log) is None


def test_undo_fix_role_reinstates_previous_holder(log):
    adb = FakeAdb()
    adb.installed |= {"com.android.chrome"}
    fix_role(adb, "android.app.role.BROWSER", "com.random.freegift", log)
    undo(adb, log.recent()[0], log)
    assert ("cmd role add-role-holder --user 0 android.app.role.BROWSER "
            "com.random.freegift") in adb.commands


def test_block_notifications_prefers_pm_revoke(log):
    adb = FakeAdb()
    assert block_notifications(adb, "com.random.freegift", log)
    assert any(c.startswith("pm revoke com.random.freegift "
                            "android.permission.POST_NOTIFICATIONS")
               for c in adb.commands)
    entry = log.recent()[0]
    assert entry["action"] == "block-notifications" and can_undo(entry)


def test_block_notifications_falls_back_to_appops(log):
    class OldAdb(FakeAdb):
        def shell_text(self, args, timeout=10):
            if args[:2] == ["pm", "revoke"]:
                raise AdbError("Unknown permission")   # Android <13
            return super().shell_text(args, timeout)
    adb = OldAdb()
    assert block_notifications(adb, "com.random.freegift", log)
    assert "appops set com.random.freegift POST_NOTIFICATION ignore" in adb.commands


def test_restrict_background_uses_netpolicy_and_undoes(log):
    adb = FakeAdb()
    assert restrict_background(adb, "com.random.freegift", 10231, log)
    assert "cmd netpolicy add restrict-background-blacklist 10231" in adb.commands
    entry = log.recent()[0]
    assert can_undo(entry)
    undo(adb, entry, log)
    assert "cmd netpolicy remove restrict-background-blacklist 10231" in adb.commands


def test_restrict_background_refuses_non_app_uid(log):
    adb = FakeAdb()
    with pytest.raises(ProtectedAppError):
        restrict_background(adb, "com.x", 0, log)
    assert not any("netpolicy" in c for c in adb.commands)
