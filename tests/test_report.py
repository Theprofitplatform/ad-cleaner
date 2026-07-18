import report
from report import render_history_html, render_receipt_html

RECEIPT = {
    "when": "2026-07-13 14:30:02", "model": "SM G991B", "android": "14",
    "stopped": 12, "acted": 4, "removed": True, "popups_blocked": 6,
    "packages": ["com.random.adware", "com.evil.pop"],
    "dns": "On — AdGuard", "freed_gb": 1.2,
}


def test_receipt_includes_counts_packages_and_freed_space():
    out = render_receipt_html(RECEIPT)
    assert "com.random.adware" in out
    assert "1.2 GB" in out
    assert "6" in out            # pop-ups blocked
    assert "Removed" in out


def test_receipt_pause_mode_states_no_space_freed():
    out = render_receipt_html({**RECEIPT, "removed": False, "freed_gb": 0})
    assert "no space freed" in out.lower()
    assert "Paused" in out


def test_receipt_uninstall_mode_zero_freed_is_not_contradictory():
    out = render_receipt_html({**RECEIPT, "removed": True, "freed_gb": 0})
    assert "still installed" not in out
    assert "paused" not in out.lower()
    assert "Removed" in out


def test_receipt_verification_line():
    out = render_receipt_html({**RECEIPT, "risky_before": 6, "risky_after": 0})
    assert "6 risky app(s) found → 0 still active" in out
    left = render_receipt_html({**RECEIPT, "risky_before": 6, "risky_after": 1,
                                "remaining": ["Free Gift <Deluxe>"]})
    assert "1 still active" in left
    assert "Free Gift &lt;Deluxe&gt;" in left           # escaped
    assert "Checked after cleaning" not in render_receipt_html(RECEIPT)


def test_receipt_installs_blocked_line():
    out = render_receipt_html({**RECEIPT, "installs_blocked": 2})
    assert "Blocked from installing other apps" in out and "2 app(s)" in out
    assert "Blocked from installing" not in render_receipt_html(RECEIPT)


def test_history_escapes_hostile_package_names():
    out = render_history_html(
        [{"time": "t", "package": "<script>x</script>", "action": "pause", "result": "ok"}])
    assert "<script>x" not in out       # not rendered as a live tag
    assert "&lt;script&gt;" in out      # escaped instead
    assert "<table" in out


def test_intake_shows_managed_phone():
    out = report.render_intake_html({"app_count": 0, "managed": "com.mdm.corp (device owner)"})
    assert "Managed by" in out and "com.mdm.corp (device owner)" in out
    assert "Managed by" not in report.render_intake_html({"app_count": 0})


def test_shop_header_on_receipt_and_intake():
    r = {"when": "t", "stopped": 1, "acted": 0,
         "shop_name": "Phone Fix <Bros>", "shop_contact": "07 5555 5555 · fix.example"}
    out = report.render_receipt_html(r)
    assert "Phone Fix &lt;Bros&gt;" in out and "07 5555 5555" in out
    intake = report.render_intake_html({"app_count": 0, "shop_name": "Phone Fix <Bros>"})
    assert "Phone Fix &lt;Bros&gt;" in intake
    assert "Phone Fix" not in report.render_receipt_html({"when": "t"})
