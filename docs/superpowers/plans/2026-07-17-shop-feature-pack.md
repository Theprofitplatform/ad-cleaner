# Shop Feature Pack Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Six shop-workflow features: APK backup safety net for undo, big-file finder, managed-phone (device-owner) detection, a "stop screen control" button, shop branding on printed reports, and Wi-Fi (wireless ADB) connect.

**Architecture:** Every feature follows the existing three-layer pattern: pure parse/decide functions in `scanner.py` / `device.py` / `adb.py` (unit-testable with text fixtures, no device), device actions with undo-logging in `actions.py` (tested with a FakeAdb), thin Tk wiring in `gui.py` (covered by `tests/test_gui_smoke.py`). No new dependencies, no network at app runtime.

**Tech Stack:** Python 3.12 stdlib + tkinter, adb (subprocess) over USB/Wi-Fi (no root, no on-phone agent), pytest.

## Global Constraints

- **No new runtime dependencies.** Stdlib only. No network calls at app runtime (Wi-Fi ADB is LAN, not internet — allowed).
- **Every device mutation goes through `actions.py`**, is logged via `log.append(adb.serial, package, action, previous, cmd, result)`, and is undoable where technically possible (add the action name to `UNDOABLE` and a branch to `undo()`). File deletion is explicitly NOT undoable — it must never enter `UNDOABLE`.
- **Protected apps are sacred**: destructive actions call `_guard(app)`.
- **All parsers are defensive**: shell output varies by OEM/Android version; every parser must return an empty/neutral value on garbage input, never raise.
- **Tests are hermetic**: `FakeAdb` classes, text fixtures in `tests/fixtures/`, no real adb. Every new `demo()` assertion must pass via `python <module>.py`.
- **Ship flow per task**: after the final commit of each task run `python -m pytest -q` (expect all green), then `git checkout -b <task-branch>`, push, `gh pr create`, wait for CI (`gh pr checks --watch`), `gh pr merge --merge`, `git checkout master && git pull`. Each task is one PR.
- **Existing interfaces used throughout** (do not redefine):
  - `Adb.run(args, timeout=10) -> str` (raises `AdbError` on nonzero exit), `Adb.shell_text(args, timeout=10) -> str`, `Adb.pull(remote, local, timeout=120)`.
  - `App` dataclass (`scanner.py:101`): fields include `package, label, installer, enabled, overlay, active_accessibility, device_admin, admin_component, uid, risk, reasons`; property `protected`; `label.split(" (")[0]` is the display name.
  - `ActionLog.append(serial, package, action, previous, command, result)` (`actions.py:49`); `UNDOABLE` set; `can_undo(entry)`; `undo(adb, entry, log)`; `backup_apk(adb, app, dest_dir) -> list[str]` (`actions.py:348`); `disable_accessibility(adb, package, log=None)` (`actions.py:259`); `ProtectedAppError`.
  - `data_dir()` from `adb.py` — app data folder, already imported in `actions.py` and `gui.py`.
  - GUI helpers: `self._flat_button(parent, text, cmd, color, hot)` (colors `GREEN/AMBER/RED/SLATE` + `_HOT` variants), `self._enable_btn(btn, bool)`, `self._run_bg(fn)`, `self._post(fn, *args)` (thread-safe UI), `self.status_line(msg, kind)`, `self._friendly(err)`, `self._refresh_history()`, `self._refresh_device()`, `self.busy` flag, `self._settings` dict + `self._save_settings()` (persists to `data_dir()/settings.json`).
  - Device tab: `self.dev_vars` (StringVars), `self.dev_labels`, rows built in `_build_device_tab` (`gui.py:523`), buttons row 1 `btns` / row 2 `btns2`, tuple `self.dev_btns` (`gui.py:578`) enables/disables on connect.
  - Detail pane: buttons built in `_build_apps_tab` (`gui.py:467-490`), tuple `self.detail_btns`, gating in `_update_detail` (`gui.py:1300`).
  - Wizard: `_build_wizard` (`gui.py:885`), panel styles `Panel.TFrame`/`PanelFlat.TFrame`/`PanelMuted.TLabel`.
  - Reports: `report.render_receipt_html(receipt)`, `report.render_intake_html(info)`; receipt built in `gui._save_receipt` (`gui.py:1697`), intake info in `gui.on_intake_report` (`gui.py:2002`); `_html_page(title, body)` helper in `report.py`.
  - Test plumbing: `tests/test_actions.py` `FakeAdb` (records `calls`+`commands`, per-command handlers in `shell_text`) + `log` fixture; `tests/test_gui_smoke.py` `FakeAdb(path, serial)` + `root` fixture + `pump(root, secs)`.

---

### Task 1: APK backup safety net (undo an uninstall always works)

`undo()` restores an uninstalled app with `cmd package install-existing`, which fails when Android no longer has the APK (common for sideloaded apps). Fix: `uninstall()` pulls the APK(s) first (best-effort, via the existing `backup_apk`), records the saved paths on the log entry, and `undo()` falls back to `adb install` from the backup.

**Files:**
- Modify: `actions.py` (`ActionLog.append`, `uninstall`, `undo`)
- Test: `tests/test_actions.py`

**Interfaces:**
- Consumes: `backup_apk(adb, app, dest_dir) -> list[str]`, `data_dir()`, `Adb.run`.
- Produces: log entries for `uninstall` gain an optional `"apk": [local paths]` key; `ActionLog.append(..., apk=None)` optional kwarg; `undo()` uses `adb.run(["install", "-r", path])` / `["install-multiple", "-r", *paths]` as fallback.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_actions.py`)

```python
def test_uninstall_backs_up_apk_first(log, tmp_path, monkeypatch):
    monkeypatch.setattr(actions, "data_dir", lambda: tmp_path)
    adb = FakeAdb()
    app = App(package="com.random.adware", installer=None)
    assert uninstall(adb, app, log) is True
    entry = log.recent()[0]
    assert entry["apk"] == [str(tmp_path / "apk_backups" / "com.random.adware.apk")]
    assert Path(entry["apk"][0]).exists()


def test_undo_uninstall_prefers_install_existing(log, tmp_path, monkeypatch):
    monkeypatch.setattr(actions, "data_dir", lambda: tmp_path)
    adb = FakeAdb()
    app = App(package="com.random.adware", installer=None)
    uninstall(adb, app, log)
    assert undo(adb, log.recent()[0], log) is True
    assert "com.random.adware" in adb.installed
    assert not any(c and c[0] == "install" for c in adb.calls)


