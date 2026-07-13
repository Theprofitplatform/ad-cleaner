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


def test_history_escapes_hostile_package_names():
    out = render_history_html(
        [{"time": "t", "package": "<script>x</script>", "action": "pause", "result": "ok"}])
    assert "<script>x" not in out       # not rendered as a live tag
    assert "&lt;script&gt;" in out      # escaped instead
    assert "<table" in out
