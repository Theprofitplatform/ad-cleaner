# Private DNS Ad Blocking + Clean Receipt — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add system-wide ad blocking (Android Private DNS) and a printable "clean receipt" report to Ad Cleaner.

**Architecture:** DNS read/write/verify lives in `actions.py` alongside the existing verify-after-act + undo-log pattern. Report rendering lives in a new pure `report.py` (no device I/O, unit-tested). Both wire into the existing Tkinter Device tab, History tab, and clean flow in `gui.py`. All device work stays on background threads via the existing `_run_bg`/`_post` queue.

**Tech Stack:** Python 3.11+, Tkinter, ADB via `subprocess` (existing `Adb` wrapper). Standard library only.

## Global Constraints

- **Python 3.11+**, **standard library only** — no new third-party dependencies.
- **Everything reversible and logged** — actions append to the existing `ActionLog`; the app never performs an irreversible change without a documented undo path.
- **Never crash on ADB failure** — every device call is wrapped; errors surface in the status bar (`status_line(..., "error")`), never an uncaught exception.
- **Offline** — no network calls from the app itself.
- **Only ever touch third-party packages**; protected system apps are never modified (enforced in `actions.py`).
- **Preflight:** work on a feature branch — `git checkout -b feature/adblock-dns-receipt` before Task 1.
- Tests run with `python -m pytest` from the repo root (`conftest.py` puts top-level modules on the path). Each module also keeps a `demo()` self-check runnable via `python <module>.py`.
- Commit messages end with the trailer `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `report.py` | Pure HTML rendering for the clean receipt and full-history export | **Create** |
| `tests/test_report.py` | Unit tests for `report.py` | **Create** |
| `actions.py` | + Private DNS read/set/clear; `clean_risky` reports pop-ups blocked | **Modify** |
| `tests/test_actions.py` | + DNS tests; extend `FakeAdb` for `settings global` | **Modify** |
| `gui.py` | Device-tab DNS controls; receipt after clean; History export → HTML | **Modify** |
| `tests/test_gui_smoke.py` | + DNS toggle + receipt smoke tests; extend `FakeAdb` | **Modify** |

---

## Task 1: `report.py` — printable HTML reports

**Files:**
- Create: `report.py`
- Test: `tests/test_report.py`

**Interfaces:**
- Consumes: nothing (pure).
- Produces:
  - `render_receipt_html(receipt: dict) -> str` where `receipt` has keys
    `when, model, android, stopped, acted, removed(bool), popups_blocked,
    packages(list[str]), dns(str), freed_gb(float)`.
  - `render_history_html(entries: list[dict]) -> str` where each entry has
    `time, package, action, result`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_report.py`:

```python
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


def test_history_escapes_hostile_package_names():
    out = render_history_html(
        [{"time": "t", "package": "<script>x</script>", "action": "pause", "result": "ok"}])
    assert "<script>x" not in out       # not rendered as a live tag
    assert "&lt;script&gt;" in out      # escaped instead
    assert "<table" in out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_report.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'report'`.

- [ ] **Step 3: Write minimal implementation**

Create `report.py`:

```python
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
    freed_line = (f"<p><b>Space freed:</b> {freed} GB</p>" if freed > 0
                  else "<p class='muted'>Apps were paused (still installed) — "
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
    hist = render_history_html([{"time": "t", "package": "<b>x", "action": "pause", "result": "ok"}])
    assert "<b>x" not in hist and "&lt;b&gt;x" in hist
    print("report.py demo OK")


if __name__ == "__main__":
    demo()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_report.py -v && python report.py`
Expected: 3 passed; `report.py demo OK`.

- [ ] **Step 5: Commit**

