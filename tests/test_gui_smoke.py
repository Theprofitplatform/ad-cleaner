"""Headless GUI smoke/integration test.

Drives the real Tk widgets with a fake ADB so we exercise the connect wizard,
scan, protection-guard, and the one-click clean — all without a phone.
Skipped where Tk can't open a display.
"""

import base64
import time
from datetime import datetime

import pytest

TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M8AAAMBAQDJ/pLvAAAAAElFTkSuQmCC")

tkinter = pytest.importorskip("tkinter")
import gui
from actions import ActionLog
from scanner import App, score_app

NOW = datetime(2024, 6, 1)


class FakeAdb:
    devices_list = [{"serial": "S1", "state": "device", "model": "SM_TEST"}]

    def __init__(self, path, serial=None):
        self.adb_path = path
        self.serial = serial
        self.disabled = set()
        self.installed = {"com.random.adware", "com.google.android.gms"}
        self.calls = []
        self.rebooted = False
        self.png = TINY_PNG

    def start_server(self):
        pass

    def devices(self):
        return list(self.devices_list)

    def get_prop(self, prop, timeout=10):
        return {"ro.product.model": "SM Test", "ro.build.version.release": "13"}.get(prop, "")

    def pull(self, remote, local, timeout=120):
        from pathlib import Path
        Path(local).write_text("apk"); return "pulled"

    def reboot(self, timeout=10):
        self.rebooted = True; return ""

    def screencap(self, timeout=20):
        return self.png  # a valid tiny PNG

    def shell_text(self, args, timeout=10):
        self.calls.append(args)
        if args[:3] == ["pm", "disable-user", "--user"]:
            self.disabled.add(args[-1]); return ""
        if args[:3] == ["pm", "uninstall", "--user"]:
            self.installed.discard(args[-1]); return "Success"
        if args[:3] == ["pm", "clear", "--user"]:
            return "Success"
        if args[:2] == ["pm", "path"]:
            return "package:/data/app/x/base.apk\n"
        if args[:4] == ["settings", "get", "secure", "enabled_accessibility_services"]:
            return ""
        if args[:2] in (["am", "force-stop"], ["appops", "set"], ["settings", "put"]):
            return ""
        if args == ["pm", "list", "packages", "-d"]:
            return "".join(f"package:{p}\n" for p in self.disabled)
        if args == ["pm", "list", "packages"]:
            return "".join(f"package:{p}\n" for p in self.installed)
        return ""


class NoDeviceAdb(FakeAdb):
    devices_list = []


def make_apps():
    adware = App(package="com.random.adware", installer=None, overlay=True,
                 first_install=datetime(2024, 5, 20))
    protected = App(package="com.google.android.gms", installer="com.android.vending",
                    first_install=datetime(2020, 1, 1))
    for a in (adware, protected):
        score_app(a, NOW)
    return [adware, protected]


@pytest.fixture
def root():
    r = None
    for _ in range(3):  # multiple Tk() per process can transiently fail on Windows
        try:
            r = tkinter.Tk()
            break
        except tkinter.TclError:
            time.sleep(0.2)
    if r is None:
        pytest.skip("no display available")
    r.withdraw()
    yield r
    try:
        r.destroy()
    except tkinter.TclError:
        pass


def pump(root, seconds=1.0):
    end = time.time() + seconds
    while time.time() < end:
        root.update()
        root.update_idletasks()
        time.sleep(0.02)


def _wire(gui_mod, monkeypatch, tmp_path, adb_cls=FakeAdb):
    monkeypatch.setattr(gui_mod, "ActionLog", lambda: ActionLog(tmp_path / "log.json"))
    monkeypatch.setattr(gui_mod, "find_adb", lambda: "fakeadb")
    monkeypatch.setattr(gui_mod, "Adb", adb_cls)
    monkeypatch.setattr(gui_mod, "build_inventory",
                        lambda adb, progress=None, now=None: make_apps())
    monkeypatch.setattr(gui_mod.messagebox, "askyesno", lambda *a, **k: True)
    monkeypatch.setattr(gui_mod.messagebox, "showinfo", lambda *a, **k: None)


def test_opens_and_shows_wizard_without_phone(root, monkeypatch, tmp_path):
    """Acceptance #1: no phone -> app opens, shows the connect wizard, no crash."""
    _wire(gui, monkeypatch, tmp_path, adb_cls=NoDeviceAdb)
    app = gui.AdCleanerApp(root)
    pump(root, 0.6)
    assert app.wizard.winfo_manager()           # wizard is visible
    assert str(app.clean_btn["state"]) == "disabled"
    assert app.alive


