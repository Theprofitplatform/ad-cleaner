from datetime import datetime
from pathlib import Path

import pytest

import scanner
from scanner import (
    App, build_inventory, looks_random, parse_device_admins, parse_disabled,
    parse_enabled_accessibility, parse_first_install, parse_launcher_packages,
    parse_overlay_allowed, parse_perms, parse_role_holders, parse_third_party,
    prettify_label, score_app,
)

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2024, 6, 1)


@pytest.fixture(autouse=True)
def _isolated_blocklist(monkeypatch, tmp_path):
    """build_inventory reads adcleaner_data/blocklist.txt via adb.data_dir; point
    it at an empty tmp dir so tests never see the machine's real blocklist, and
    restore the seed blocklist afterwards."""
    import adb
    monkeypatch.setattr(adb, "data_dir", lambda: tmp_path)
    yield
    scanner.reset_blocklist()


def fx(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


class FakeAdb:
    """Serves canned fixture output for the commands build_inventory issues."""
    serial = "FIXTURE"

    def shell_text(self, args, timeout=10):
        if args == ["pm", "list", "packages", "-3", "-i"]:
            return fx("packages_i.txt")
        if args == ["pm", "list", "packages", "-d"]:
            return fx("disabled.txt")
        if args == ["appops", "query-op", "SYSTEM_ALERT_WINDOW", "allow"]:
            return fx("appops_overlay.txt")
        if args == ["dumpsys", "device_policy"]:
            return fx("device_policy.txt")
        if args[:2] == ["dumpsys", "package"]:
            return fx(f"dumpsys_{args[2]}.txt")
        if args[:2] == ["cmd", "package"]:  # query-activities (launchers)
            return fx("launchers.txt")
        if args[:2] == ["dumpsys", "notification"]:
            return fx("dumpsys_notification.txt")
        # Real device subcommand is `get-role-holders` (NOT `holders`); serving it
        # only under the correct name catches a regression to the broken command.
        if args[:3] == ["cmd", "role", "get-role-holders"]:
            return "com.random.freegift\n" if args[3] == "android.app.role.BROWSER" else ""
        return ""


# --- Parser tests -----------------------------------------------------------

def test_parse_third_party():
    got = parse_third_party(fx("packages_i.txt"))
    assert got["com.spotify.music"] == "com.android.vending"
    assert got["com.random.freegift"] is None  # installer=null -> None
    assert len(got) == 4


def test_parse_disabled():
    assert parse_disabled(fx("disabled.txt")) == {"com.oldstuff.calc"}


def test_parse_overlay_allowed_bare_list():
    assert parse_overlay_allowed(fx("appops_overlay.txt")) == {"com.random.freegift"}


def test_parse_overlay_allowed_grouped_form():
    grouped = "Uid 10234:\n  Package com.foo.bar:\n    SYSTEM_ALERT_WINDOW: allow\n"
    assert parse_overlay_allowed(grouped) == {"com.foo.bar"}


def test_parse_device_admins():
    admins = parse_device_admins(fx("device_policy.txt"))
    assert admins == {
        "com.evil.deviceadmin": "com.evil.deviceadmin/com.evil.deviceadmin.AdminReceiver"
    }


def test_parse_first_install():
    dt = parse_first_install(fx("dumpsys_com.random.freegift.txt"))
    assert dt == datetime(2024, 5, 20, 14, 22, 10)
    assert parse_first_install("no date here") is None


def test_parse_perms():
    perms = parse_perms(fx("dumpsys_com.random.freegift.txt"))
    assert perms["request_install"] and perms["overlay_perm"]
    assert not perms["accessibility"]
    admin_perms = parse_perms(fx("dumpsys_com.evil.deviceadmin.txt"))
    assert admin_perms["accessibility"]


def test_parse_perms_sensitive():
    text = ("requested permissions:\n"
            "  android.permission.READ_SMS\n"
            "  android.permission.ACCESS_FINE_LOCATION\n"
            "  android.permission.CAMERA\n")
    perms = parse_perms(text)
    assert "Read your text messages" in perms["sensitive_perms"]
    assert "Track your location" in perms["sensitive_perms"]
    assert "Use the camera" in perms["sensitive_perms"]
    assert perms["sensitive_data"] is True  # READ_SMS counts as personal-data access


def test_parse_launcher_packages():
    out = "2 activities found:\n  com.foo/.Main\n  com.bar/com.bar.Home\n"
    assert parse_launcher_packages(out) == {"com.foo", "com.bar"}


def test_hidden_app_scored_and_reasoned():
    app = App(package="com.sneaky.hidden", installer=None, hidden=True,
              first_install=datetime(2020, 1, 1))
    score_app(app, NOW)
    assert scanner.REASONS["hidden"] in app.reasons
    assert app.score == 45  # sideloaded 25 + hidden 20


def test_sensitive_data_scored():
    app = App(package="com.spy.tool", installer="com.android.vending",
              sensitive_data=True, first_install=datetime(2020, 1, 1))
    score_app(app, NOW)
    assert scanner.REASONS["sensitive_data"] in app.reasons
    assert app.score == 10


def test_parse_enabled_accessibility():
    assert parse_enabled_accessibility("com.a/.S:com.b/.T") == {"com.a", "com.b"}
    assert parse_enabled_accessibility("null") == set()
    assert parse_enabled_accessibility("") == set()


def test_parse_role_holders():
    assert parse_role_holders("com.foo.browser\n") == ["com.foo.browser"]
    # AOSP joins multiple holders with ';' on one line.
    assert parse_role_holders("com.foo.sms;com.bar.sms\n") == ["com.foo.sms", "com.bar.sms"]
    assert parse_role_holders("No holders.") == []
    assert parse_role_holders("") == []


def test_active_accessibility_scored():
    app = App(package="com.evil.x", installer=None, active_accessibility=True,
              first_install=datetime(2020, 1, 1))
    score_app(app, NOW)
    assert scanner.REASONS["active_accessibility"] in app.reasons
    assert app.score == 50  # sideloaded 25 + active_accessibility 25


def test_role_hijack_scored_with_named_defaults():
    app = App(package="com.evil.home", installer=None,
              hijacked_roles=["home screen", "browser"], first_install=datetime(2020, 1, 1))
    score_app(app, NOW)
    assert any("Took over a system default (home screen, browser)" in r for r in app.reasons)
    assert app.score == 40  # sideloaded 25 + role_hijack 15


def test_nuisance_cleaner_from_store_is_medium():
    # A Play-Store cleaner with no dangerous perms would otherwise score 0.
    app = App(package="com.phone.cleaner.shineapps", installer="com.android.vending",
              label="cleaner", first_install=datetime(2020, 1, 1))
    score_app(app, NOW)
    assert scanner.REASONS["nuisance"] in app.reasons
    assert app.risk == "Medium" and app.score == 30


def test_blocklisted_app_forced_high():
    app = App(package="com.cleanmaster.mguard", installer="com.android.vending",
              first_install=datetime(2020, 1, 1))
    score_app(app, NOW)
    assert app.risk == "HIGH"
    assert scanner.BLOCKED_REASON in app.reasons


# --- Label + heuristic tests ------------------------------------------------

def test_prettify_label():
    assert prettify_label("com.foo.flashlight") == "Flashlight (com.foo.flashlight)"


@pytest.mark.parametrize("pkg", ["com.a.xkwptqzr", "com.a.a8f3k2j9x"])
def test_looks_random_true(pkg):
    assert looks_random(pkg)


@pytest.mark.parametrize("pkg", ["com.spotify.music", "com.foo.flashlight", "com.whatsapp"])
def test_looks_random_false(pkg):
    assert not looks_random(pkg)


# --- Scoring tests (BUILD_PLAN 4.2 table) -----------------------------------

def test_overlay_plus_sideloaded_both_listed_high():
    """Acceptance #3: overlay + sideloaded app is HIGH with both reasons."""
    app = App(package="com.random.freegift", installer=None, overlay=True,
              first_install=datetime(2024, 5, 20))
    score_app(app, NOW)
    assert app.risk == "HIGH"
    assert scanner.REASONS["overlay"] in app.reasons
    assert scanner.REASONS["sideloaded"] in app.reasons


def test_clean_store_app_low():
    app = App(package="com.spotify.music", installer="com.android.vending",
              first_install=datetime(2020, 1, 15))
    score_app(app, NOW)
    assert app.risk == "Low" and app.score == 0


def test_spoof_forced_high_even_with_low_score():
    # First-party name from the generic sideload installer -> impostor, forced HIGH.
    app = App(package="com.google.android.fakecore",
              installer="com.google.android.packageinstaller",
              first_install=datetime(2019, 3, 3))
    score_app(app, NOW)
    assert app.risk == "HIGH"
    assert scanner.SPOOF_REASON == app.reasons[0]


def test_genuine_oem_preload_is_protected_and_low():
    # Regression (real Samsung device): a first-party app with a null installer is
    # a genuine preload, not sideloaded adware -- protected, never scored risky.
    app = App(package="com.sec.android.app.kidshome", installer=None, hidden=True,
              first_install=datetime(2024, 5, 20))
    score_app(app, NOW)
    assert app.protected and app.risk == "Low" and app.reasons == []


def test_threshold_boundaries():
    # Exactly medium threshold.
    app = App(package="com.x.y", installer=None, first_install=datetime(2024, 5, 20))
    score_app(app, NOW)  # sideloaded 25 + recent 15 = 40
    assert app.score == 40 and app.risk == "Medium"


# --- End-to-end inventory over fixtures -------------------------------------

def test_build_inventory_scores_whole_device():
    apps = {a.package: a for a in build_inventory(FakeAdb(), now=NOW)}
    assert apps["com.spotify.music"].risk == "Low"
    assert apps["com.random.freegift"].risk == "HIGH"
    assert apps["com.evil.deviceadmin"].risk == "HIGH"
    assert apps["com.evil.deviceadmin"].device_admin
    assert apps["com.evil.deviceadmin"].admin_component.endswith("AdminReceiver")
    # deviceadmin has no launcher entry -> flagged hidden; others have icons.
    assert apps["com.evil.deviceadmin"].hidden
    assert not apps["com.spotify.music"].hidden
    assert apps["com.google.android.fakecore"].risk == "HIGH"
    # Highest risk sorts first.
    ordered = build_inventory(FakeAdb(), now=NOW)
    assert ordered[0].score >= ordered[-1].score


def test_build_inventory_detects_role_hijack():
    # Regression: the role query must use `cmd role get-role-holders`. The old
    # `cmd role holders` is rejected by the device ("Unknown command"), which
    # silently zeroed every UI-takeover detection (home/browser/sms/dialer).
    apps = {a.package: a for a in build_inventory(FakeAdb(), now=NOW)}
    assert apps["com.random.freegift"].hijacked_roles == ["browser"]


def test_user_blocklist_file_loaded_and_deletions_apply(tmp_path):
    # utf-8-sig: a BOM'd file (typical of Windows editors) must not poison the
    # first entry; an inline comment must be stripped.
    (tmp_path / "blocklist.txt").write_text(
        "com.random.freegift   # verified junk\n", encoding="utf-8-sig")
    apps = {a.package: a for a in build_inventory(FakeAdb(), now=NOW)}
    assert apps["com.random.freegift"].risk == "HIGH"
    assert scanner.BLOCKED_REASON in apps["com.random.freegift"].reasons
    # Deleting the line takes effect on the very next scan (no restart needed).
    (tmp_path / "blocklist.txt").write_text("", encoding="utf-8")
    apps = {a.package: a for a in build_inventory(FakeAdb(), now=NOW)}
    assert scanner.BLOCKED_REASON not in apps["com.random.freegift"].reasons


def test_unreadable_blocklist_file_does_not_kill_the_scan(tmp_path):
    # A UTF-16 file (PowerShell's default '>>' encoding) must be skipped, not
    # crash build_inventory with UnicodeDecodeError.
    (tmp_path / "blocklist.txt").write_bytes("com.foo.bar\n".encode("utf-16"))
    apps = build_inventory(FakeAdb(), now=NOW)
    assert apps  # scan completed on the bundled seed alone


def test_build_inventory_marks_overlay_from_appops():
    apps = {a.package: a for a in build_inventory(FakeAdb(), now=NOW)}
    assert apps["com.random.freegift"].overlay
    assert not apps["com.spotify.music"].overlay


def test_parse_notification_counts():
    counts = scanner.parse_notification_counts(fx("dumpsys_notification.txt"))
    assert counts == {"com.random.freegift": 5, "com.whatsapp": 1}
    assert scanner.parse_notification_counts("") == {}


def test_notif_spam_scored():
    app = App(package="com.random.freegift", installer=None,
              first_install=datetime(2020, 1, 1), notif_count=5)
    score_app(app, NOW)
    assert scanner.REASONS["notif_spam"] in app.reasons