```bash
git add report.py tests/test_report.py
git commit -m "Add printable HTML reports (receipt + history) in report.py

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: `actions.py` — Private DNS read/set/clear

**Files:**
- Modify: `actions.py` (add `import re` near the top; add constants + functions after `clear_caches`, before the `--- Undo ---` section)
- Modify: `tests/test_actions.py` (extend `FakeAdb`; add DNS tests)

**Interfaces:**
- Consumes: the `Adb`-like object's `shell_text(list) -> str`, `serial`; the `ActionLog` from Task's existing module.
- Produces:
  - `DNS_PROVIDERS: dict[str, str]` — display-label → DoT hostname.
  - `read_private_dns(adb) -> tuple[str, str]` — `(mode, hostname)`; `mode` is
    `"off"`/`"opportunistic"`/`"hostname"`, `hostname` is `""` when not set.
  - `set_private_dns(adb, hostname: str, log) -> bool` — validates, writes,
    verifies by read-back, logs `set-dns`. Raises `ValueError` on a malformed hostname.
  - `clear_private_dns(adb, log) -> bool` — sets mode `off`, verifies, logs `clear-dns`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_actions.py`. First extend `FakeAdb.__init__` to add a globals store and its two `shell_text` branches:

```python
# in FakeAdb.__init__, alongside self.a11y = "":
        self.globals = {}
```

```python
# in FakeAdb.shell_text, BEFORE the final `return ""`:
        if args[:3] == ["settings", "get", "global"]:
            return self.globals.get(args[3], "null")
        if args[:3] == ["settings", "put", "global"]:
            self.globals[args[3]] = args[4]; return ""
```

Then add the import and tests:

```python
from actions import (
    DNS_PROVIDERS, clear_private_dns, read_private_dns, set_private_dns,
)


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
    assert read_private_dns(adb)[0] == "off"
    assert log.entries[-1]["action"] == "clear-dns"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_actions.py -k private_dns -v`
Expected: FAIL with `ImportError: cannot import name 'DNS_PROVIDERS'`.

- [ ] **Step 3: Write minimal implementation**

At the top of `actions.py`, add `import re` (alongside `import json`).

Add after `clear_caches` (before the `# --- Undo ---` comment):

```python
# --- Private DNS (system-wide ad blocking) ----------------------------------

DNS_PROVIDERS = {
    "AdGuard — blocks ads + trackers": "dns.adguard.com",
    "Cloudflare Family — malware + adult": "family.cloudflare-dns.com",
}
_DNS_HOSTNAME_RE = re.compile(r"^[a-z0-9.-]+$")


def read_private_dns(adb):
    """Return (mode, hostname). hostname is '' unless mode == 'hostname'."""
    mode = (adb.shell_text(["settings", "get", "global", "private_dns_mode"]) or "").strip()
    host = (adb.shell_text(["settings", "get", "global", "private_dns_specifier"]) or "").strip()
    if mode in ("", "null"):
        mode = "off"
    if host in ("", "null"):
        host = ""
    return mode, host


def set_private_dns(adb, hostname, log):
    """Turn Private DNS on with `hostname` (a DNS-over-TLS resolver).

    Blocks ads/trackers in every app. Reversible via clear_private_dns / the
    Off button. Logged as 'set-dns' but NOT in UNDOABLE:
    ponytail: the On/Off buttons are the reversal path, no history-undo needed.
    """
    hostname = (hostname or "").strip().lower()
    if not hostname or not _DNS_HOSTNAME_RE.match(hostname):
        raise ValueError("That doesn't look like a valid DNS address.")
    adb.shell_text(["settings", "put", "global", "private_dns_mode", "hostname"])
    adb.shell_text(["settings", "put", "global", "private_dns_specifier", hostname])
    mode, host = read_private_dns(adb)
    ok = mode == "hostname" and host == hostname
    if log is not None:
        log.append(adb.serial, "(device)", "set-dns", "off",
                   ["settings", "put", "global", "private_dns_specifier", hostname],
                   "ok" if ok else "failed")
    return ok


def clear_private_dns(adb, log):
    """Turn Private DNS off (mode=off). Reversible via set_private_dns."""
    adb.shell_text(["settings", "put", "global", "private_dns_mode", "off"])
    ok = read_private_dns(adb)[0] == "off"
    if log is not None:
        log.append(adb.serial, "(device)", "clear-dns", "on",
                   ["settings", "put", "global", "private_dns_mode", "off"],
                   "ok" if ok else "failed")
    return ok
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_actions.py -v && python actions.py`
Expected: all pass (including the four new DNS tests); `actions.py demo OK`.

- [ ] **Step 5: Commit**