def test_connect_scan_and_protection(root, monkeypatch, tmp_path):
    """Acceptance #2 + #7: connect, wizard hides, table fills, protected app locked."""
    _wire(gui, monkeypatch, tmp_path)
    app = gui.AdCleanerApp(root)
    pump(root, 1.5)

    assert app.status_var.get() == "Connected"
    assert "SM Test" in app.model_var.get()
    assert not app.wizard.winfo_manager()        # wizard hidden once connected
    assert str(app.clean_btn["state"]) == "normal"

    app.suspicious_var.set(False)
    app._render_table()
    pump(root, 0.1)
    assert set(app.tree.get_children()) >= {"com.random.adware", "com.google.android.gms"}

    app.tree.selection_set("com.google.android.gms")
    app._on_select()
    pump(root, 0.1)
    assert str(app.pause_btn["state"]) == "disabled"
    assert str(app.uninstall_btn["state"]) == "disabled"

    app.tree.selection_set("com.random.adware")
    app._on_select()
    pump(root, 0.1)
    assert str(app.pause_btn["state"]) == "normal"


def test_one_click_clean(root, monkeypatch, tmp_path):
    """The big green button pauses the risky app and never touches the protected one."""
    _wire(gui, monkeypatch, tmp_path)
    app = gui.AdCleanerApp(root)
    pump(root, 1.5)
    assert str(app.clean_btn["state"]) == "normal"

    app.on_clean()
    pump(root, 1.0)
    assert "com.random.adware" in app.adb.disabled       # risky app paused
    assert "com.google.android.gms" not in app.adb.disabled  # protected untouched


def test_device_tab_buttons_enable_on_connect(root, monkeypatch, tmp_path):
    _wire(gui, monkeypatch, tmp_path)
    app = gui.AdCleanerApp(root)
    pump(root, 1.5)
    assert str(app.cache_btn["state"]) == "normal"
    assert str(app.dev_refresh_btn["state"]) == "normal"


def test_uninstall_mode_removes_apps(root, monkeypatch, tmp_path):
    """With the uninstall toggle on, clean removes risky apps and drops them."""
    _wire(gui, monkeypatch, tmp_path)
    app = gui.AdCleanerApp(root)
    pump(root, 1.5)
    app.uninstall_mode.set(True)
    app.on_clean()
    pump(root, 1.0)
    assert "com.random.adware" not in app.adb.installed        # uninstalled
    assert "com.google.android.gms" in app.adb.installed       # protected kept
    assert app._app_by_pkg("com.random.adware") is None         # dropped from list


def test_bulk_uninstall_multi_select(root, monkeypatch, tmp_path):
    _wire(gui, monkeypatch, tmp_path)
    app = gui.AdCleanerApp(root)
    pump(root, 1.5)
    a1 = App(package="com.junk.one", installer=None, risk="HIGH")
    a2 = App(package="com.junk.two", installer=None, risk="HIGH")
    app.apps = [a1, a2]
    app.suspicious_var.set(False)
    app._render_table()
    app.adb.installed.update({"com.junk.one", "com.junk.two"})
    app.tree.selection_set("com.junk.one", "com.junk.two")
    app._on_select()
    pump(root, 0.1)
    app.on_uninstall()          # askyesno patched to Yes -> bulk removes both
    pump(root, 1.0)
    assert "com.junk.one" not in app.adb.installed
    assert "com.junk.two" not in app.adb.installed
    assert app._app_by_pkg("com.junk.one") is None


def test_reset_data_from_detail(root, monkeypatch, tmp_path):
    _wire(gui, monkeypatch, tmp_path)
    app = gui.AdCleanerApp(root)
    pump(root, 1.5)
    app.suspicious_var.set(False)
    app._render_table()
    app.tree.selection_set("com.random.adware")
    app._on_select()
    pump(root, 0.1)
    app.on_reset_data()
    pump(root, 0.6)
    assert ["pm", "clear", "--user", "0", "com.random.adware"] in app.adb.calls


def test_screenshot_and_reboot(root, monkeypatch, tmp_path):
    _wire(gui, monkeypatch, tmp_path)
    app = gui.AdCleanerApp(root)
    pump(root, 1.5)
    app.on_screenshot()         # captures TINY_PNG, opens a viewer, saves a file
    pump(root, 0.6)
    app.on_reboot()             # askyesno patched to Yes
    pump(root, 0.3)
    assert app.adb.rebooted


def test_shop_mode_auto_cleans_on_scan(root, monkeypatch, tmp_path):
    """Shop mode: a scan auto-triggers the clean (confirmed once), no CLEAN click."""
    _wire(gui, monkeypatch, tmp_path)
    app = gui.AdCleanerApp(root)
    pump(root, 1.5)                                 # connects + scans, shop off -> no clean
    assert "com.random.adware" not in app.adb.disabled
    app.shop_mode.set(True)
    app.on_rescan()                                 # scan -> shop mode auto-cleans
    pump(root, 1.0)
    assert "com.random.adware" in app.adb.disabled
    assert "com.google.android.gms" not in app.adb.disabled