def test_undo_uninstall_falls_back_to_saved_apk(log, tmp_path, monkeypatch):
    monkeypatch.setattr(actions, "data_dir", lambda: tmp_path)
    adb = FakeAdb()
    adb.gone_for_good = {"com.random.adware"}     # install-existing will fail
    app = App(package="com.random.adware", installer=None)
    uninstall(adb, app, log)
    assert undo(adb, log.recent()[0], log) is True
    apk = str(tmp_path / "apk_backups" / "com.random.adware.apk")
    assert ["install", "-r", apk] in adb.calls


def test_undo_uninstall_no_backup_no_apk_raises(log, tmp_path, monkeypatch):
    monkeypatch.setattr(actions, "data_dir", lambda: tmp_path)
    adb = FakeAdb()
    adb.gone_for_good = {"com.random.adware"}
    app = App(package="com.random.adware", installer=None)
    uninstall(adb, app, log)
    entry = dict(log.recent()[0], apk=[])          # simulate a failed backup
    with pytest.raises(AdbError):
        undo(adb, entry, log)
```

`FakeAdb` in `tests/test_actions.py` needs: `self.gone_for_good = set()` in `__init__`, a `run` method, and an updated `install-existing` handler:

```python
    def run(self, args, timeout=120):
        self.calls.append(list(args))
        self.commands.append(" ".join(args))
        if args and args[0] in ("install", "install-multiple"):
            self.installed.add("com.random.adware")
            return "Success"
        return ""
```

and replace the existing `install-existing` branch in `shell_text` with:

```python
        if args[:3] == ["cmd", "package", "install-existing"]:
            if args[-1] in self.gone_for_good:
                raise AdbError(f"Package {args[-1]} doesn't exist")
            self.installed.add(args[-1]); return "installed"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_actions.py -k "backs_up_apk or undo_uninstall" -v`
Expected: FAIL (KeyError `'apk'` / `install` never called).

- [ ] **Step 3: Implement**

In `actions.py`, `ActionLog.append` — add optional kwarg and key:

```python
    def append(self, serial, package, action, previous, command, result, apk=None):
        entry = {
            "time": datetime.now().isoformat(timespec="seconds"),
            "serial": serial,
            "package": package,
            "action": action,
            "previous": previous,
            "command": " ".join(command) if isinstance(command, list) else command,
            "result": result,
        }
        if apk:
            entry["apk"] = apk
        self.entries.append(entry)
        self._save()
        return entry
```

`uninstall()` — back up first (best-effort; a failed pull must never block the removal), and record the paths:

```python
def uninstall(adb, app, log):
    """Remove for user 0. Auto-neutralises accessibility + device-admin first,
    and saves the APK(s) so History → Undo works even for sideloaded apps."""
    _guard(app)
    if getattr(app, "active_accessibility", False):
        try:
            disable_accessibility(adb, app.package, log)  # stop it re-granting/blocking
        except AdbError:
            pass
    saved = []
    try:
        saved = backup_apk(adb, app, data_dir() / "apk_backups")
    except Exception:
        pass          # ponytail: best-effort net; install-existing still covers most apps
    cmd = ["pm", "uninstall", "--user", "0", app.package]
    try:
        adb.shell_text(cmd)
    except AdbError:
        if app.device_admin and app.admin_component:
            adb.shell_text(["dpm", "remove-active-admin", app.admin_component])
            adb.shell_text(cmd)  # retry
        else:
            raise
    ok = not _is_installed(adb, app.package)
    log.append(adb.serial, app.package, "uninstall", app.status, cmd,
               "ok" if ok else "failed", apk=saved or None)
    return ok
```

`undo()` — replace the `uninstall` branch:

```python
    elif action == "uninstall":
        cmd = ["cmd", "package", "install-existing", pkg]
        try:
            adb.shell_text(cmd)
            restored = _installed(adb, pkg)
        except AdbError:
            restored = False
        if not restored:
            apks = [p for p in entry.get("apk") or [] if Path(p).exists()]
            if not apks:
                raise AdbError("Android no longer has this app's install file and "
                               "no backup was saved, so it can't be restored.")
            cmd = (["install", "-r"] + apks if len(apks) == 1
                   else ["install-multiple", "-r"] + apks)
            adb.run(cmd, timeout=120)
```

Note `_installed` (pm path probe) already exists at `actions.py:287`. `Path` is already imported.

- [ ] **Step 4: Run the full actions suite**

Run: `python -m pytest tests/test_actions.py -v` then `python actions.py`
Expected: all PASS (existing `test_undo_reinstalls` style tests still green — `FakeAdb.pull` writes the file, so entries gain `apk` harmlessly), demo prints `actions.py demo OK`.

- [ ] **Step 5: Commit**

```bash
git add actions.py tests/test_actions.py
git commit -m "feat: back up APKs before uninstall so undo always restores"
```

---

### Task 2: Big-file finder (the "phone is full" fix)

Cache-clearing already exists; the other half of "phone is full" is forgotten giant files — old videos, Downloads junk, WhatsApp media. Find them with one shell command, list them biggest-first, let the tech delete selected ones (permanent, clearly labelled).

**Files:**
- Modify: `device.py` (parser + reader), `actions.py` (`delete_file`), `gui.py` (Device-tab button + window)
- Test: `tests/test_device.py`, `tests/test_actions.py`, `tests/test_gui_smoke.py`

**Interfaces:**
- Produces: `device.parse_big_files(text) -> list[(path, mb)]` biggest first; `device.read_big_files(adb, min_mb=100) -> list[(path, mb)]`; `actions.delete_file(adb, path, log) -> True` (raises `ProtectedAppError` off shared storage; action `"delete-file"`, NOT in `UNDOABLE`).

- [ ] **Step 1: Write the failing parser tests** (append to `tests/test_device.py`)

```python
def test_parse_big_files():
    out = ("512\t/storage/emulated/0/Movies/holiday video (1).mp4\n"
           "du: /storage/emulated/0/Android/data: Permission denied\n"
           "1300\t/storage/emulated/0/Download/game.apk\n"
           "garbage line\n")
    assert device.parse_big_files(out) == [
        ("/storage/emulated/0/Download/game.apk", 1300),
        ("/storage/emulated/0/Movies/holiday video (1).mp4", 512),
    ]
    assert device.parse_big_files("") == []
    assert device.parse_big_files(None) == []
```

(`tests/test_device.py` already does `import device`; if it imports names instead, add `import device`.)

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_device.py::test_parse_big_files -v`
Expected: FAIL with "has no attribute 'parse_big_files'".

- [ ] **Step 3: Implement parser + reader** (append to `device.py`, before `demo()`)