```bash
git add actions.py tests/test_actions.py
git commit -m "Add Private DNS read/set/clear to actions.py

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: `actions.py` — count pop-ups blocked in `clean_risky`

**Files:**
- Modify: `actions.py` (`clean_risky`, ~line 163-188)
- Modify: `tests/test_actions.py` (assert the new key)

**Interfaces:**
- Consumes: existing `clean_risky(adb, apps, log, progress=None, remove=False)`.
- Produces: `clean_risky` return dict gains key `"popups_blocked": int` (count of
  non-protected overlay apps whose pop-up permission the clean denies). Existing
  keys `stopped, acted, removed, packages` are unchanged.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_actions.py`:

```python
def test_clean_risky_reports_popups_blocked(log):
    adb = FakeAdb()
    adware = App(package="com.random.adware", installer=None, overlay=True, risk="HIGH")
    quiet = App(package="com.play.cleaner", installer="com.android.vending",
                overlay=False, risk="Medium")
    res = clean_risky(adb, [adware, quiet], log)
    assert res["popups_blocked"] == 1     # only the overlay app is denied
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_actions.py -k popups_blocked -v`
Expected: FAIL with `KeyError: 'popups_blocked'`.

- [ ] **Step 3: Write minimal implementation**

In `clean_risky`, replace the final `return {...}` line. The function currently ends:

```python
    return {"stopped": stopped, "acted": len(acted), "removed": remove, "packages": acted}
```

Change to (add the `popups` count computed the same way `stop_all(block_popups=True)` selects targets):

```python
    popups = sum(1 for a in apps if a.overlay and not a.protected)
    return {"stopped": stopped, "acted": len(acted), "removed": remove,
            "packages": acted, "popups_blocked": popups}
```

Note: compute `popups` from the same predicate `stop_all` uses for pop-up denial
(`a.overlay and not a.protected`). `stop_all` sets `a.overlay = False` on success,
so this line must read the count from a snapshot taken *before* mutation — but
because it counts the number denied (all matching apps are attempted), and
`stop_all` already ran, capture the count up front instead. Replace the whole
tail of `clean_risky` so the count is taken before `stop_all`:

```python
def clean_risky(adb, apps, log, progress=None, remove=False):
    # ... unchanged docstring ...
    popups = sum(1 for a in apps if a.overlay and not a.protected)   # before stop_all clears overlay
    stopped, _ = stop_all(adb, apps, log, block_popups=True, progress=progress)
    acted = []
    for app in apps:
        if app.risk not in SUSPICIOUS or app.protected:
            continue
        try:
            if remove:
                if uninstall(adb, app, log):
                    acted.append(app.package)
            elif app.enabled and pause(adb, app, log):
                acted.append(app.package)
        except (ProtectedAppError, AdbError):
            pass
    return {"stopped": stopped, "acted": len(acted), "removed": remove,
            "packages": acted, "popups_blocked": popups}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_actions.py -v && python actions.py`
Expected: all pass; `actions.py demo OK`.

- [ ] **Step 5: Commit**

```bash
git add actions.py tests/test_actions.py
git commit -m "Report pop-ups blocked from clean_risky

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: `gui.py` — Device-tab Private DNS controls

**Files:**
- Modify: `gui.py` — imports (line 17-19), `_build_device_tab` (~line 422-463),
  `_on_connected` (~line 745-759), new handlers after `on_reboot` (~line 1355)
- Modify: `tests/test_gui_smoke.py` — extend `FakeAdb`; add a DNS toggle test

**Interfaces:**
- Consumes: `set_private_dns`, `clear_private_dns`, `read_private_dns`,
  `DNS_PROVIDERS` from Task 2; existing `_run_bg`, `_post`, `status_line`,
  `_enable_btn`, `_flat_button`, `messagebox`.
- Produces: `self.dns_provider` (StringVar), `self.dns_custom` (Entry),
  `self.dns_status` (StringVar), `self.dns_on_btn`, `self.dns_off_btn`; methods
  `on_dns_on`, `on_dns_off`, `_refresh_dns`, `_show_dns`.

- [ ] **Step 1: Write the failing test**

In `tests/test_gui_smoke.py`, extend `FakeAdb`:

```python
# in FakeAdb.__init__ add:
        self.globals = {}
```

```python
# in FakeAdb.shell_text, before the final `return ""`:
        if args[:3] == ["settings", "get", "global"]:
            return self.globals.get(args[3], "null")
        if args[:3] == ["settings", "put", "global"]:
            self.globals[args[3]] = args[4]; return ""
