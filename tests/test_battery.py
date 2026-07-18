"""Parser tests for battery.py, seeded from a real `dumpsys batterystats` block."""
from battery import (build_uses, parse_packages, parse_power_use, summarize,
                     _uid_to_num)

# Trimmed from an actual `dumpsys batterystats --charged`; the trailing section
# header must not leak into the parsed power block.
STATS = (
    "Statistics since last charge:\n"
    "  Estimated power use (mAh):\n"
    "    Capacity: 5000, Computed drain: 812, actual drain: 790-840\n"
    "    Screen: 300 Excluded from smearing\n"
    "    Uid u0a208: 260 ( cpu=240 wifi=18 gps=2 )\n"
    "    Uid 1000: 55.2 ( cpu=40 sensor=15.2 )\n"
    "    Cell standby: 30 ( radio=30 )\n"
    "    Uid u0a61: 12.4 ( cpu=12.4 )\n"
    "    Idle: 20\n"
    "  All screen wake reasons:\n"          # next section -- parser must stop
    "    Uid u0a999: 9999\n"
)
PKGS = (
    "package:com.instagram.android uid:10208\n"
    "package:com.random.junkcleaner uid:10061\n"
)


def test_uid_token_to_number():
    assert _uid_to_num("u0a208") == 10208      # user 0, app 208
    assert _uid_to_num("u10a5") == 1010005     # secondary user 10
    assert _uid_to_num("1000") == 1000
    assert _uid_to_num("Screen") is None


def test_parses_capacity_and_computed():
    p = parse_power_use(STATS)
    assert p["capacity"] == 5000
    assert p["computed"] == 812


def test_next_section_does_not_leak():
    p = parse_power_use(STATS)
    assert all(name != "u0a999" for _, name, _ in p["entries"])


def test_join_resolves_package_names_and_sorts():
    uses = build_uses(parse_power_use(STATS), parse_packages(PKGS))
    assert uses == sorted(uses, key=lambda u: u.mah, reverse=True)  # biggest first
    apps = [u for u in uses if u.is_app]
    assert apps[0].name == "com.instagram.android" and apps[0].mah == 260
    assert "com.random.junkcleaner" in [u.name for u in uses]


def test_system_uid_labelled_not_flagged_as_app():
    uses = build_uses(parse_power_use(STATS), parse_packages(PKGS))
    sysrow = [u for u in uses if u.name == "Android system"]
    assert sysrow and not sysrow[0].is_app
    assert any(u.name == "Screen" and not u.is_app for u in uses)   # misc kept


def test_unknown_uid_shown_as_system():
    uses = build_uses(parse_power_use(STATS), {})   # empty map -> no app names
    assert any(u.name == "System (uid u0a208)" for u in uses)
    assert not any(u.is_app for u in uses)


def test_summary_flags_high_share_app():
    uses = build_uses(parse_power_use(STATS), parse_packages(PKGS))
    text, kind = summarize(uses, 812)
    assert kind == "warn"                 # 260/812 = 32% >= 25%
    assert "com.instagram.android" in text


def test_summary_good_when_no_app_dominates():
    from battery import BatteryUse
    small = [BatteryUse("Screen", 300, False), BatteryUse("com.foo", 40, True)]
    text, kind = summarize(small, 812)
    assert kind == "good"


def test_summary_empty():
    assert summarize([], None)[1] == "warn"