```python
def parse_big_files(text):
    """`du -m` lines ('123<TAB>/path with spaces') -> [(path, mb)] biggest
    first. Error lines and anything not size-then-absolute-path are skipped."""
    rows = []
    for line in (text or "").splitlines():
        m = re.match(r"^(\d+)\s+(/.+)$", line.rstrip())
        if m:
            rows.append((m.group(2), int(m.group(1))))
    return sorted(rows, key=lambda r: -r[1])


def read_big_files(adb, min_mb=100):
    """Biggest files on shared storage (photos, videos, downloads live here).
    App-private dirs under Android/ are invisible to the shell on Android 11+
    and make `find` exit nonzero; 2>/dev/null + `|| true` keep that noise from
    failing the whole run (adb shell passes these through to the phone's sh)."""
    try:
        out = adb.shell_text(
            ["find", "/storage/emulated/0", "-type", "f", "-size", f"+{min_mb}M",
             "-exec", "du", "-m", "{}", "+", "2>/dev/null", "||", "true"],
            timeout=120)
    except Exception:
        return []
    return parse_big_files(out)
```

Add to `device.py`'s `demo()`:

```python
    big = parse_big_files("512\t/storage/emulated/0/a b.mp4\ndu: x: denied\n")
    assert big == [("/storage/emulated/0/a b.mp4", 512)]
```

- [ ] **Step 4: Run parser tests**

Run: `python -m pytest tests/test_device.py -v` then `python device.py`
Expected: PASS / `device.py demo OK`.

- [ ] **Step 5: Write the failing action tests** (append to `tests/test_actions.py`)

```python
def test_delete_file_removes_shared_storage_file(log):
    adb = FakeAdb()
    assert actions.delete_file(adb, "/storage/emulated/0/Movies/big file.mp4", log) is True
    assert ["rm", "-f", "--", "'/storage/emulated/0/Movies/big file.mp4'"] in adb.calls
    entry = log.recent()[0]
    assert entry["action"] == "delete-file" and not can_undo(entry)


@pytest.mark.parametrize("path", [
    "/data/app/com.foo/base.apk",              # not shared storage
    "/storage/emulated/0/../data/secret",      # traversal
    "relative/path.mp4",                       # not absolute
])
def test_delete_file_refuses_unsafe_paths(log, path):
    adb = FakeAdb()
    with pytest.raises(ProtectedAppError):
        actions.delete_file(adb, path, log)
    assert not any(c and c[0] == "rm" for c in adb.calls)
```

`FakeAdb.shell_text` needs one handler:

```python
        if args[:2] == ["rm", "-f"]:
            return ""
```

- [ ] **Step 6: Run to verify they fail, then implement** (append to `actions.py`, near `clear_caches`)

```python
_SHARED_PREFIXES = ("/storage/emulated/0/", "/sdcard/")


def delete_file(adb, path, log):
    """Permanently delete ONE file on shared storage (big-file cleanup).

    NOT undoable, so the guard is hard: shared-storage prefix only, no
    traversal. shlex.quote survives the round trip through `adb shell`'s
    re-parsing, so spaces/parens in filenames can't split into extra args.
    A directory or an undeletable file makes rm exit nonzero -> AdbError.
    """
    if ".." in path or not path.startswith(_SHARED_PREFIXES):
        raise ProtectedAppError(f"refusing to delete {path!r} — not shared storage")
    cmd = ["rm", "-f", "--", shlex.quote(path)]
    adb.shell_text(cmd)
    log.append(adb.serial, "(file)", "delete-file", path, cmd, "ok")
    return True
```

Add `import shlex` to the imports at the top of `actions.py`.

Run: `python -m pytest tests/test_actions.py -k delete_file -v`
Expected: PASS.

- [ ] **Step 7: Wire the GUI**

In `gui.py` imports: extend the `device` import line (currently importing `read_device_stats` etc.) with `read_big_files`, and the `actions` import block with `delete_file`.

In `_build_device_tab`, after `self.bloat_btn` creation (`gui.py:574-577`), add the button to `btns2` and to the `dev_btns` tuple:

```python
        self.bigfiles_btn = self._flat_button(btns2, "🗂  Find big files",
                                              self.on_big_files, GREEN, GREEN_HOT)
```

— include `self.bigfiles_btn` in the `for b in (...)` pack loop for `btns2` and append it to the `self.dev_btns` tuple at `gui.py:578`.

Handlers (add near `on_clear_caches`):

```python
    def on_big_files(self):
        if not self.serial:
            return
        self._enable_btn(self.bigfiles_btn, False)
        self.status_line("Looking for big files… this can take a minute on a full phone.")

        def work():
            rows = read_big_files(self.adb)
            self._post(self._show_big_files, rows)

        self._run_bg(work)

    def _show_big_files(self, rows):
        self._enable_btn(self.bigfiles_btn, True)
        self.status_line("")
        win = tk.Toplevel(self.root)
        win.title("Big files on this phone")
        win.configure(bg=BASE)
        tk.Label(win, text="The biggest files on the phone's shared storage — old videos "
                           "and downloads usually live here. Deleting is permanent (files "
                           "do not go to a recycle bin).",
                 bg=BASE, fg=INK, padx=12, pady=8, wraplength=620,
                 justify="left").pack(anchor="w")
        t = ttk.Treeview(win, columns=("file", "size"), show="headings",
                         height=16, selectmode="extended")
        t.heading("file", text="File")
        t.heading("size", text="Size")
        t.column("file", width=520, anchor="w")
        t.column("size", width=90, anchor="e")
        for path, mb in rows:
            short = path.replace("/storage/emulated/0/", "").replace("/sdcard/", "")
            size = f"{mb / 1024:.1f} GB" if mb >= 1024 else f"{mb} MB"
            t.insert("", "end", iid=path, values=(short, size))
        if not rows:
            t.insert("", "end", values=("No files over 100 MB found "
                                        "(or the storage couldn't be read).", ""))
        t.pack(fill="both", expand=True, padx=10, pady=(0, 6))
        self.bigfiles_tree = t          # tests
        row = ttk.Frame(win)
        row.pack(pady=(0, 10))
        self._flat_button(row, "🗑  Delete selected",
                          lambda: self._delete_big_files(win, t), RED, RED_HOT).pack()

    def _delete_big_files(self, win, tree):
        paths = [iid for iid in tree.selection() if iid.startswith("/")]
        if not paths or self.busy:
            return
        if not messagebox.askyesno(
                "Delete files",
                f"Permanently delete {len(paths)} file(s) from the phone?\n\n"
                "This cannot be undone.", default="no", parent=win):
            return
        self.busy = True

        def work():
            gone, err = [], None
            for p in paths:
                try:
                    delete_file(self.adb, p, self.log)
                    gone.append(p)
                except Exception as e:
                    err = str(e)
            self._post(self._big_files_done, tree, gone, err)

        self._run_bg(work)

    def _big_files_done(self, tree, gone, err):
        self.busy = False
        for p in gone:
            if tree.exists(p):
                tree.delete(p)
        self._refresh_history()
        self._refresh_device()
        if err:
            self.status_line("Some files couldn't be deleted. " + self._friendly(err),
                             "error")
        else:
            self.status_line(f"✅ Deleted {len(gone)} file(s).", "good")
```

