# ADB Feature Expansion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the seven researched features to Ad Cleaner: restore hijacked default apps, notification-spam detection + fix, stalkerware detection, preinstalled-bloatware disabling, battery report, background-data hogs, and screen-time on the receipt.

**Architecture:** Every feature follows the existing three-layer pattern: pure parse/decide functions in `scanner.py` / `device.py` / new small modules (unit-testable with text fixtures, no adb), device actions with undo-logging in `actions.py` (tested with a FakeAdb), thin Tk wiring in `gui.py` (covered by `tests/test_gui_smoke.py`). No new dependencies, no network at runtime.

**Tech Stack:** Python 3.12 stdlib + tkinter, adb shell over USB (no root, no on-phone agent), pytest.

## Global Constraints

- **No new runtime dependencies.** Stdlib only. No network calls at app runtime (offline-first shop tool).
- **Every device mutation goes through `actions.py`**, is logged via `log.append(adb.serial, package, action, previous, cmd, result)`, and is undoable where technically possible (add the action name to `UNDOABLE` and a branch to `undo()`).
- **Protected apps are sacred**: destructive actions call `_guard(app)`; new action types that are safe + reversible (notification revoke, background-data restrict) may skip `_guard` but must be in `UNDOABLE`.
- **All parsers are defensive**: `dumpsys` output varies by OEM/Android version; every parser must return an empty/neutral value on garbage input, never raise.
- **Tests are hermetic**: text fixtures in `tests/fixtures/`, `FakeAdb` classes, the autouse `_isolated_blocklist` fixture pattern from `tests/test_scanner.py` for anything touching module-global state. Every new `demo()` assertion must pass via `python <module>.py`.
- **Ship flow per task**: after the final commit of each task run `python -m pytest -q` (expect all green), then `git checkout -b <task-branch>`, push, `gh pr create`, wait for CI (`gh pr checks --watch`), `gh pr merge --merge`, `git checkout master && git pull`. Each task is one PR.
- **Existing interfaces used throughout** (do not redefine):
  - `Adb.shell_text(args: list[str], timeout=10) -> str`, raises `AdbError` (message for a missing remote object contains "does not exist"; `_friendly()` maps common stderr).
  - `App` dataclass (`scanner.py:79`): fields include `package, label, installer, is_system, enabled, overlay, hijacked_roles (list of friendly role names), score, risk, reasons (list of display strings)`; property `protected`.
  - `scanner.ROLES = {"android.app.role.HOME": "home screen", "android.app.role.BROWSER": "browser", "android.app.role.SMS": "text messages", "android.app.role.DIALER": "phone dialer"}`.
  - `ActionLog.append(serial, package, action, previous, command, result)`; `UNDOABLE` set; `can_undo(entry)`; `undo(adb, entry, log)`.
  - GUI helpers: `self._flat_button(parent, text, cmd, color, hot)`, `self._enable_btn(btn, bool)`, `self._run_bg(fn)`, `self._post(fn, *args)` (thread-safe UI), `self.status_line(msg, kind)`, button tuples enabled/disabled on connect/disconnect at `gui.py` (`_on_connect`/`_on_disconnect` loops over `self.bulk_btns + self.dev_btns + self.dns_btns + self.move_btns`).
  - `device.read_device_stats(adb) -> dict` of display-ready values shown in the Device tab (`self.dev_vars`).
  - `report.render_receipt_html(receipt: dict) -> str`; receipt dict built in `gui.py:~1448`.
  - Test plumbing: `tests/test_gui_smoke.py` `_wire(gui, monkeypatch, tmp_path)` stubs adb + messageboxes; `pump(root, secs)` runs the Tk loop. `tests/test_scanner.py` `FakeAdb.shell_text` serves `tests/fixtures/*.txt` per command.

---

### Task 1: Restore hijacked default apps

Ad Cleaner already detects role hijack (`App.hijacked_roles`); this adds the fix: hand the role back to a stock app via `cmd role add-role-holder` (works no-root, Android 10+; shell holds `MANAGE_ROLE_HOLDERS`).

**Files:**
- Modify: `scanner.py` (add `ROLE_IDS` reverse map + stock candidates)
- Modify: `actions.py` (add `fix_role`, extend `UNDOABLE`/`undo`)
- Modify: `gui.py` (detail-pane "Restore default apps" button)
- Test: `tests/test_actions.py`, `tests/test_gui_smoke.py`

**Interfaces:**
- Consumes: `App.hijacked_roles` (friendly names), `scanner.ROLES`.
- Produces: `scanner.ROLE_IDS: dict[str, str]` (friendly → role id), `scanner.STOCK_ROLE_HOLDERS: dict[str, tuple[str, ...]]` (role id → candidate stock packages, best first), `actions.fix_role(adb, role_id, hijacker_pkg, log) -> str | None` (returns the package the role was given to, `None` if no candidate installed).

- [ ] **Step 1: Write the failing tests** (append to `tests/test_actions.py`)

```python
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
```

`FakeAdb` in `tests/test_actions.py` needs two new handlers inside `shell_text` (also record every call: add `self.commands = []` in `__init__` and `self.commands.append(" ".join(args))` at the top of `shell_text`):

```python
if args[:2] == ["pm", "path"]:
    return f"package:/data/app/{args[-1]}/base.apk" if args[-1] in self.installed else ""
if args[:3] == ["cmd", "role", "add-role-holder"]:
    self.role_holder = args[-1]; return ""
if args[:3] == ["cmd", "role", "get-role-holders"]:
    return getattr(self, "role_holder", "")
```