```

(The existing `["settings", "put"]` branch on line ~68 returns `""` for any
`settings put`; the new, more specific `global` branch must come *before* it, or
place it before that line. Since dict-store must happen, put both new branches
just above the final `return ""`.)

Add the test:

```python
def test_dns_toggle_sets_and_clears(root, monkeypatch, tmp_path):
    _wire(gui, monkeypatch, tmp_path)
    app = gui.AdCleanerApp(root)
    pump(root, 1.5)
    app.dns_provider.set("AdGuard — blocks ads + trackers")
    app.on_dns_on()
    pump(root, 0.6)
    assert app.adb.globals.get("private_dns_mode") == "hostname"
    assert app.adb.globals.get("private_dns_specifier") == "dns.adguard.com"
    app.on_dns_off()
    pump(root, 0.6)
    assert app.adb.globals.get("private_dns_mode") == "off"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_gui_smoke.py -k dns_toggle -v`
Expected: FAIL with `AttributeError: 'AdCleanerApp' object has no attribute 'dns_provider'`.

- [ ] **Step 3: Write minimal implementation**

**(a)** Extend the `actions` import in `gui.py` (line 17-20) to add the DNS names:

```python
from actions import (
    ActionLog, DNS_PROVIDERS, ProtectedAppError, backup_apk, can_undo, clean_risky,
    clear_caches, clear_private_dns, pause, read_private_dns, reboot, reset_app_data,
    resume, set_private_dns, stop_all, undo, uninstall,
)
```

**(b)** In `_build_device_tab`, after the closing muted `ttk.Label(... wraplength=760 ...)` (the "Clearing caches frees space…" note near line 461-463), append the DNS group:

```python
        ttk.Separator(tab, orient="horizontal").pack(fill="x", pady=(18, 12))
        ttk.Label(tab, text="🛡️  Block ads system-wide (Private DNS)",
                  font=(FONT, 12, "bold")).pack(anchor="w")
        ttk.Label(tab, text="Blocks ads and trackers in every app — even ones you keep. "
                            "Reversible any time; never touches photos, messages or accounts.",
                  style="Muted.TLabel", wraplength=760).pack(anchor="w", pady=(2, 8))

        dns_row = ttk.Frame(tab)
        dns_row.pack(anchor="w")
        self.dns_provider = tk.StringVar(value=list(DNS_PROVIDERS)[0])
        choices = list(DNS_PROVIDERS) + ["Custom…"]
        self.dns_combo = ttk.Combobox(dns_row, textvariable=self.dns_provider,
                                      values=choices, state="readonly", width=34)
        self.dns_combo.pack(side="left", padx=(0, 8))
        self.dns_combo.bind("<<ComboboxSelected>>", lambda *_: self._sync_dns_custom())
        self.dns_custom = ttk.Entry(dns_row, width=24)
        self.dns_custom.pack(side="left", padx=(0, 8))
        self.dns_on_btn = self._flat_button(dns_row, "Turn on", self.on_dns_on, GREEN, GREEN_HOT)
        self.dns_on_btn.pack(side="left", padx=(0, 6))
        self.dns_off_btn = self._flat_button(dns_row, "Turn off", self.on_dns_off, SLATE, SLATE_HOT)
        self.dns_off_btn.pack(side="left")

        self.dns_status = tk.StringVar(value="—")
        ttk.Label(tab, textvariable=self.dns_status, style="Muted.TLabel").pack(
            anchor="w", pady=(8, 0))
        for b in (self.dns_on_btn, self.dns_off_btn):
            self._enable_btn(b, False)
        self.dns_btns = (self.dns_on_btn, self.dns_off_btn)
        self._sync_dns_custom()
```

**(c)** In `_on_connected` (line 754-757), add the DNS buttons to the enable loop and refresh DNS state. Change:

```python
        for b in self.dev_btns:
            self._enable_btn(b, True)
        self._enable_btn(self.crash_btn, True)
        self._refresh_device()
```

to:

```python
        for b in self.dev_btns + self.dns_btns:
            self._enable_btn(b, True)
        self._enable_btn(self.crash_btn, True)
        self._refresh_device()
        self._refresh_dns()