- [ ] **Step 8: Write the GUI smoke test** (append to `tests/test_gui_smoke.py`; use the existing connected-GUI fixture pattern in that file — build the gui, connect the FakeAdb, then:)

```python
def test_big_files_window_deletes_selected(root, monkeypatch, tmp_path):
    g = _connected_gui(root, monkeypatch, tmp_path)   # use this file's existing helper/pattern
    g._show_big_files([("/storage/emulated/0/Movies/big.mp4", 500)])
    g.bigfiles_tree.selection_set("/storage/emulated/0/Movies/big.mp4")
    monkeypatch.setattr(gui.messagebox, "askyesno", lambda *a, **k: True)
    g._delete_big_files(g.bigfiles_tree.master, g.bigfiles_tree)
    pump(root, 0.5)
    assert any(c[:2] == ["rm", "-f"] for c in g.adb.calls)
    assert not g.bigfiles_tree.exists("/storage/emulated/0/Movies/big.mp4")
```

(Adapt the first line to however the other smoke tests construct a connected gui — several already do; copy that pattern, don't invent a new helper. `FakeAdb.shell_text` in this file needs the same `rm -f` handler returning `""`.)

- [ ] **Step 9: Run everything, commit**

Run: `python -m pytest -q` — all green; `python device.py`, `python actions.py` — demos OK.

```bash
git add device.py actions.py gui.py tests/test_device.py tests/test_actions.py tests/test_gui_smoke.py
git commit -m "feat: big-file finder with delete on the Device tab"
```

---

### Task 3: Managed-phone detection (rogue device owner / work profile)

A Device Owner fully controls the phone (legit corporate MDM — or a scam "support" app); a Profile Owner on the personal user is the work-profile trick stalkerware uses. Detect both from `dumpsys device_policy`, show it on the Device tab, and print it on the intake condition report.

**Files:**
- Modify: `scanner.py` (`parse_owners`), `gui.py` (Device-tab row + intake info), `report.py` (intake row)
- Test: `tests/test_scanner.py`, `tests/test_report.py`, `tests/test_gui_smoke.py`

**Interfaces:**
- Produces: `scanner.parse_owners(output) -> {"device": pkg|None, "profile": pkg|None}`; `gui.self.owners` (same dict, or `None` before read); intake info dict gains optional `"managed"` string; `render_intake_html` prints it.

- [ ] **Step 1: Write the failing parser test** (append to `tests/test_scanner.py`)

```python
def test_parse_owners():
    out = ("Current Device Policy Manager state:\n"
           "  Device Owner: \n"
           "    admin=ComponentInfo{com.mdm.corp/com.mdm.corp.Admin}\n"
           "    name=Corp MDM\n"
           "  Profile Owner (User 0): \n"
           "    admin=ComponentInfo{com.spy.hidden/com.spy.hidden.P}\n")
    assert parse_owners(out) == {"device": "com.mdm.corp", "profile": "com.spy.hidden"}
    assert parse_owners("") == {"device": None, "profile": None}
    assert parse_owners(None) == {"device": None, "profile": None}
    # a dump with per-app admins but no owner block stays None
    assert parse_owners(fx("device_policy.txt")) == {"device": None, "profile": None}
```

Add `parse_owners` to the `from scanner import (...)` list at the top.

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_scanner.py::test_parse_owners -v`
Expected: FAIL with ImportError.

- [ ] **Step 3: Implement** (in `scanner.py`, right after `parse_device_admins`)

```python
def parse_owners(output):
    """`dumpsys device_policy` -> {'device': pkg or None, 'profile': pkg or None}.

    A Device Owner fully controls the phone (MDM — or a scam 'support' app);
    a Profile Owner on user 0 is the work-profile trick stalkerware uses.
    The 400-char window keeps the search inside the owner's own block so a
    later per-app admin's ComponentInfo can't be misattributed.
    """
    def owner_after(header):
        m = re.search(header + r".{0,400}?ComponentInfo\{([\w.]+)/", output or "", re.S)
        return m.group(1) if m else None
    return {"device": owner_after(r"Device Owner:"),
            "profile": owner_after(r"Profile Owner \(User 0\):")}
```

Add to `scanner.py`'s `demo()`:

```python
    owners = parse_owners("Device Owner:\n  admin=ComponentInfo{com.mdm.x/.A}\n")
    assert owners == {"device": "com.mdm.x", "profile": None}
```

Run: `python -m pytest tests/test_scanner.py -v` and `python scanner.py` — PASS.

- [ ] **Step 4: Wire the Device tab + intake report**

`gui.py`:
1. Import: add `parse_owners` to the existing `from scanner import ...` line.
2. `__init__` (near `self.battery_report = None`, `gui.py:201`): add `self.owners = None`.
3. `_build_device_tab` rows list (`gui.py:536-539`): append `("🏢  Managed / work profile", "owner")` and add `"owner"` to the `dev_vars` key tuple at `gui.py:530-532`.
4. `_refresh_device` `work()` (`gui.py:1823`): add a third block after the battery report:

```python
            try:
                owners = parse_owners(self.adb.shell_text(["dumpsys", "device_policy"]))
                self._post(self._show_owners, owners)
            except Exception:
                pass
```

5. New method after `_show_battery_report`:

```python
    def _show_owners(self, owners):
        self.owners = owners
        dev, prof = owners.get("device"), owners.get("profile")
        if dev or prof:
            kind = "device owner" if dev else "work profile"
            self.dev_vars["owner"].set(f"⚠ Controlled by {dev or prof} ({kind}) — "
                                       "ask if the customer expects this")
            self.dev_labels["owner"].config(foreground="#b45309")
        else:
            self.dev_vars["owner"].set("none — not a managed phone")
            self.dev_labels["owner"].config(foreground=INK)
```

6. `_disconnect` (`gui.py:1127`): after `self.battery_report = None`, add `self.owners = None`.
7. `on_intake_report` `info` dict (`gui.py:2014`): add

```python
                "managed": (lambda o: (f"{o['device']} (device owner)" if o and o.get("device")
                            else f"{o['profile']} (work profile)" if o and o.get("profile")
                            else ""))(getattr(self, "owners", None)),
```

`report.py` `render_intake_html`: in the Device section after `row("Serial number", "serial")`, add `+ row("Managed by (MDM / work profile)", "managed")`.

- [ ] **Step 5: Tests for the wiring**

Append to `tests/test_report.py`:

```python
def test_intake_shows_managed_phone():
    out = report.render_intake_html({"app_count": 0, "managed": "com.mdm.corp (device owner)"})
    assert "Managed by" in out and "com.mdm.corp (device owner)" in out
    assert "Managed by" not in report.render_intake_html({"app_count": 0})
```

Append to `tests/test_gui_smoke.py` (same connected-gui pattern as Task 2):

```python
def test_owner_row_warns_on_managed_phone(root, monkeypatch, tmp_path):
    g = _connected_gui(root, monkeypatch, tmp_path)
    g._show_owners({"device": "com.mdm.corp", "profile": None})
    assert "com.mdm.corp" in g.dev_vars["owner"].get()
    g._show_owners({"device": None, "profile": None})
    assert "not a managed" in g.dev_vars["owner"].get()
```

- [ ] **Step 6: Run everything, commit**

Run: `python -m pytest -q`; `python scanner.py`; `python report.py`.

```bash
git add scanner.py gui.py report.py tests/test_scanner.py tests/test_report.py tests/test_gui_smoke.py
git commit -m "feat: detect device-owner / work-profile management, show on Device tab + intake report"
```

---

### Task 4: "Stop screen control" button (expose the accessibility kill, undoable)

The scan already flags `active_accessibility` and `uninstall` auto-neutralises it — but there's no way to switch OFF an app's screen control while keeping the app. Expose `disable_accessibility` as a detail-pane button and make it undoable (the removed service entries go in the log's `previous` field so undo can restore them).

**Files:**
- Modify: `actions.py` (`disable_accessibility` logging, `UNDOABLE`, `undo`), `gui.py` (detail button + handler)
- Test: `tests/test_actions.py`, `tests/test_gui_smoke.py`

**Interfaces:**
- Consumes: `disable_accessibility(adb, package, log=None)` (`actions.py:259`), `App.active_accessibility`.
- Produces: log action `"disable-accessibility"` with `previous` = `":"`-joined removed `pkg/Service` entries; in `UNDOABLE`; undo re-adds them and sets `accessibility_enabled 1`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_actions.py`)

