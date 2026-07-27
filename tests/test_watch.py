"""Parser + session tests for watch.py.

The fixture is hand-built to the documented WindowManager dump layout (window
blocks, mAttrs ty=, mCurrentFocus/mFocusedApp) rather than captured off a
handset -- so real-device format drift is the thing to confirm in a field test,
not something these tests can prove.
"""
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import watch
from scanner import App, score_app
from watch import (
    KEEP_DAYS, WatchEvent, WatchSession, forget, load_caught, parse_focus,
    parse_overlays, record,
)

FIXTURES = Path(__file__).parent / "fixtures"
DUMP = (FIXTURES / "dumpsys_window.txt").read_text(encoding="utf-8")
NOW = datetime(2026, 7, 27, 15, 42)


@pytest.fixture(autouse=True)
def _isolated_log(monkeypatch, tmp_path):
    """Never touch the machine's real watch_log.json."""
    import adb
    monkeypatch.setattr(adb, "data_dir", lambda: tmp_path)


# --- parsers ---------------------------------------------------------------

def test_finds_the_overlay_owner():
    assert "com.adware.pop" in parse_overlays(DUMP)


def test_parked_invisible_overlay_is_not_evidence():
    # com.sneaky.hidden holds a 1x1 overlay with no surface -- nobody sees it.
    assert "com.sneaky.hidden" not in parse_overlays(DUMP)


def test_toasts_and_keyboards_are_not_overlays():
    found = parse_overlays(DUMP)
    assert "com.chat.app" not in found                      # ty=TOAST
    assert "com.samsung.android.honeyboard" not in found     # ty=INPUT_METHOD


def test_ordinary_app_windows_are_not_overlays():
    # Chrome owns a BASE_APPLICATION window and an APPLICATION_STARTING splash.
    assert "com.android.chrome" not in parse_overlays(DUMP)


def test_system_overlay_is_parsed_then_filtered_by_the_session():
    # SystemUI genuinely owns an APPLICATION_OVERLAY (screen decor), so the
    # parser reports it and the ignore rule -- not the parser -- drops it.
    assert "com.android.systemui" in parse_overlays(DUMP)
    assert WatchSession().update(DUMP, NOW) == [
        WatchEvent(NOW, "com.adware.pop", "overlay")]


def test_focus_package():
    assert parse_focus(DUMP) == "com.android.chrome"


def test_focus_falls_back_to_focused_app():
    # Pulling the shade down leaves a focus window with no package in it.
    no_current = DUMP.replace(
        "mCurrentFocus=Window{e10a3f5 u0 com.android.chrome/"
        "com.google.android.apps.chrome.Main}",
        "mCurrentFocus=Window{a0b1c2d u0 NotificationShade}")
    assert parse_focus(no_current) == "com.android.chrome"


def test_focus_none_when_unreadable():
    assert parse_focus("") is None
    assert parse_focus("mCurrentFocus=null") is None


def test_empty_dump_is_harmless():
    assert parse_overlays("") == set()
    assert WatchSession().update("", NOW) == []


# --- session ---------------------------------------------------------------

def test_overlay_reported_once_while_it_stays_up():
    s = WatchSession()
    assert [e.package for e in s.update(DUMP, NOW)] == ["com.adware.pop"]
    assert s.update(DUMP, NOW) == []


def test_overlay_reported_again_after_it_goes_and_returns():
    s = WatchSession()
    s.update(DUMP, NOW)
    s.update(DUMP.replace("package=com.adware.pop", "package=com.other.app"), NOW)
    assert [e.package for e in s.update(DUMP, NOW)] == ["com.adware.pop"]


def test_first_poll_does_not_report_the_app_already_in_front():
    assert not [e for e in WatchSession().update(DUMP, NOW) if e.kind == "focus"]


def test_focus_change_to_a_third_party_app_is_reported():
    s = WatchSession()
    s.update(DUMP, NOW)
    moved = DUMP.replace("com.android.chrome/com.google.android.apps.chrome.Main",
                         "com.random.gift/.MainActivity")
    events = [e for e in s.update(moved, NOW) if e.kind == "focus"]
    assert [e.package for e in events] == ["com.random.gift"]


def test_focus_change_between_system_apps_is_ignored():
    s = WatchSession()
    s.update(DUMP, NOW)
    moved = DUMP.replace("com.android.chrome/com.google.android.apps.chrome.Main",
                         "com.android.settings/.Settings")
    assert s.update(moved, NOW) == []


# --- evidence log ----------------------------------------------------------

def test_overlay_is_recorded_and_a_bare_focus_change_is_not():
    record([WatchEvent(NOW, "com.adware.pop", "overlay"),
            WatchEvent(NOW, "com.random.gift", "focus")], NOW)
    assert load_caught(NOW) == {"com.adware.pop": NOW}


def test_operator_flag_is_recorded():
    record([WatchEvent(NOW, "com.random.gift", "flagged")], NOW)
    assert load_caught(NOW) == {"com.random.gift": NOW}


def test_evidence_expires():
    record([WatchEvent(NOW, "com.adware.pop", "overlay")], NOW)
    assert load_caught(NOW + timedelta(days=KEEP_DAYS + 1)) == {}


def test_latest_sighting_wins():
    later = NOW + timedelta(minutes=5)
    record([WatchEvent(NOW, "com.adware.pop", "overlay")], NOW)
    record([WatchEvent(later, "com.adware.pop", "overlay")], later)
    assert load_caught(later) == {"com.adware.pop": later}


def test_log_is_capped_per_package():
    for i in range(watch.MAX_PER_PKG + 10):
        record([WatchEvent(NOW + timedelta(seconds=i), "com.adware.pop", "overlay")], NOW)
    entries = watch._read(watch.log_path())["com.adware.pop"]
    assert len(entries) == watch.MAX_PER_PKG


def test_forget_clears_one_package():
    record([WatchEvent(NOW, "com.adware.pop", "overlay")], NOW)
    assert forget("com.adware.pop")
    assert load_caught(NOW) == {}
    assert not forget("com.never.seen")


def test_missing_and_corrupt_logs_read_empty():
    assert load_caught(NOW) == {}
    watch.log_path().write_text("not json", encoding="utf-8")
    assert load_caught(NOW) == {}


# --- scoring ---------------------------------------------------------------

def test_caught_live_alone_is_high_and_names_the_time():
    app = App(package="com.quiet.adware", installer="com.android.vending",
              first_install=datetime(2020, 1, 1), caught_live=NOW)
    score_app(app, NOW)
    assert app.risk == "HIGH", (app.score, app.reasons)
    assert app.reasons[0].startswith("Caught drawing a pop-up")
    assert "3:42 PM" in app.reasons[0]


def test_household_app_drawing_bubbles_is_waived():
    app = App(package="com.facebook.orca", installer="com.android.vending",
              first_install=datetime(2020, 1, 1), caught_live=NOW)
    score_app(app, NOW)
    assert app.risk == "Low", (app.score, app.reasons)