```

**(d)** Add the handlers after `on_reboot`'s method block (after line ~1355, before `# --- device maintenance` ends / `on_export`). Place within the class:

```python
    def _sync_dns_custom(self):
        """Enable the custom-hostname box only when 'Custom…' is chosen."""
        custom = self.dns_provider.get() == "Custom…"
        self.dns_custom.configure(state="normal" if custom else "disabled")

    def _dns_hostname(self):
        """Resolve the chosen provider/custom entry to a hostname string."""
        label = self.dns_provider.get()
        if label == "Custom…":
            return self.dns_custom.get().strip()
        return DNS_PROVIDERS.get(label, "")

    def on_dns_on(self):
        if self.busy or not self.serial:
            return
        host = self._dns_hostname()
        if not host:
            messagebox.showinfo("Block ads", "Type a DNS address for the Custom option.")
            return
        self.status_line("Turning on ad blocking…")

        def work():
            try:
                set_private_dns(self.adb, host, self.log)
                self._post(self._after_dns, None)
            except ValueError as ve:
                self._post(self._after_dns, str(ve))
            except Exception as e:
                self._post(self._after_dns, self._friendly(str(e)))

        self._run_bg(work)

    def on_dns_off(self):
        if self.busy or not self.serial:
            return
        self.status_line("Turning off ad blocking…")

        def work():
            try:
                clear_private_dns(self.adb, self.log)
                self._post(self._after_dns, None)
            except Exception as e:
                self._post(self._after_dns, self._friendly(str(e)))

        self._run_bg(work)

    def _after_dns(self, err):
        self._refresh_history()
        if err:
            self.status_line("Couldn't change ad blocking. " + err, "error")
        self._refresh_dns()

    def _refresh_dns(self):
        if not self.serial:
            return

        def work():
            try:
                mode, host = read_private_dns(self.adb)
                self._post(self._show_dns, mode, host)
            except Exception:
                pass

        self._run_bg(work)

    def _show_dns(self, mode, host):
        if mode == "hostname" and host:
            label = next((k for k, v in DNS_PROVIDERS.items() if v == host), host)
            self.dns_status.set(f"On — {label}")
        else:
            self.dns_status.set("Off")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_gui_smoke.py -v`
Expected: all pass, including `test_dns_toggle_sets_and_clears`.

- [ ] **Step 5: Commit**

```bash
git add gui.py tests/test_gui_smoke.py
git commit -m "Add Device-tab Private DNS ad-blocking controls

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 5: `gui.py` — clean receipt + History export → HTML

**Files:**
- Modify: `gui.py` — imports (add `webbrowser`; add `report` + `read_device_stats`
  already imported), `_start_clean.work()` (~line 1161-1169), `_clean_done`
  (~line 1171-1204), `on_export` (~line 1434-1452), remove unused `csv` import
- Modify: `tests/test_gui_smoke.py` — patch `webbrowser.open`; add a receipt test

**Interfaces:**
- Consumes: `clean_risky` return dict (now with `popups_blocked`), `read_device_stats`
  (already imported), `render_receipt_html`, `render_history_html`, `read_private_dns`.
- Produces: `self.android` (str, set in `_on_connected`); `self._save_receipt(res) -> Path|None`.

- [ ] **Step 1: Write the failing test**

In `tests/test_gui_smoke.py`, patch `webbrowser.open` inside `_wire` so no browser
launches during tests (add to the `_wire` body):

```python
    monkeypatch.setattr(gui.webbrowser, "open", lambda *a, **k: None)
```

Add the test:

```python
def test_clean_writes_receipt_html(root, monkeypatch, tmp_path):
    from adb import data_dir
    _wire(gui, monkeypatch, tmp_path)
    app = gui.AdCleanerApp(root)
    pump(root, 1.5)
    app.on_clean()
    pump(root, 1.0)
    reports = list((data_dir() / "reports").glob("receipt_*.html"))
    assert reports, "a receipt HTML file should be written after a clean"
    text = reports[-1].read_text(encoding="utf-8")
    assert "Ad Cleaner — clean receipt" in text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_gui_smoke.py -k receipt -v`
Expected: FAIL — no `receipt_*.html` written (and/or `AttributeError` on `gui.webbrowser`).

- [ ] **Step 3: Write minimal implementation**

**(a)** Imports at the top of `gui.py`: remove `import csv` (line 8) — it becomes
unused after this task — and add `import webbrowser`. Add the report import after
the `from device import read_device_stats` line (line 22):

```python
import webbrowser
```

```python
from report import render_history_html, render_receipt_html
```

(Verify `csv` is unused: `grep -n "csv" gui.py` should return only the removed
import line. If any other use exists, keep the import.)

**(b)** Store the Android version. In `_on_connected` (line 745-746), after
`self.model = model` add:

```python
        self.android = android