```python
def test_disable_accessibility_is_undoable(log):
    adb = FakeAdb()
    adb.a11y = "com.evil.admin/.Spy:com.ok.app/.Helper"
    disable_accessibility(adb, "com.evil.admin", log)
    entry = log.recent()[0]
    assert can_undo(entry)
    assert entry["previous"] == "com.evil.admin/.Spy"
    assert undo(adb, entry, log) is True
    assert "com.evil.admin/.Spy" in adb.a11y and "com.ok.app/.Helper" in adb.a11y
    assert ["settings", "put", "secure", "accessibility_enabled", "1"] in adb.calls
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_actions.py::test_disable_accessibility_is_undoable -v`
Expected: FAIL (`can_undo` is False).

- [ ] **Step 3: Implement**

`actions.py`:
1. Add `"disable-accessibility"` to `UNDOABLE` (`actions.py:18`).
2. In `disable_accessibility`, capture what's removed and log it as `previous`:

```python
def disable_accessibility(adb, package, log=None):
    """Switch OFF an app's accessibility service so it can't control the
    screen (or block its own removal). Reads the enabled list, drops the
    offender's entries, writes the rest back (shell holds
    WRITE_SECURE_SETTINGS — no root). Undo restores the dropped entries."""
    cur = adb.shell_text(["settings", "get", "secure", "enabled_accessibility_services"])
    entries = [s for s in (cur or "").strip().split(":") if s and s != "null"]
    removed = [s for s in entries if s.startswith(package + "/")]
    kept = [s for s in entries if not s.startswith(package + "/")]
    value = ":".join(kept) if kept else "null"
    adb.shell_text(["settings", "put", "secure", "enabled_accessibility_services", value])
    if not kept:
        adb.shell_text(["settings", "put", "secure", "accessibility_enabled", "0"])
    if log is not None:
        log.append(adb.serial, package, "disable-accessibility", ":".join(removed),
                   ["settings", "put", "secure", "enabled_accessibility_services"], "ok")
    return True
```

3. New `undo()` branch (before the final `else`):

```python
    elif action == "disable-accessibility":
        removed = entry.get("previous") or ""
        if not removed:
            raise AdbError("Nothing recorded to restore for this entry.")
        cur = (adb.shell_text(["settings", "get", "secure",
                               "enabled_accessibility_services"]) or "").strip()
        cur = "" if cur in ("", "null") else cur
        value = ":".join(x for x in (cur, removed) if x)
        cmd = ["settings", "put", "secure", "enabled_accessibility_services", value]
        adb.shell_text(cmd)
        adb.shell_text(["settings", "put", "secure", "accessibility_enabled", "1"])
```