- [ ] **Step 2: Run to verify failure** — `python -m pytest tests/test_actions.py -q -k fix_role` → FAIL (`ImportError: cannot import name 'fix_role'`). Add `fix_role` to the `from actions import (...)` list first.

- [ ] **Step 3: Implement.** In `scanner.py` directly under `ROLES`:

```python
ROLE_IDS = {v: k for k, v in ROLES.items()}   # friendly name -> role id

# Stock apps to hand a hijacked role back to, best candidate first.
# ponytail: static candidate list, not device introspection -- extend as
# OEMs surface. fix_role picks the first one actually installed.
STOCK_ROLE_HOLDERS = {
    "android.app.role.HOME": ("com.sec.android.app.launcher",
                              "com.google.android.apps.nexuslauncher",
                              "com.miui.home", "com.android.launcher3"),
    "android.app.role.BROWSER": ("com.android.chrome",
                                 "com.sec.android.app.sbrowser",
                                 "com.mi.globalbrowser", "com.android.browser"),
    "android.app.role.SMS": ("com.google.android.apps.messaging",
                             "com.samsung.android.messaging"),
    "android.app.role.DIALER": ("com.google.android.dialer",
                                "com.samsung.android.dialer"),
}
```

In `actions.py` (import `STOCK_ROLE_HOLDERS` from scanner; add `"fix-role"` to `UNDOABLE`):

```python
def _installed(adb, package):
    """True if the package is present for user 0 (pm path prints its APK)."""
    try:
        return bool(adb.shell_text(["pm", "path", package]).strip())
    except AdbError:
        return False


def fix_role(adb, role_id, hijacker_pkg, log):
    """Hand a hijacked default (browser/SMS/dialer/home) back to a stock app.

    Works no-root on Android 10+ (shell holds MANAGE_ROLE_HOLDERS). The target
    must qualify for the role or Android silently ignores the command, so the
    result is verified by re-reading the holder. Undo re-crowns the hijacker.
    """
    for candidate in STOCK_ROLE_HOLDERS.get(role_id, ()):
        if not _installed(adb, candidate):
            continue
        cmd = ["cmd", "role", "add-role-holder", "--user", "0", role_id, candidate]
        adb.shell_text(cmd)
        holders = adb.shell_text(["cmd", "role", "get-role-holders", role_id])
        ok = candidate in holders
        log.append(adb.serial, candidate, "fix-role", hijacker_pkg, cmd,
                   "ok" if ok else "failed")
        return candidate if ok else None
    return None
```

`undo()` branch (previous holder is in `entry["previous"]`; the role id is the 6th token of the logged command):

```python
elif action == "fix-role":
    role_id = entry["command"].split()[5]
    cmd = ["cmd", "role", "add-role-holder", "--user", "0", role_id, entry["previous"]]
    adb.shell_text(cmd)
```

- [ ] **Step 4: Run tests** — `python -m pytest tests/test_actions.py -q` → PASS. Commit: `git add -A tests/test_actions.py actions.py scanner.py && git commit -m "Add fix_role: restore hijacked default apps to stock holders"`

- [ ] **Step 5: GUI wiring + smoke test.** In `gui.py` `_update_detail` (the detail pane), after the existing per-app action buttons are configured, show a restore button only when `self.selected.hijacked_roles` is non-empty (create the button once in the detail pane builder next to the other detail buttons, using the pattern of the existing ones, e.g. `self.fixrole_btn = self._flat_button(parent, "🛠  Restore default apps", self.on_fix_roles, GREEN, GREEN_HOT)`; `pack_forget()`/`pack()` it in `_update_detail` exactly like other conditional widgets there). Handler:

```python
def on_fix_roles(self):
    a = self.selected
    if not a or self.busy or not self.serial:
        return
    def work():
        restored = []
        for friendly in list(a.hijacked_roles):
            role_id = ROLE_IDS.get(friendly)
            if role_id:
                pkg = fix_role(self.adb, role_id, a.package, self.log)
                if pkg:
                    restored.append(friendly)
        self._post(self._fix_roles_done, a, restored)
    self.busy = True
    self._run_bg(work)

def _fix_roles_done(self, app, restored):
    self.busy = False
    if restored:
        app.hijacked_roles = [r for r in app.hijacked_roles if r not in restored]
        self.status_line("✅ Restored: " + ", ".join(restored), "good")
        self._update_detail()
    else:
        self.status_line("Couldn't restore the defaults on this phone.", "error")
```

Import `fix_role` in gui's actions import and `ROLE_IDS` from scanner. Smoke test (append to `tests/test_gui_smoke.py`; extend its `FakeAdb.shell_text` with the same three handlers as Step 1):

```python
def test_fix_roles_button_restores_and_updates_detail(root, monkeypatch, tmp_path):
    _wire(gui, monkeypatch, tmp_path)
    app = gui.AdCleanerApp(root)
    pump(root, 1.5)
    app.adb.installed |= {"com.android.chrome"}
    a = App(package="com.random.freegift", installer=None, risk="HIGH",
            hijacked_roles=["browser"])
    app.apps = [a]; app._render_table()
    app.tree.selection_set("com.random.freegift"); app._on_select()
    app.on_fix_roles()
    pump(root, 1.0)
    assert a.hijacked_roles == []
```

