"""Printable HTML reports: a post-clean receipt + a full action-log export.

Pure functions (no device I/O) so they unit-test in isolation. Every value
that originates from the phone (package names, log fields) is HTML-escaped --
a malicious app name can never inject markup into a report opened in a browser.
"""

import html

_STYLE = (
    "body{font-family:'Segoe UI',Arial,sans-serif;margin:32px;color:#111827}"
    "h1{font-size:20px;margin:0 0 4px}h2{font-size:15px;margin:18px 0 6px}"
    "table{border-collapse:collapse;width:100%}"
    "td,th{border:1px solid #e5e7eb;padding:6px 10px;text-align:left;font-size:13px}"
    "th{background:#f1f5f9}.muted{color:#6b7280}li{font-size:13px}"
)


def _html_page(title, body):
    t = html.escape(title)
    return (f"<!doctype html><html><head><meta charset='utf-8'><title>{t}</title>"
            f"<style>{_STYLE}</style></head><body>{body}</body></html>")


def render_receipt_html(receipt):
    """Render a one-page receipt for a single clean. See plan for the dict shape."""
    r = receipt
    verb = "Removed" if r.get("removed") else "Paused"
    freed = r.get("freed_gb") or 0
    if freed > 0:
        freed_line = f"<p><b>Space freed:</b> {freed} GB</p>"
    elif r.get("removed"):
        freed_line = "<p class='muted'>No measurable change in free space.</p>"
    else:
        freed_line = ("<p class='muted'>Apps were paused (still installed) — "
                      "no space freed.</p>")
    pkgs = r.get("packages") or []
    pkg_block = ""
    if pkgs:
        items = "".join(f"<li>{html.escape(p)}</li>" for p in pkgs)
        pkg_block = f"<h2>{verb} apps</h2><ul>{items}</ul>"
    body = (
        "<h1>Ad Cleaner — clean receipt</h1>"
        f"<p class='muted'>{html.escape(r.get('when', ''))} &middot; "
        f"{html.escape(r.get('model', ''))} &middot; "
        f"Android {html.escape(str(r.get('android', '')))}</p>"
        f"<p><b>Apps closed:</b> {r.get('stopped', 0)}</p>"
        f"<p><b>Pop-up permissions blocked:</b> {r.get('popups_blocked', 0)}</p>"
        f"<p><b>{verb}:</b> {r.get('acted', 0)} risky app(s)</p>"
        f"<p><b>Ad blocking (Private DNS):</b> {html.escape(str(r.get('dns', 'Off')))}</p>"
        f"{freed_line}{pkg_block}"
    )
    return _html_page("Ad Cleaner receipt", body)


def render_history_html(entries):
    """Render the whole action log (newest-first list of dicts) as a table."""
    rows = "".join(
        "<tr>"
        f"<td>{html.escape(e.get('time', ''))}</td>"
        f"<td>{html.escape(e.get('package', ''))}</td>"
        f"<td>{html.escape(e.get('action', ''))}</td>"
        f"<td>{html.escape(e.get('result', ''))}</td>"
        "</tr>" for e in entries)
    body = ("<h1>Ad Cleaner — full history</h1>"
            "<table><tr><th>Time</th><th>App</th><th>Action</th><th>Result</th></tr>"
            f"{rows}</table>")
    return _html_page("Ad Cleaner history", body)


def demo():
    r = {"when": "2026-07-13 14:30:02", "model": "SM G991B", "android": "14",
         "stopped": 12, "acted": 4, "removed": True, "popups_blocked": 6,
         "packages": ["com.random.adware"], "dns": "On — AdGuard", "freed_gb": 1.2}
    out = render_receipt_html(r)
    assert "1.2 GB" in out and "com.random.adware" in out and "Removed" in out
    assert "no space freed" in render_receipt_html({**r, "removed": False, "freed_gb": 0}).lower()
    removed0 = render_receipt_html({**r, "removed": True, "freed_gb": 0})
    assert "still installed" not in removed0 and "No measurable change" in removed0
    hist = render_history_html([{"time": "t", "package": "<b>x", "action": "pause", "result": "ok"}])
    assert "<b>x" not in hist and "&lt;b&gt;x" in hist
    print("report.py demo OK")


if __name__ == "__main__":
    demo()