Run: `python -m pytest tests/test_actions.py -v` — all PASS (the two existing `disable_accessibility` tests don't assert `previous`, so they stay green).

- [ ] **Step 4: Wire the detail-pane button**

`gui.py`:
1. Import `disable_accessibility` in the `from actions import ...` block.
2. In `_build_apps_tab` after `self.data_btn` (`gui.py:483-484`):

```python
        self.a11y_btn = self._flat_button(btns, "🖐  Stop screen control",
                                          self.on_disable_a11y, RED, RED_HOT)
```

   and add `self.a11y_btn` to the `self.detail_btns` tuple (`gui.py:485-487`).
3. Gating in `_update_detail` after the `data_btn` line (`gui.py:1341`):

```python
        self._enable_btn(self.a11y_btn, a.active_accessibility)
```

4. Handlers (near `on_block_notifs`):

```python
    def on_disable_a11y(self):
        a = self.selected
        if not a or self.busy or not self.serial:
            return
        label = a.label.split(" (")[0]

        def work():
            try:
                disable_accessibility(self.adb, a.package, self.log)
                self._post(self._a11y_done, a, label, None)
            except AdbError as e:
                self._post(self._a11y_done, a, label, str(e))

        self.busy = True
        self._run_bg(work)

    def _a11y_done(self, app, label, err):
        self.busy = False
        self._refresh_history()
        if err:
            self.status_line("Couldn't switch it off. " + self._friendly(err), "error")
        else:
            app.active_accessibility = False
            self.status_line(f"✅ {label} can no longer control the screen.", "good")
            self._update_detail()
```

- [ ] **Step 5: Smoke test** (append to `tests/test_gui_smoke.py`; the file's `FakeAdb.shell_text` needs a generic secure-put handler — add `if args[:3] == ["settings", "put", "secure"]: return ""`)

```python
def test_stop_screen_control_button(root, monkeypatch, tmp_path):
    g = _connected_gui(root, monkeypatch, tmp_path)
    spy = App(package="com.random.adware", installer=None, active_accessibility=True)
    score_app(spy, NOW)
    g.apps = [spy]
    g.selected = spy
    g._update_detail()
    assert str(g.a11y_btn["state"]) == "normal"
    g.on_disable_a11y()
    pump(root, 0.5)
    assert spy.active_accessibility is False
    assert any(c[:4] == ["settings", "put", "secure", "enabled_accessibility_services"]
               for c in g.adb.calls)
```

- [ ] **Step 6: Run everything, commit**

Run: `python -m pytest -q`; `python actions.py`.

```bash
git add actions.py gui.py tests/test_actions.py tests/test_gui_smoke.py
git commit -m "feat: Stop screen control button — undoable accessibility kill"
```

---

### Task 5: Shop branding on printed reports

The clean receipt and intake condition report are handed to customers — put the shop's name and contact line at the top. Stored in the existing `settings.json`; edited via a small dialog on the History tab (next to Export report).

**Files:**
- Modify: `report.py` (`_shop_header` + both renderers), `gui.py` (settings merge, dialog, receipt/intake dicts)
- Test: `tests/test_report.py`, `tests/test_gui_smoke.py`

**Interfaces:**
- Produces: receipt/intake dicts accept optional `"shop_name"`, `"shop_contact"`; `self._settings["shop_name"/"shop_contact"]` persisted.

- [ ] **Step 1: Write the failing report tests** (append to `tests/test_report.py`)

```python
def test_shop_header_on_receipt_and_intake():
    r = {"when": "t", "stopped": 1, "acted": 0,
         "shop_name": "Phone Fix <Bros>", "shop_contact": "07 5555 5555 · fix.example"}
    out = report.render_receipt_html(r)
    assert "Phone Fix &lt;Bros&gt;" in out and "07 5555 5555" in out
    intake = report.render_intake_html({"app_count": 0, "shop_name": "Phone Fix <Bros>"})
    assert "Phone Fix &lt;Bros&gt;" in intake
    assert "Phone Fix" not in report.render_receipt_html({"when": "t"})
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_report.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement in `report.py`** (helper above `render_receipt_html`)

```python
def _shop_header(d: dict) -> str:
    """Shop name + contact line above the report title, when configured."""
    name = d.get("shop_name")
    if not name:
        return ""
    contact = d.get("shop_contact") or ""
    out = f"<p style='font-size:17px;font-weight:bold;margin:0'>{html.escape(name)}</p>"
    if contact:
        out += f"<p class='muted' style='margin:0 0 14px'>{html.escape(contact)}</p>"
    else:
        out += "<div style='margin-bottom:14px'></div>"
    return out
```

In `render_receipt_html`, change the start of `body` to `_shop_header(r) + "<h1>Ad Cleaner — clean receipt</h1>" ...`; in `render_intake_html`, change it to `_shop_header(i) + "<h1>Phone condition report</h1>" ...`.

Add to `report.py` `demo()`:

```python
    branded = render_receipt_html({**r, "shop_name": "Fix<It>", "shop_contact": "ph 1234"})
    assert "Fix&lt;It&gt;" in branded and "ph 1234" in branded
```

Run: `python -m pytest tests/test_report.py -v` and `python report.py` — PASS.

- [ ] **Step 4: Wire the GUI**

1. `_save_settings` (`gui.py:243`) — persist unknown keys too, so shop details survive:

```python
    def _save_settings(self):
        try:
            (data_dir() / "settings.json").write_text(json.dumps({
                **self._settings,
                "shop_mode": self.shop_mode.get(),
                "uninstall_mode": self.uninstall_mode.get(),
                "brand": self.brand_var.get(),
            }, indent=2), encoding="utf-8")
        except Exception:
            pass
```

2. History tab (`_build_history_tab`, after `self.export_btn` at `gui.py:516-518`):

```python
        self.shopinfo_btn = self._flat_button(row, "🏪  Shop details…",
                                              self.on_shop_details, SLATE, SLATE_HOT)
        self.shopinfo_btn.pack(side="left", padx=6)
```

   and add it to the always-enabled loop `for b in (self.undo_btn, self.export_btn, self.shopinfo_btn):`.

3. Dialog (near `on_export`):

```python
    def on_shop_details(self):
        """Shop name + contact printed on receipts and condition reports."""
        win = tk.Toplevel(self.root)
        win.title("Shop details")
        win.configure(bg=BASE)
        win.transient(self.root)
        win.grab_set()
        ttk.Label(win, text="Printed at the top of receipts and condition reports.\n"
                            "Leave blank for plain reports.",
                  justify="left").grid(row=0, column=0, columnspan=2,
                                       padx=14, pady=(12, 8), sticky="w")
        name = tk.StringVar(value=self._settings.get("shop_name", ""))
        contact = tk.StringVar(value=self._settings.get("shop_contact", ""))
        for r, (lbl, var) in enumerate((("Shop name", name),
                                        ("Phone / website", contact)), start=1):
            ttk.Label(win, text=lbl).grid(row=r, column=0, sticky="e",
                                          padx=(14, 8), pady=4)
            ttk.Entry(win, textvariable=var, width=34).grid(row=r, column=1,
                                                            sticky="w",
                                                            padx=(0, 14), pady=4)

        def save():
            self._settings["shop_name"] = name.get().strip()
            self._settings["shop_contact"] = contact.get().strip()
            self._save_settings()
            win.destroy()
            self.status_line("✅ Shop details saved — they'll print on every report.",
                             "good")

        self._flat_button(win, "💾  Save", save, GREEN, GREEN_HOT).grid(
            row=3, column=0, columnspan=2, pady=(10, 14))
```

4. `_save_receipt` (`gui.py:1697`) — after the `receipt = {...}` literal:

```python
            if self._settings.get("shop_name"):
                receipt["shop_name"] = self._settings["shop_name"]
                receipt["shop_contact"] = self._settings.get("shop_contact", "")
```

5. `on_intake_report` — same two lines on the `info` dict, right after it's built.

- [ ] **Step 5: Smoke test** (append to `tests/test_gui_smoke.py`)

```python
def test_receipt_carries_shop_details(root, monkeypatch, tmp_path):
    g = _connected_gui(root, monkeypatch, tmp_path)
    g._settings["shop_name"] = "Fix It Bros"
    path = g._save_receipt({"stopped": 1, "acted": 0, "removed": False,
                            "popups_blocked": 0, "packages": [], "dns": "Off",
                            "freed_gb": 0})
    assert path and "Fix It Bros" in path.read_text(encoding="utf-8")
```

(`_save_receipt` writes under `data_dir()/reports` — the existing smoke tests already redirect `data_dir` to `tmp_path` in `_wire`; keep that pattern.)

- [ ] **Step 6: Run everything, commit**

Run: `python -m pytest -q`; `python report.py`.

```bash
git add report.py gui.py tests/test_report.py tests/test_gui_smoke.py
git commit -m "feat: shop name + contact printed on receipts and condition reports"
```

---

### Task 6: Connect over Wi-Fi (wireless ADB)

Phones with broken USB data pins (very common in a repair shop — the charge-port test exists for exactly this) can still connect via Android 11+ Wireless debugging. One dialog: pairing address + code (first time) and connect address; after `adb connect` succeeds the existing 2-second device poll picks the phone up like any USB device (its serial is `ip:port`).

**Files:**
- Modify: `adb.py` (`wifi_connect`), `gui.py` (wizard button + dialog + worker)
- Test: `tests/test_adb.py`, `tests/test_gui_smoke.py`

**Interfaces:**
- Produces: `adb.wifi_connect(adb, connect_hostport, pair_hostport="", code="") -> (ok: bool, message: str)`. Pure control flow around `Adb.run(["pair", ...])` / `Adb.run(["connect", ...])`; judged on output text because `adb connect` exits 0 even on failure.
- GUI: `self._wifi_connect_bg(conn, pair, code, on_done)` (worker, testable without the dialog), `on_wifi_connect` (dialog).

- [ ] **Step 1: Write the failing tests** (append to `tests/test_adb.py`)

```python
from adb import wifi_connect


class WifiFake:
    def __init__(self, pair_out="Successfully paired to 192.168.1.9:41567 [guid=x]",
                 connect_out="connected to 192.168.1.9:37099"):
        self.calls = []
        self.pair_out, self.connect_out = pair_out, connect_out

    def run(self, args, timeout=10):
        self.calls.append(list(args))
        if args[0] == "pair":
            return self.pair_out
        if args[0] == "connect":
            return self.connect_out
        return ""


def test_wifi_connect_pairs_then_connects():
    fake = WifiFake()
    ok, msg = wifi_connect(fake, "192.168.1.9:37099", "192.168.1.9:41567", "123456")
    assert ok and "connected" in msg
    assert fake.calls[0] == ["pair", "192.168.1.9:41567", "123456"]
    assert fake.calls[1] == ["connect", "192.168.1.9:37099"]


def test_wifi_connect_skips_pairing_when_blank():
    fake = WifiFake()
    ok, _ = wifi_connect(fake, "192.168.1.9:37099")
    assert ok and fake.calls == [["connect", "192.168.1.9:37099"]]


def test_wifi_connect_reports_connect_failure():
    fake = WifiFake(connect_out="failed to connect to 192.168.1.9:37099")
    ok, msg = wifi_connect(fake, "192.168.1.9:37099")
    assert not ok and "failed" in msg


def test_wifi_connect_reports_pair_failure():
    fake = WifiFake(pair_out="Failed: Wrong password or connection was dropped")
    ok, msg = wifi_connect(fake, "192.168.1.9:37099", "192.168.1.9:41567", "000000")
    assert not ok and len(fake.calls) == 1   # never tries to connect


def test_wifi_connect_already_connected_is_ok():
    fake = WifiFake(connect_out="already connected to 192.168.1.9:37099")
    ok, _ = wifi_connect(fake, "192.168.1.9:37099")
    assert ok
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_adb.py -v`
Expected: FAIL with ImportError.

- [ ] **Step 3: Implement in `adb.py`** (after the `Adb` class, before `parse_devices`)

```python
def wifi_connect(adb, connect_hostport, pair_hostport="", code=""):
    """Pair (first time) then connect to a phone over Wi-Fi — Android 11+
    'Wireless debugging'. Returns (ok, message). `adb connect` exits 0 even
    when it fails, so success is judged from the output text, not the exit
    code. Once connected the phone shows up in `adb devices` with an
    ip:port serial and the normal poll takes over."""
    if pair_hostport and code:
        try:
            out = adb.run(["pair", pair_hostport, code], timeout=30)
        except AdbError as e:
            return False, str(e)
        if "paired" not in out.lower() or "failed" in out.lower():
            return False, out.strip() or "Pairing failed — check the code and address."
    try:
        out = adb.run(["connect", connect_hostport], timeout=30)
    except AdbError as e:
        return False, str(e)
    low = out.lower()
    ok = "connected to" in low and "failed" not in low and "cannot" not in low
    return ok, out.strip()
```

Add to `adb.py`'s `demo()`:

```python
    class _F:
        def run(self, args, timeout=10):
            return {"pair": "Successfully paired to h [guid]",
                    "connect": "connected to 1.2.3.4:5555"}[args[0]]
    ok, _ = wifi_connect(_F(), "1.2.3.4:5555", "1.2.3.4:4444", "123456")
    assert ok
```

Run: `python -m pytest tests/test_adb.py -v` and `python adb.py` — PASS.

- [ ] **Step 4: Wire the wizard**

`gui.py`:
1. Import: extend the `from adb import ...` line with `wifi_connect`.
2. `_build_wizard` — after the steps loop, before `self._set_wizard_state("searching")` (`gui.py:922`):

```python
        wifi_row = ttk.Frame(self.wizard, style="PanelFlat.TFrame")
        wifi_row.pack(fill="x", pady=(10, 0))
        ttk.Label(wifi_row, text="Broken charging port or no cable?",
                  style="PanelMuted.TLabel").pack(side="left", padx=(0, 8))
        self.wifi_btn = self._flat_button(wifi_row, "📶  Connect over Wi-Fi…",
                                          self.on_wifi_connect, SLATE, SLATE_HOT)
        self.wifi_btn.pack(side="left")
```

3. Worker + dialog (new section near `_poll_devices`):

```python
    def _wifi_connect_bg(self, conn, pair, code, on_done):
        """Run wifi_connect off the UI thread; on_done(ok, message) on the UI
        thread. Split from the dialog so tests can drive it headlessly."""
        def work():
            ok, msg = wifi_connect(self.adb, conn, pair, code)
            self._post(on_done, ok, msg)
        self._run_bg(work)

    def on_wifi_connect(self):
        if not self.adb:
            return
        win = tk.Toplevel(self.root)
        win.title("Connect over Wi-Fi")
        win.configure(bg=BASE)
        win.transient(self.root)
        win.grab_set()
        ttk.Label(win, justify="left", wraplength=470, text=(
            "The phone and this PC must be on the same Wi-Fi network.\n\n"
            "On the phone: Settings → Developer options → Wireless debugging → ON.\n"
            "1.  Tap “Pair device with pairing code” — type the code and the\n"
            "     pairing address it shows (first time only).\n"
            "2.  The main Wireless debugging screen shows the connect address\n"
            "     (IP address & Port).")).grid(row=0, column=0, columnspan=2,
                                               padx=14, pady=(12, 10), sticky="w")
        pair_v, code_v, conn_v = tk.StringVar(), tk.StringVar(), tk.StringVar()
        fields = (("Pairing address (IP:port)", pair_v),
                  ("Pairing code (6 digits)", code_v),
                  ("Connect address (IP:port)", conn_v))
        for r, (lbl, var) in enumerate(fields, start=1):
            ttk.Label(win, text=lbl).grid(row=r, column=0, sticky="e",
                                          padx=(14, 8), pady=4)
            ttk.Entry(win, textvariable=var, width=24).grid(
                row=r, column=1, sticky="w", padx=(0, 14), pady=4)
        status = ttk.Label(win, text="", wraplength=470, justify="left")
        status.grid(row=4, column=0, columnspan=2, padx=14, pady=(6, 0), sticky="w")

        def done(ok, msg):
            if not win.winfo_exists():
                return
            if ok:
                win.destroy()
                self.status_line("✅ Connected over Wi-Fi — the phone will appear "
                                 "in a moment.", "good")
            else:
                status.config(text="Couldn't connect: " + msg)

        def go():
            conn = conn_v.get().strip()
            if not conn:
                status.config(text="Enter the connect address — it looks like "
                                   "192.168.1.23:37099.")
                return
            status.config(text="Connecting…")
            self._wifi_connect_bg(conn, pair_v.get().strip(), code_v.get().strip(), done)

        self._flat_button(win, "📶  Connect", go, GREEN, GREEN_HOT).grid(
            row=5, column=0, columnspan=2, pady=(10, 14))
```

No changes to the poll: a connected Wi-Fi phone appears in `adb devices -l` as `192.168.1.9:37099 device product:... model:...` and `parse_devices` + `_pick_serial` already handle it.

- [ ] **Step 5: Smoke test** (append to `tests/test_gui_smoke.py`; the file's `FakeAdb` needs a `run` method — `def run(self, args, timeout=10): self.calls.append(list(args)); return "connected to 1.2.3.4:5555" if args[0] == "connect" else ""`)

```python
def test_wifi_connect_worker(root, monkeypatch, tmp_path):
    g = _connected_gui(root, monkeypatch, tmp_path)
    results = []
    g._wifi_connect_bg("1.2.3.4:5555", "", "", lambda ok, msg: results.append((ok, msg)))
    pump(root, 0.5)
    assert results and results[0][0] is True
    assert ["connect", "1.2.3.4:5555"] in g.adb.calls
```

- [ ] **Step 6: Run everything, commit**

Run: `python -m pytest -q`; `python adb.py`.

```bash
git add adb.py gui.py tests/test_adb.py tests/test_gui_smoke.py
git commit -m "feat: connect over Wi-Fi (wireless ADB) for phones with dead USB ports"
```

---

### Task 7: Release — README, version bump, exe, GitHub release

**Files:**
- Modify: `README.md`, `gui.py:40` (`APP_VERSION`)

- [ ] **Step 1: README updates**
  - Buttons table: add rows for **🗂 Find big files**, **🖐 Stop screen control**, **🏪 Shop details…** (History tab), and a **📶 Connect over Wi-Fi** line.
  - New short section "No cable? Connect over Wi-Fi" after "Connecting the phone": phone + PC on same Wi-Fi, Developer options → Wireless debugging, pair once with the code, then enter the connect address.
  - "History / Undo" section: mention uninstalled apps are restorable even if sideloaded (APKs are saved to `adcleaner_data/apk_backups`), and that deleted files are permanent.
  - Troubleshooting: "Phone charges but is never detected → the cable or the phone's USB port may be data-dead — use **📶 Connect over Wi-Fi**."

- [ ] **Step 2: Version bump**

`gui.py:40`: `APP_VERSION = "1.3.0"`.

- [ ] **Step 3: Full verification**

Run: `python -m pytest -q` (all green) and each changed module's demo: `python actions.py`, `python device.py`, `python scanner.py`, `python report.py`, `python adb.py`.

- [ ] **Step 4: Ship the docs PR** (same flow as every task), then rebuild + release:

```bash
# kill any running exe first (build fails if AdCleaner.exe is open)
taskkill /IM AdCleaner.exe /F 2>NUL
build.bat
# smoke: launch dist\AdCleaner.exe, confirm it opens and shows v1.3.0, close it
gh release create v2026.MM.DD "dist\AdCleaner.exe" --title "Ad Cleaner YYYY-MM-DD" --notes-file <notes>
```

---

## Self-Review Notes

- **Coverage:** all six requested features have a task (accessibility audit was found already implemented in the scanner; Task 4 delivers the missing user-facing control). Release/docs folded into Task 7.
- **Type consistency:** `wifi_connect` returns `(bool, str)` in adb.py, worker, and tests; `parse_owners` dict keys `device`/`profile` match `_show_owners` and the intake lambda; log-entry key `"apk"` (list) matches append kwarg, undo, and tests; `parse_big_files` returns `(path, mb)` tuples consumed by `_show_big_files(rows)`.
- **Known deliberate simplifications:** big-file `find` skips `Android/data` (invisible without root — noted in docstring); `delete_file` verifies via rm's exit code only; managed-phone detection displays but never scores (legit MDM exists); shop details have no logo image (text only).
- The `_connected_gui(...)` helper named in smoke tests is a stand-in for this file's existing connected-GUI setup pattern — implementers must reuse whatever helper/fixture the neighbouring tests use rather than inventing a new one.