- [ ] **Step 6: Full suite + ship** — `python -m pytest -q` → all pass. Commit `git add -A && git commit -m "GUI: Restore default apps button in the detail pane"`, then the ship flow (branch `feature/fix-hijacked-defaults`, PR, CI, merge).

---

### Task 2: Notification-spam detection + fix

Detect apps flooding notifications (`dumpsys notification --noredact`), add a scoring signal, a per-app "Stop notifications" action, and a Device-tab one-click "Stop fake virus pop-ups" (revokes Chrome's notification permission — the only no-root fix for site-notification spam).

**Files:**
- Create: `tests/fixtures/dumpsys_notification.txt`
- Modify: `scanner.py` (parser, `App.notif_count`, signal), `actions.py` (`block_notifications`), `gui.py` (detail button + Device-tab quick fix)
- Test: `tests/test_scanner.py`, `tests/test_actions.py`, `tests/test_gui_smoke.py`

**Interfaces:**
- Produces: `scanner.parse_notification_counts(text) -> dict[str, int]`, `App.notif_count: int = 0` field, `WEIGHTS["notif_spam"] = 10`, `REASONS["notif_spam"] = "Floods the phone with notifications"`, `NOISY_THRESHOLD = 5`, `actions.block_notifications(adb, package, log) -> bool` (no `_guard` — safe + reversible; works on protected apps like Chrome by design).

- [ ] **Step 1: Fixture + failing parser test.** `tests/fixtures/dumpsys_notification.txt`:

```
  NotificationRecord(0x123 pkg=com.random.freegift user=UserHandle{0} id=1 tag=null
  NotificationRecord(0x124 pkg=com.random.freegift user=UserHandle{0} id=2 tag=null
  NotificationRecord(0x125 pkg=com.random.freegift user=UserHandle{0} id=3 tag=null
  NotificationRecord(0x126 pkg=com.random.freegift user=UserHandle{0} id=4 tag=null
  NotificationRecord(0x127 pkg=com.random.freegift user=UserHandle{0} id=5 tag=null
  NotificationRecord(0x128 pkg=com.whatsapp user=UserHandle{0} id=9 tag=msg
```

Test (`tests/test_scanner.py`):

```python
def test_parse_notification_counts():
    counts = scanner.parse_notification_counts(fx("dumpsys_notification.txt"))
    assert counts == {"com.random.freegift": 5, "com.whatsapp": 1}
    assert scanner.parse_notification_counts("") == {}


def test_notif_spam_scored():
    app = App(package="com.random.freegift", installer=None,
              first_install=datetime(2020, 1, 1), notif_count=5)
    score_app(app, NOW)
    assert scanner.REASONS["notif_spam"] in app.reasons
```

- [ ] **Step 2: Verify failure** — `python -m pytest tests/test_scanner.py -q -k notif` → FAIL (no attribute).

- [ ] **Step 3: Implement in `scanner.py`.** Parser (next to the other parsers):

```python
def parse_notification_counts(output):
    """`dumpsys notification --noredact` -> {package: active notification count}."""
    counts = {}
    for m in re.finditer(r"NotificationRecord\([^)]*\bpkg=([\w.]+)", output or ""):
        counts[m.group(1)] = counts.get(m.group(1), 0) + 1
    return counts
```

`App` gains `notif_count: int = 0`. `WEIGHTS["notif_spam"] = 10`, `REASONS["notif_spam"] = "Floods the phone with notifications"`, module constant `NOISY_THRESHOLD = 5`. In `score_app` signals dict add `"notif_spam": app.notif_count >= NOISY_THRESHOLD`. In `build_inventory`, before the app loop: `notif = parse_notification_counts(_safe(lambda: adb.shell_text(["dumpsys", "notification", "--noredact"])))` and inside the loop set `notif_count=notif.get(pkg, 0)` when constructing the App. Add the fixture route to `tests/test_scanner.py` `FakeAdb`: `if args[:2] == ["dumpsys", "notification"]: return fx("dumpsys_notification.txt")`.

- [ ] **Step 4: Run + commit** — `python -m pytest tests/test_scanner.py -q` → PASS. `git commit -am "Scanner: notification-spam signal from dumpsys notification"`

- [ ] **Step 5: Action + failing test.** Test (`tests/test_actions.py`; extend FakeAdb: record commands, and make `["pm", "revoke", ...]` return "" while `["appops", ...]` returns ""):

```python
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
```

Implementation (`actions.py`; add `"block-notifications"` to `UNDOABLE`):

```python
def block_notifications(adb, package, log):
    """Silence an app's notifications. Safe + reversible, so no protected-app
    guard -- this is how fake-virus pop-ups from Chrome site notifications get
    stopped. Android 13+: revoke POST_NOTIFICATIONS; older: legacy appop.
    """
    cmd = ["pm", "revoke", package, "android.permission.POST_NOTIFICATIONS"]
    try:
        adb.shell_text(cmd)
        # let the customer be re-prompted later instead of hard-blocking forever
        try:
            adb.shell_text(["pm", "clear-permission-flags", package,
                            "android.permission.POST_NOTIFICATIONS",
                            "user-set", "user-fixed"])
        except AdbError:
            pass
    except AdbError:
        cmd = ["appops", "set", package, "POST_NOTIFICATION", "ignore"]
        adb.shell_text(cmd)
    log.append(adb.serial, package, "block-notifications", "allowed", cmd, "ok")
    return True
```

`undo()` branch:

```python
elif action == "block-notifications":
    if "appops" in entry["command"]:
        cmd = ["appops", "set", pkg, "POST_NOTIFICATION", "allow"]
    else:
        cmd = ["pm", "grant", pkg, "android.permission.POST_NOTIFICATIONS"]
    adb.shell_text(cmd)
```

- [ ] **Step 6: Run + commit** — `python -m pytest tests/test_actions.py -q` → PASS. `git commit -am "Action: block notifications (pm revoke, appops fallback), undoable"`

- [ ] **Step 7: GUI.** (a) Detail pane: `self.notif_btn = self._flat_button(..., "🔕  Stop its notifications", self.on_block_notifs, AMBER, AMBER_HOT)`, shown in `_update_detail` when `self.selected.notif_count > 0` (same pack/pack_forget pattern as Task 1's button); handler runs `block_notifications(self.adb, a.package, self.log)` via `_run_bg` + `_post` status "✅ Notifications stopped for {label}". (b) Device tab, next to the existing maintenance buttons: `"🚫  Stop fake virus pop-ups (Chrome)"` with handler named exactly `on_chrome_popups` (the smoke test calls it) → `askyesno` first ("This silences ALL Chrome notifications (including sites the customer wants). They can re-enable in Android Settings. Continue?"), then `_run_bg` → `block_notifications(self.adb, "com.android.chrome", self.log)` → `_post` status. Add the button to `self.dev_btns` so it follows connect/disconnect. Smoke test:

```python
def test_chrome_popup_quickfix_blocks_notifications(root, monkeypatch, tmp_path):
    _wire(gui, monkeypatch, tmp_path)
    app = gui.AdCleanerApp(root)
    pump(root, 1.5)
    app.on_chrome_popups()
    pump(root, 1.0)
    assert any("com.android.chrome" in c and
               ("revoke" in c or "POST_NOTIFICATION" in c)
               for c in app.adb.commands)
```

(extend the gui-smoke `FakeAdb` with the same pm revoke/appops handlers + `commands` recording).

- [ ] **Step 8: Full suite + ship** — `python -m pytest -q` → all pass; `git commit -am "GUI: stop-notifications button + Chrome fake-pop-up quick fix"`; ship flow (branch `feature/notification-spam`).

---

### Task 3: Stalkerware detection

Match scanned packages against the Echap `stalkerware-indicators` package-id list (CC-BY, credit required). A hit forces HIGH with its own reason and a victim-safety note in the detail pane.

**Files:**
- Create: `stalkerware.py` (generated data module), `scripts/update_stalkerware.py` (dev-time regenerator)
- Modify: `protected.py` (unprotect override), `scanner.py` (forced-HIGH signal), `gui.py` (detail-pane caution)
- Test: `tests/test_scanner.py`, `tests/test_protected.py`

**Interfaces:**
- Produces: `stalkerware.is_stalkerware(package: str) -> bool`, `stalkerware.STALKERWARE: frozenset[str]`, `scanner.STALKER_REASON = "Hidden tracking app (stalkerware)"`.

- [ ] **Step 1: Create `stalkerware.py`** with a starter set and CC-BY attribution header:

```python
"""Known stalkerware package ids.

Data derived from the stalkerware-indicators project by Echap
(https://github.com/AssoEchap/stalkerware-indicators), licensed CC-BY.
Regenerate with scripts/update_stalkerware.py (dev-time only; the app
itself never touches the network).
"""

STALKERWARE = frozenset({
    "com.mspy.lite", "com.thetruthspy", "com.flexispy.core",
    "com.hellospy.system", "com.mobiletracker.free", "net.cocospy",
    "com.ikeymonitor", "com.spyera", "com.xnspy", "com.hoverwatch",
    "com.cerberusapp", "com.snoopza", "com.spyzie", "com.copy9",
    "com.spyhuman", "com.letmespy", "com.mobistealth", "com.highsterspy",
    "com.spytomobile", "com.trackview",
})


def is_stalkerware(package):
    """True if the package id is on the known-stalkerware list."""
    return package in STALKERWARE
```

And `scripts/update_stalkerware.py` (dev-time, stdlib-only):

```python
"""Regenerate stalkerware.py from Echap's ioc.yaml (run manually, needs internet)."""
import re, urllib.request

URL = ("https://raw.githubusercontent.com/AssoEchap/"
       "stalkerware-indicators/master/ioc.yaml")

raw = urllib.request.urlopen(URL, timeout=30).read().decode("utf-8")
pkgs = sorted(set(re.findall(r"^\s+- ([a-zA-Z][\w]*(?:\.[\w]+){2,})\s*$",
                             raw, re.MULTILINE)))
assert len(pkgs) > 100, f"suspiciously few ids parsed: {len(pkgs)}"
print(f"{len(pkgs)} package ids")
# splice into stalkerware.py between the frozenset braces, preserving the header
```

(the executor completes the splice — read `stalkerware.py`, replace the literal set with the fetched ids, write back — then runs it once and commits the refreshed module; if offline, the starter set ships as-is.)

- [ ] **Step 2: Failing tests.** `tests/test_scanner.py`:

```python
def test_stalkerware_forced_high():
    app = App(package="com.thetruthspy", installer=None,
              first_install=datetime(2020, 1, 1))
    score_app(app, NOW)
    assert app.risk == "HIGH"
    assert scanner.STALKER_REASON in app.reasons
    assert not app.protected
```

`tests/test_protected.py`:

```python
def test_stalkerware_is_never_protected():
    from stalkerware import is_stalkerware
    assert is_stalkerware("com.thetruthspy")
    assert not is_stalkerware("com.whatsapp")
```

- [ ] **Step 3: Verify failure, implement.** `scanner.py`: import `is_stalkerware` from stalkerware; add `STALKER_REASON = "Hidden tracking app (stalkerware)"`; in `score_app`, next to the existing blocklist branch:

```python
    stalker = is_stalkerware(app.package)
    if stalker:
        app.reasons.insert(0, STALKER_REASON)

    if spoof or blocked or stalker or app.score >= HIGH_THRESHOLD:
        app.risk = "HIGH"
```

`protected.py` `is_protected`: extend the existing prefix-fence line to include stalkerware (import at top of the function body to avoid a module cycle — `protected` must not import `scanner`):

```python
    from stalkerware import is_stalkerware
    if package not in PROTECTED_EXACT and (
            is_blocked(package) or looks_like_junk(package)
            or is_stalkerware(package)):
        return False
```

- [ ] **Step 4: Run + commit** — `python -m pytest tests/test_scanner.py tests/test_protected.py -q` → PASS. `git add stalkerware.py scripts/update_stalkerware.py scanner.py protected.py tests && git commit -m "Detect stalkerware via bundled Echap indicator list (CC-BY)"`

- [ ] **Step 5: GUI caution + README credit.** In `_update_detail`, when `scanner.STALKER_REASON in a.reasons`, append to the detail text: `"⚠ This looks like a hidden tracking app. Ask the customer privately whether they expected it — removing it can alert whoever installed it."` README: add a "Stalkerware" paragraph under the features section including the CC-BY credit line: `Stalkerware detection data © Echap (stalkerware-indicators), CC-BY.` Smoke assertion: build an App with `package="com.thetruthspy"`, run `score_app`, select it, assert the caution string appears in the detail widget text.

- [ ] **Step 6: Full suite + ship** — `python -m pytest -q`; `git commit -am "GUI: stalkerware caution note; README credit"`; ship flow (branch `feature/stalkerware-detection`).

---

### Task 4: Preinstalled bloatware disabling

Find known-junk *system* packages (Ad Cleaner today only scans third-party) and disable them. Disable-first (`pm disable-user`), never uninstall by default — real bootloop precedents exist (e.g. `com.miui.securitycenter`). Only exact matches of a curated list are ever touched.

**Files:**
- Create: `bloatware.py`
- Modify: `actions.py` (`debloat`), `gui.py` (Device-tab section)
- Test: `tests/test_bloatware.py` (new), `tests/test_actions.py`, `tests/test_gui_smoke.py`

**Interfaces:**
- Produces: `bloatware.BLOAT_SEED: frozenset[str]`, `bloatware.find_bloat(adb) -> list[str]` (installed system packages matching seed ∪ `adcleaner_data/bloatware.txt`), `actions.debloat(adb, package, log) -> bool` (disable-user; raises `ProtectedAppError` if package not on the bloat list — the list IS the safety authorization).

- [ ] **Step 1: Create `bloatware.py`:**

```python
"""Curated preinstalled-junk list (carrier installers, OEM ad services,
Facebook preload stubs). Only EXACT matches here may be disabled -- this list
is the safety authorization, so keep it to packages with well-documented
removals and no OS role. ponytail: seed + user file, same model as the
blocklist; UAD-style tiers can come later if the seed proves too small.
"""
from adb import data_dir

BLOAT_SEED = frozenset({
    # Facebook preload stubs (background downloaders, no UI)
    "com.facebook.appmanager", "com.facebook.services", "com.facebook.system",
    # Carrier app installers / "content delivery" (DT Ignite family)
    "com.dti.att", "com.dti.tmobile", "com.dti.sprint", "com.dti.telstra",
    "com.aura.oobe", "com.aura.oobe.att", "com.aura.oobe.samsung",
    "com.ironsource.appcloud.oobe", "com.ironsource.appcloud.oobe.hutchison",
    # OEM ad/analytics services
    "com.miui.msa.global",            # Xiaomi ad service
    "com.miui.analytics",
    "com.samsung.android.mateagent",  # Samsung promotion agent
    "com.samsung.android.app.omcagent",
    # Lock-screen ads/content
    "com.glance.internet", "us.zoom.videomeetings.preload",
})


def _user_bloat():
    path = data_dir() / "bloatware.txt"
    if not path.exists():
        return set()
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeDecodeError):
        return set()
    return {ln.split("#", 1)[0].strip() for ln in lines} - {""}


def find_bloat(adb):
    """Installed system packages that are on the junk list, sorted."""
    out = adb.shell_text(["pm", "list", "packages", "-s"])
    installed = {ln.split(":", 1)[1].strip()
                 for ln in (out or "").splitlines() if ln.startswith("package:")}
    return sorted(installed & (BLOAT_SEED | _user_bloat()))
```

- [ ] **Step 2: Failing tests.** `tests/test_bloatware.py`:

```python
import pytest

import bloatware
from actions import ProtectedAppError, debloat, undo
from tests.test_actions import FakeAdb, log  # reuse fixtures


@pytest.fixture(autouse=True)
def _tmp_data_dir(monkeypatch, tmp_path):
    import adb
    monkeypatch.setattr(adb, "data_dir", lambda: tmp_path)


def test_find_bloat_matches_only_listed_system_packages(monkeypatch, tmp_path):
    class SysAdb(FakeAdb):
        def shell_text(self, args, timeout=10):
            if args == ["pm", "list", "packages", "-s"]:
                return ("package:com.facebook.appmanager\n"
                        "package:com.android.systemui\n")
            return super().shell_text(args, timeout)
    assert bloatware.find_bloat(SysAdb()) == ["com.facebook.appmanager"]


def test_user_bloat_file_extends_seed(tmp_path):
    (tmp_path / "bloatware.txt").write_text("com.carrier.junk  # verified\n",
                                            encoding="utf-8")
    class SysAdb(FakeAdb):
        def shell_text(self, args, timeout=10):
            if args == ["pm", "list", "packages", "-s"]:
                return "package:com.carrier.junk\n"
            return super().shell_text(args, timeout)
    assert bloatware.find_bloat(SysAdb()) == ["com.carrier.junk"]


def test_debloat_disables_and_refuses_unlisted(log):
    adb = FakeAdb()
    assert debloat(adb, "com.facebook.appmanager", log)
    assert "com.facebook.appmanager" in adb.disabled
    entry = log.recent()[0]
    assert entry["action"] == "debloat"
    undo(adb, entry, log)
    assert "com.facebook.appmanager" not in adb.disabled
    with pytest.raises(ProtectedAppError):
        debloat(adb, "com.android.systemui", log)   # not on the list -> refused
```

- [ ] **Step 3: Verify failure, implement `actions.debloat`** (add `"debloat"` to `UNDOABLE`; undo branch = the existing `pm enable` pattern used for `"pause"`):

```python
def debloat(adb, package, log):
    """Disable a preinstalled junk package. Only exact members of the curated
    bloat list may be touched -- the list is the safety authorization
    (uninstalling the wrong OEM package can bootloop a phone; disabling can't).
    """
    from bloatware import BLOAT_SEED, _user_bloat
    if package not in (BLOAT_SEED | _user_bloat()):
        raise ProtectedAppError(f"{package} is not on the bloatware list")
    cmd = ["pm", "disable-user", "--user", "0", package]
    adb.shell_text(cmd)
    ok = _is_disabled(adb, package)
    log.append(adb.serial, package, "debloat", "enabled", cmd, "ok" if ok else "failed")
    return ok
```

`undo()` branch: `elif action == "debloat":` → same `pm enable --user 0` fallback chain as the `"pause"` branch.

- [ ] **Step 4: Run + commit** — `python -m pytest tests/test_bloatware.py -q` → PASS. `git add bloatware.py actions.py tests/test_bloatware.py && git commit -m "Preinstalled bloatware: curated seed + user file, disable-first with undo"`

- [ ] **Step 5: GUI.** Device tab, new labelled row: `self.bloat_btn = self._flat_button(tab, "💤  Disable preinstalled junk", self.on_debloat, AMBER, AMBER_HOT)` added to `self.dev_btns`. Handler: `_run_bg` → `found = find_bloat(self.adb)`; if empty `_post` status "No known preinstalled junk on this phone." else `_post` an `askyesno` listing the packages (reuse `_confirm_bulk`-style name list), then `_run_bg` → `debloat` each (collect failures), `_post` status `"✅ Disabled N preinstalled junk app(s)."`. All disables land in History via the log, so undo works from the existing History tab. Smoke test: gui `FakeAdb` returns one seed package for `pm list packages -s`; assert it ends up in `app.adb.disabled` after `on_debloat` + pump.

- [ ] **Step 6: Full suite + ship** — `python -m pytest -q`; commit `"GUI: disable-preinstalled-junk button on the Device tab"`; ship flow (branch `feature/bloatware-debloat`).

---

### Task 5: Battery report (per-app drain + best-effort health)

**Files:**
- Create: `tests/fixtures/batterystats.txt`, `tests/fixtures/packages_uids.txt`
- Modify: `device.py` (parsers + `read_battery_report`), `gui.py` (Device tab lines + receipt key), `report.py` (render receipt line)
- Test: `tests/test_device.py` (create if absent — check first; if device parsers are tested elsewhere, follow that file)

**Interfaces:**
- Produces: `device.parse_uid_map(text) -> dict[str, str]` (uid string like `u0a231` → package), `device.parse_power_use(text) -> list[tuple[str, float]]` (uid-or-label, mAh, descending), `device.read_battery_report(adb, uid_map=None) -> dict` with keys `top_drainers: list[tuple[str, float]]` (package names where resolvable, max 5) and `health_pct: int | None` (Samsung `mSavedBatteryAsoc`, else None).

- [ ] **Step 1: Fixtures + failing tests.** `tests/fixtures/batterystats.txt` (trimmed real shape):

```
Estimated power use (mAh):
    Capacity: 4000, Computed drain: 812, actual drain: 700-900
    Uid u0a231: 145 ( cpu=90 wake=30 sensor=25 )
    Uid u0a145: 40.2 ( cpu=40.2 )
    Screen: 220
    Uid 1000: 88.1 ( cpu=88.1 )
```

`tests/fixtures/packages_uids.txt`:

```
package:com.random.freegift uid:10231
package:com.whatsapp uid:10145
```

Tests:

```python
def test_parse_uid_map():
    m = device.parse_uid_map(fx("packages_uids.txt"))
    assert m == {"u0a231": "com.random.freegift", "u0a145": "com.whatsapp"}


def test_parse_power_use_ranks_uids():
    top = device.parse_power_use(fx("batterystats.txt"))
    assert top[0] == ("u0a231", 145.0)
    assert ("u0a145", 40.2) in top
    assert device.parse_power_use("") == []
```

- [ ] **Step 2: Verify failure, implement in `device.py`:**

```python
def parse_uid_map(text):
    """`pm list packages -U` -> {'u0a231': package}. uid 10231 == u0a231."""
    out = {}
    for m in re.finditer(r"package:(\S+)\s+uid:(\d+)", text or ""):
        uid = int(m.group(2))
        if uid >= 10000:
            out[f"u0a{uid - 10000}"] = m.group(1)
    return out


def parse_power_use(text):
    """'Estimated power use' section of `dumpsys batterystats --charged`
    -> [(uid_str, mAh)] descending. Labels like 'Screen' are dropped."""
    rows = []
    for m in re.finditer(r"^\s+Uid (u0a\d+):\s+([\d.]+)", text or "", re.MULTILINE):
        rows.append((m.group(1), float(m.group(2))))
    return sorted(rows, key=lambda r: -r[1])


def read_battery_report(adb, uid_map=None):
    stats = _safe(adb, ["dumpsys", "batterystats", "--charged"])
    if uid_map is None:
        uid_map = parse_uid_map(_safe(adb, ["pm", "list", "packages", "-U", "-3"]))
    top = [(uid_map.get(uid, uid), mah)
           for uid, mah in parse_power_use(stats) if uid in uid_map][:5]
    asoc = re.search(r"mSavedBatteryAsoc:\s*(\d+)", _safe(adb, ["dumpsys", "battery"]))
    return {"top_drainers": top, "health_pct": int(asoc.group(1)) if asoc else None}
```

- [ ] **Step 3: Run + commit** — parser tests PASS. `git commit -am "Device: per-app battery drain + best-effort Samsung battery health"`

- [ ] **Step 4: Surface it.** Device tab: extend `read_device_stats` display block in gui to also run `read_battery_report` in the same background refresh and set two new `dev_vars` rows: `"Battery health"` → `"{health_pct}% of original capacity"` or `"—"`, `"Top battery user"` → `"label (145 mAh since last charge)"`. Store the report on the app object when the background refresh completes (`self.battery_report = read_battery_report(self.adb)`, default `self.battery_report = None` in `__init__`). Receipt (`gui.py:~1448` receipt dict): when `self.battery_report and self.battery_report["health_pct"]`, add `receipt["battery_health"] = f"{self.battery_report['health_pct']}% of original capacity"`; `report.render_receipt_html`: render the `battery_health` key, when present, as one row under the device stats section (follow the existing receipt row markup exactly). Smoke: gui FakeAdb serves both fixtures; assert the dev_var contains "mAh".

- [ ] **Step 5: Full suite + ship** — `python -m pytest -q`; commit `"Device tab + receipt: battery report"`; ship flow (branch `feature/battery-report`).

---

### Task 6: Background-data hogs + restrict

**Files:**
- Create: `tests/fixtures/netstats.txt`
- Modify: `device.py` (`parse_data_use`), `scanner.py` (attach `data_mb` to App in `build_inventory`), `actions.py` (`restrict_background`), `gui.py` (detail line + button)
- Test: `tests/test_device.py`/`tests/test_scanner.py`, `tests/test_actions.py`

**Interfaces:**
- Produces: `device.parse_data_use(text) -> dict[int, int]` (uid → total bytes), `App.data_mb: int = 0`, `App.uid: int = 0`, `actions.restrict_background(adb, package, uid, log) -> bool` (undoable; `cmd netpolicy` blocks background *metered* data only).

- [ ] **Step 1: Fixture + failing tests.** `tests/fixtures/netstats.txt`:

```
  uid=10231 set=DEFAULT tag=0x0 rb=52428800 rp=1200 tb=1048576 tp=300
  uid=10231 set=BACKGROUND tag=0x0 rb=31457280 rp=800 tb=524288 tp=100
  uid=10145 set=DEFAULT tag=0x0 rb=2097152 rp=90 tb=1024 tp=10
```

```python
def test_parse_data_use_sums_buckets_per_uid():
    use = device.parse_data_use(fx("netstats.txt"))
    assert use[10231] == 52428800 + 1048576 + 31457280 + 524288
    assert use[10145] == 2097152 + 1024
    assert device.parse_data_use("") == {}
```

```python
def test_restrict_background_uses_netpolicy_and_undoes(log):
    adb = FakeAdb()
    assert restrict_background(adb, "com.random.freegift", 10231, log)
    assert "cmd netpolicy add restrict-background-blacklist 10231" in adb.commands
    entry = log.recent()[0]
    assert can_undo(entry)
    undo(adb, entry, log)
    assert "cmd netpolicy remove restrict-background-blacklist 10231" in adb.commands
```

- [ ] **Step 2: Verify failure, implement.** `device.py`:

```python
def parse_data_use(text):
    """`dumpsys netstats` bucket lines -> {uid: total rx+tx bytes}. Defensive:
    OEM formats vary; anything that doesn't match the uid/rb/tb shape is skipped."""
    use = {}
    for m in re.finditer(r"uid=(\d+)\b[^\n]*?\brb=(\d+)[^\n]*?\btb=(\d+)",
                         text or ""):
        uid = int(m.group(1))
        use[uid] = use.get(uid, 0) + int(m.group(2)) + int(m.group(3))
    return use
```

`actions.py` (add `"restrict-data"` to `UNDOABLE`):

```python
def restrict_background(adb, package, uid, log):
    """Block the app's background mobile data (same as the per-app 'Background
    data' toggle -- Wi-Fi unaffected). Safe + reversible, no guard."""
    cmd = ["cmd", "netpolicy", "add", "restrict-background-blacklist", str(uid)]
    adb.shell_text(cmd)
    log.append(adb.serial, package, "restrict-data", str(uid), cmd, "ok")
    return True
```

`undo()` branch: `elif action == "restrict-data": cmd = ["cmd", "netpolicy", "remove", "restrict-background-blacklist", entry["previous"]]`.

`scanner.build_inventory`: fetch `uids = parse_uid_map_raw = adb.shell_text(["pm", "list", "packages", "-3", "-U"])` and `net = parse_data_use(_safe(lambda: adb.shell_text(["dumpsys", "netstats"])))` (import `parse_data_use` from device); when constructing each App set `uid` (from a `{pkg: int_uid}` dict built with the same regex as `device.parse_uid_map` but keyed by package) and `data_mb=net.get(uid, 0) // (1024 * 1024)`. Add both fixture routes to the scanner FakeAdb.

- [ ] **Step 3: Run + commit** — PASS; `git commit -am "Background-data usage per app + netpolicy restrict action"`

- [ ] **Step 4: GUI.** Detail pane: when `a.data_mb >= 1` append line `f"Data used: {a.data_mb} MB"`; button `"📵  Block background data"` (pattern of Task 2's detail button) calling `restrict_background(self.adb, a.package, a.uid, self.log)`. Smoke test asserts the netpolicy command lands in `app.adb.commands`.

- [ ] **Step 5: Full suite + ship** — `python -m pytest -q`; commit; ship flow (branch `feature/background-data`).

---

### Task 7: Screen time on detail + receipt

**Files:**
- Create: `tests/fixtures/usagestats.txt`
- Modify: `device.py` (`parse_usage_minutes`), `scanner.py` (attach `used_min`), `gui.py` (detail line; receipt top-3)
- Test: `tests/test_device.py`/`tests/test_scanner.py`

**Interfaces:**
- Produces: `device.parse_usage_minutes(text) -> dict[str, int]` (package → foreground minutes, best-effort), `App.used_min: int = 0`.

- [ ] **Step 1: Fixture + failing test.** `tests/fixtures/usagestats.txt`:

```
    package=com.random.freegift totalTimeUsed="04:20" lastTimeUsed="2024-06-01 09:00:00" totalTimeVisible="04:20"
    package=com.whatsapp totalTimeUsed="1:02:11" lastTimeUsed="2024-06-01 10:00:00" totalTimeVisible="1:02:11"
```

```python
def test_parse_usage_minutes():
    use = device.parse_usage_minutes(fx("usagestats.txt"))
    assert use["com.whatsapp"] == 62
    assert use["com.random.freegift"] == 4
    assert device.parse_usage_minutes("junk") == {}
```

- [ ] **Step 2: Implement:**

```python
def parse_usage_minutes(text):
    """`dumpsys usagestats` -> {package: foreground minutes}. Format is
    unstable across OEMs; matches both h:mm:ss and mm:ss time strings and
    keeps the largest value seen per package."""
    use = {}
    for m in re.finditer(
            r'package=(\S+)[^\n]*?totalTime(?:Used|Visible)="(?:(\d+):)?(\d+):(\d+)"',
            text or ""):
        pkg = m.group(1)
        h = int(m.group(2) or 0)
        mins = h * 60 + int(m.group(3))
        use[pkg] = max(use.get(pkg, 0), mins)
    return use
```

`scanner.build_inventory`: `usage = parse_usage_minutes(_safe(lambda: adb.shell_text(["dumpsys", "usagestats"])))`, set `used_min=usage.get(pkg, 0)` on each App; fixture route in scanner FakeAdb. Detail pane line when `a.used_min`: `f"Used about {a.used_min} min recently"` (helps the "never opened but always running" conversation). Receipt: add `"most_used"` key = top-3 `f"{label} ({min} min)"` joined; render in `report.render_receipt_html` as one row.

- [ ] **Step 3: Run, full suite, ship** — `python -m pytest -q`; commit `"Screen time: per-app usage in detail pane and on the receipt"`; ship flow (branch `feature/screen-time`).

---

### Task 8: Release

- [ ] **Step 1:** `python -m pytest -q` on master after all merges → all green (expect ~135+ tests).
- [ ] **Step 2:** Update README feature list (one line per new feature, keep the existing plain-English voice; include the Echap CC-BY credit if Task 3 didn't already).
- [ ] **Step 3:** Rebuild: `pyinstaller --onefile --windowed --name AdCleaner --add-data "platform-tools;platform-tools" main.py --noconfirm`; smoke-launch `dist/AdCleaner.exe` (window title "Ad Cleaner" appears; kill it).
- [ ] **Step 4:** `gh release create v<today's date YYYY.MM.DD> "dist/AdCleaner.exe#AdCleaner.exe (Windows, standalone)" --title "Ad Cleaner <date>" --notes "<summary of the seven features>"`.
- [ ] **Step 5:** `graphify update .`