```

Also add a default in `__init__` (near the other instance attributes) so the
receipt never fails before a connect: `self.android = ""`.

**(c)** Capture free space + DNS around the clean. Replace `_start_clean.work()`
(line 1161-1169) with:

```python
        def work():
            try:
                before = self._free_gb()
                res = clean_risky(self.adb, self.apps, self.log, progress=progress,
                                  remove=remove)
                res["freed_gb"] = round(max(0.0, self._free_gb() - before), 1)
                try:
                    mode, host = read_private_dns(self.adb)
                    label = next((k for k, v in DNS_PROVIDERS.items() if v == host), host)
                    res["dns"] = f"On — {label}" if mode == "hostname" and host else "Off"
                except Exception:
                    res["dns"] = "Off"
                self._post(self._clean_done, res, None)
            except Exception as e:
                self._post(self._clean_done, None, str(e))
```

Add a small helper (anywhere in the class, e.g. just above `_start_clean`):

```python
    def _free_gb(self):
        """Free space on /data in GB, or 0.0 if it can't be read.
        ponytail: reuses read_device_stats (0.1 GB granularity); sub-100 MB
        cache trims read as 0 freed, which is fine for a receipt.
        """
        try:
            return read_device_stats(self.adb).get("storage_free_gb", 0) or 0.0
        except Exception:
            return 0.0
```

**(d)** Add the receipt-saver and wire it into `_clean_done`. Add the method:

```python
    def _save_receipt(self, res):
        """Write a printable HTML receipt for this clean; return its path (or None)."""
        try:
            receipt = {
                "when": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "model": getattr(self, "model", "") or "",
                "android": getattr(self, "android", "") or "",
                "stopped": res.get("stopped", 0), "acted": res.get("acted", 0),
                "removed": res.get("removed", False),
                "popups_blocked": res.get("popups_blocked", 0),
                "packages": res.get("packages", []), "dns": res.get("dns", "Off"),
                "freed_gb": res.get("freed_gb", 0),
            }
            folder = data_dir() / "reports"
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"receipt_{datetime.now():%Y%m%d_%H%M%S}.html"
            path.write_text(render_receipt_html(receipt), encoding="utf-8")
            return path
        except Exception:
            return None
```

In `_clean_done` (line 1171-1204), after the `if res["removed"]:` block and the
`self._refresh_history(); self._render_table(); self._update_detail()` calls,
save the receipt once. Replace the shop-mode and normal-mode tail so both save
the receipt, and normal mode offers to open it:

```python
        verb = "removed" if res["removed"] else "paused"
        summary = f"Closed {res['stopped']} app(s) and {verb} {res['acted']} risky one(s)."
        receipt_path = self._save_receipt(res)
        if self.shop_mode.get():
            self._set_summary("✅  DONE — unplug and connect the next phone.", "good")
            try:
                self.root.bell()
            except tk.TclError:
                pass
            self.status_line(f"✅ Cleaned — {summary}  Unplug and connect the next phone.",
                             "good")
            return
        self._set_summary(f"✅  Done — {summary}", "good")
        self.status_line(f"✅ Done! {summary} Your phone should be usable now.", "good")
        open_it = messagebox.askyesno(
            "All done",
            f"{summary}\n\n"
            "Your photos, messages and system apps were not touched.\n"
            "You can undo anything from the History tab.\n\n"
            "Open a printable receipt now?",
            default="no")
        if open_it and receipt_path:
            try:
                webbrowser.open(receipt_path.as_uri())
            except Exception:
                pass
```

**(e)** Repoint `on_export` (line 1434-1452) to write printable HTML via
`render_history_html` and open it, replacing the CSV body:

```python
    def on_export(self):
        entries = self.log.recent()
        if not entries:
            self.status_line("Nothing to export yet.")
            return
        folder = data_dir() / "reports"
        folder.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = folder / f"history_{stamp}.html"
        try:
            path.write_text(render_history_html(entries), encoding="utf-8")
            self.status_line(f"✅ Report saved to {path}", "good")
            try:
                webbrowser.open(path.as_uri())
            except Exception:
                pass
        except Exception as ex:
            self.status_line("Couldn't save report. " + self._friendly(str(ex)), "error")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest -v`
Expected: the whole suite passes, including `test_clean_writes_receipt_html`,
`test_one_click_clean`, `test_uninstall_mode_removes_apps`, and
`test_shop_mode_auto_cleans_on_scan` (the receipt save must not break those).

- [ ] **Step 5: Commit**

```bash
git add gui.py tests/test_gui_smoke.py
git commit -m "Save a printable clean receipt and export history as HTML

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 6: README + final full-suite check

**Files:**
- Modify: `README.md` (button table + a line on Private DNS / receipt)

**Interfaces:** none.

- [ ] **Step 1: Update the docs**

In `README.md`, under "What the buttons do" (the table around line 68-76) add:

```markdown
| **🛡️ Block ads (Private DNS)** | On the Device tab. Sends the phone's DNS through an ad-blocking resolver so ads are blocked in **every** app, even ones you keep. Reversible with **Turn off**. |
```

And under "History / Undo" add a sentence:

```markdown
After a clean, Ad Cleaner saves a **printable receipt** (in `adcleaner_data/reports/`)
listing what it closed, blocked and removed — handy when cleaning someone else's
phone. The **Export report** button saves the full history the same way.
```

- [ ] **Step 2: Run the full suite + every module self-check**

Run:
```bash
python -m pytest -v
python report.py && python actions.py && python device.py && python scanner.py && python crashes.py && python adb.py && python protected.py
```
Expected: all tests pass; every `demo() OK` prints.

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "Document Private DNS ad blocking and clean receipt

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage:**
- Private DNS small picker (AdGuard / Cloudflare-family / custom) → Task 2 (`DNS_PROVIDERS`, validation) + Task 4 (combobox + custom entry). ✓
- DNS opt-in on Device tab; CLEAN untouched → Task 4 adds a separate Device-tab group; `_start_clean`/`clean_risky` never call `set_private_dns`. ✓
- Reversible + logged → `set-dns`/`clear-dns` logged; Off button reverses (Task 2). ✓
- Receipt auto after clean + printable HTML → Task 5 `_save_receipt` + `render_receipt_html`. ✓
- Manual export on History → **existing `on_export` upgraded** from CSV to HTML (Task 5). Deviation from the spec's "add a button" — a button already existed; upgrading it is the lazier, non-duplicating choice. ✓
- Receipt fields (model, android, counts, pop-ups, DNS, space freed) → receipt dict in Task 5; `popups_blocked` from Task 3; `android` stored in Task 5(b). ✓
- Space freed honesty (pause frees nothing) → `render_receipt_html` prints the "no space freed" line when `freed_gb <= 0` (Task 1). ✓
- Standard library only, no new deps → `html`, `webbrowser`, `re` are stdlib. ✓
- Tests via demo() + pytest → every task has both. ✓

**Deviations from spec (intentional, noted for the reviewer):**
1. Manual history export **upgrades the existing CSV button** rather than adding a new one.
2. Space freed reported in **GB (1 decimal)** not MB — matches the Device tab's units and `read_device_stats`; sub-100 MB deltas read as 0 (documented `ponytail:` ceiling).
3. Receipt delivery reuses the existing "All done" dialog as an **askyesno → open in browser** (auto-saves every time) instead of a bespoke Save-button Toplevel — consistent with how screenshots auto-save to a folder.

**Placeholder scan:** none — every code step contains complete code.

**Type consistency:** `read_private_dns` returns `(mode, host)` everywhere; `DNS_PROVIDERS` label→hostname used consistently in Tasks 2/4/5; `clean_risky` dict key `popups_blocked` produced in Task 3 and consumed in Task 5; `_free_gb`/`_save_receipt` defined and called in Task 5 only.
