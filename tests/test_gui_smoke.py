"""Headless GUI smoke/integration test.

Drives the real Tk widgets with a fake ADB so we exercise the connect wizard,
scan, protection-guard, and the one-click clean — all without a phone.
Skipped where Tk can't open a display.
"""

import base64
import time
from datetime import datetime
from pathlib import Path

import pytest

TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M8AAAMBAQDJ/pLvAAAAAElFTkSuQmCC")

tkinter = pytest.importorskip("tkinter")
import gui
from actions import ActionLog
from adb import AdbError
from scanner import App, score_app

NOW = datetime(2024, 6, 1)
FIXTURES = Path(__file__).parent / "fixtures"


def fx(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


class FakeAdb:
    devices_list = [{"serial": "S1", "state": "device", "model": "SM_TEST"}]

    def __init__(self, path, serial=None):
        self.adb_path = path
        self.serial = serial
        self.disabled = set()
        self.installed = {"com.random.adware", "com.google.android.gms"}
        self.calls = []
        self.commands = []
        self.rebooted = False
        self.png = TINY_PNG
        self.globals = {}

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
        self.commands.append(" ".join(args))
        if args[:2] == ["pm", "revoke"]:
            return ""
        if args[:2] == ["appops", "set"] and "POST_NOTIFICATION" in args:
            return ""
        if args[:3] == ["pm", "disable-user", "--user"]:
            self.disabled.add(args[-1]); return ""
        if args[:3] == ["pm", "uninstall", "--user"]:
            self.installed.discard(args[-1]); return "Success"
        if args[:3] == ["pm", "clear", "--user"]:
            return "Success"
        if args[:2] == ["pm", "path"]:
            return f"package:/data/app/{args[-1]}/base.apk" if args[-1] in self.installed else ""
        if args[:3] == ["cmd", "role", "add-role-holder"]:
            self.role_holder = args[-1]; return ""
        if args[:3] == ["cmd", "role", "get-role-holders"]:
            return getattr(self, "role_holder", "")
        if args[:4] == ["settings", "get", "secure", "enabled_accessibility_services"]:
            return ""
        if args[:3] == ["settings", "get", "global"]:
            return self.globals.get(args[3], "null")
        if args[:3] == ["settings", "put", "global"]:
            self.globals[args[3]] = args[4]; return ""
        if args[:2] in (["am", "force-stop"], ["appops", "set"], ["settings", "put"]):
            return ""
        if args == ["pm", "list", "packages", "-d"]:
            return "".join(f"package:{p}\n" for p in self.disabled)
        if args == ["pm", "list", "packages", "-s"]:
            return "package:com.facebook.appmanager\n"
        if args == ["pm", "list", "packages"]:
            return "".join(f"package:{p}\n" for p in self.installed)
        if args == ["dumpsys", "batterystats", "--charged"]:
            return fx("batterystats.txt")
        if args == ["pm", "list", "packages", "-U", "-3"]:
            return fx("packages_uids.txt")
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
    monkeypatch.setattr(gui_mod.webbrowser, "open", lambda *a, **k: None)
    # keep tests hermetic: no Google Play lookups, no APK pulls for icons
    monkeypatch.setattr(gui_mod.playstore, "lookup", lambda pkg, **k: None)
    monkeypatch.setattr(gui_mod.playstore, "fetch_icon", lambda url, **k: None)
    monkeypatch.setattr(gui_mod.appicon, "device_icon", lambda adb, pkg: None)


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


def test_bulk_confirm_calls_out_safe_apps(root, monkeypatch, tmp_path):
    # Select all with the risky filter off ticks EVERYTHING; the confirm must
    # say how many of the selection look safe so a one-click wipe is visible.
    _wire(gui, monkeypatch, tmp_path)
    seen = {}
    monkeypatch.setattr(gui.messagebox, "askyesno",
                        lambda title, msg, **k: seen.update(msg=msg) or False)
    app = gui.AdCleanerApp(root)
    pump(root, 1.5)
    app.apps = [App(package="com.junk.one", installer=None, risk="HIGH"),
                App(package="com.whatsapp", installer="com.android.vending", risk="Low")]
    app.suspicious_var.set(False)
    app._render_table()
    app.on_select_all()
    pump(root, 0.1)
    app.on_uninstall()          # askyesno returns False -> nothing is removed
    assert "1 of these look SAFE" in seen["msg"]
    assert app._app_by_pkg("com.whatsapp") is not None


def test_select_all_then_bulk_pause(root, monkeypatch, tmp_path):
    _wire(gui, monkeypatch, tmp_path)
    app = gui.AdCleanerApp(root)
    pump(root, 1.5)
    # bulk-action buttons come alive once a phone is connected
    assert str(app.selectall_btn["state"]) == "normal"
    assert str(app.bulk_pause_btn["state"]) == "normal"
    assert str(app.bulk_uninstall_btn["state"]) == "normal"
    a1 = App(package="com.junk.one", installer=None, risk="HIGH")
    a2 = App(package="com.junk.two", installer=None, risk="HIGH")
    app.apps = [a1, a2]
    app.suspicious_var.set(False)
    app._render_table()
    app.on_select_all()                       # ticks every visible row
    pump(root, 0.1)
    assert set(app.tree.selection()) == {"com.junk.one", "com.junk.two"}
    app.on_pause()                            # bulk pause the whole selection
    pump(root, 1.0)
    assert "com.junk.one" in app.adb.disabled
    assert "com.junk.two" in app.adb.disabled


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


def test_restrict_data_from_detail(root, monkeypatch, tmp_path):
    _wire(gui, monkeypatch, tmp_path)
    app = gui.AdCleanerApp(root)
    pump(root, 1.5)
    a = App(package="com.random.freegift", installer=None, risk="HIGH",
            uid=10231, data_mb=81)
    app.apps = [a]; app._render_table()
    app.tree.selection_set("com.random.freegift"); app._on_select()
    pump(root, 0.1)
    assert "Data used: 81 MB" in app.detail_reasons["text"]
    app.on_restrict_data()
    pump(root, 0.6)
    assert "cmd netpolicy add restrict-background-blacklist 10231" in app.adb.commands


def test_data_btn_disabled_for_non_app_uid(root, monkeypatch, tmp_path):
    _wire(gui, monkeypatch, tmp_path)
    app = gui.AdCleanerApp(root)
    pump(root, 1.5)
    bad = App(package="com.unknown.pkg", installer=None, risk="HIGH", uid=0)
    app.apps = [bad]; app._render_table()
    app.tree.selection_set("com.unknown.pkg"); app._on_select()
    pump(root, 0.1)
    assert str(app.data_btn["state"]) == "disabled"

    good = App(package="com.random.freegift", installer=None, risk="HIGH", uid=10231)
    app.apps = [good]; app._render_table()
    app.tree.selection_set("com.random.freegift"); app._on_select()
    pump(root, 0.1)
    assert str(app.data_btn["state"]) != "disabled"


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


def test_fix_roles_button_clears_busy_on_adb_error(root, monkeypatch, tmp_path):
    _wire(gui, monkeypatch, tmp_path)
    app = gui.AdCleanerApp(root)
    pump(root, 1.5)

    def raise_adb_error(*a, **k):
        raise AdbError("boom")

    monkeypatch.setattr(gui, "fix_role", raise_adb_error)
    a = App(package="com.random.freegift", installer=None, risk="HIGH",
            hijacked_roles=["browser"])
    app.apps = [a]; app._render_table()
    app.tree.selection_set("com.random.freegift"); app._on_select()
    app.on_fix_roles()
    pump(root, 1.0)
    assert app.busy is False


def test_stalkerware_caution_shown_in_detail(root, monkeypatch, tmp_path):
    _wire(gui, monkeypatch, tmp_path)
    app = gui.AdCleanerApp(root)
    pump(root, 1.5)
    a = App(package="com.thetruthspy", installer=None, first_install=NOW)
    score_app(a, NOW)
    app.apps = [a]; app._render_table()
    app.tree.selection_set("com.thetruthspy"); app._on_select()
    pump(root, 0.1)
    assert "hidden tracking app" in app.detail_reasons["text"]


def test_debloat_disables_preinstalled_junk(root, monkeypatch, tmp_path):
    _wire(gui, monkeypatch, tmp_path)
    app = gui.AdCleanerApp(root)
    pump(root, 1.5)
    app.on_debloat()
    pump(root, 1.0)
    assert "com.facebook.appmanager" in app.adb.disabled


def test_chrome_popup_quickfix_blocks_notifications(root, monkeypatch, tmp_path):
    _wire(gui, monkeypatch, tmp_path)
    app = gui.AdCleanerApp(root)
    pump(root, 1.5)
    app.on_chrome_popups()
    pump(root, 1.0)
    assert any("com.android.chrome" in c and
               ("revoke" in c or "POST_NOTIFICATION" in c)
               for c in app.adb.commands)


def test_play_check_updates_table_and_detail(root, monkeypatch, tmp_path):
    """Play results trickle in post-scan: unlisted apps get flagged (once),
    listed ones show Google's official name in the detail pane."""
    _wire(gui, monkeypatch, tmp_path)
    app = gui.AdCleanerApp(root)
    pump(root, 1.5)

    adware = next(a for a in app.apps if a.package == "com.random.adware")
    app._apply_play(adware, {"listed": False, "name": None, "icon": None})
    app._apply_play(adware, {"listed": False, "name": None, "icon": None})
    # The Play verdict must NOT be appended to reasons -- that list feeds
    # will_clean's unattended nuisance fence, and reasons is read by the
    # clean worker thread while _apply_play runs on the UI thread.
    assert gui.playstore.NOT_LISTED_REASON not in adware.reasons
    app.tree.selection_set("com.random.adware"); app._on_select()
    pump(root, 0.2)
    assert gui.playstore.NOT_LISTED_REASON in app.detail_reasons["text"]

    fake = App(package="com.fake.chrome", installer=None, first_install=NOW)
    score_app(fake, NOW)
    app.apps.append(fake)
    app.suspicious_var.set(False); app._render_table()
    app._apply_play(fake, {"listed": True, "name": "Google Chrome", "icon": None})
    app.tree.selection_set("com.fake.chrome"); app._on_select()
    pump(root, 0.2)
    assert "Google Chrome" in app.detail_reasons["text"]

    # icon plumbing: a valid PNG lands in (and clears from) the detail pane
    p = tmp_path / "icon.png"
    img = tkinter.PhotoImage(master=root, width=4, height=4)
    img.put("#ff0000", to=(0, 0, 4, 4))
    img.write(str(p), format="png")
    app._set_detail_icon(p)
    assert str(app.detail_icon["image"])
    app._clear_detail()
    assert not str(app.detail_icon["image"])
    app._set_detail_icon(tmp_path / "corrupt.png")   # missing/bad file -> no crash
    assert not str(app.detail_icon["image"])


def test_device_tab_shows_top_battery_drainer(root, monkeypatch, tmp_path):
    _wire(gui, monkeypatch, tmp_path)
    app = gui.AdCleanerApp(root)
    pump(root, 1.5)
    assert "mAh" in app.dev_vars["top_drainer"].get()


def test_battery_health_zero_shows_no_data(root, monkeypatch, tmp_path):
    """When battery health is 0 (uncalibrated), show no-data indicator, not '0%'."""
    _wire(gui, monkeypatch, tmp_path)
    app = gui.AdCleanerApp(root)
    pump(root, 1.5)
    app._show_battery_report({"top_drainers": [], "health_pct": 0})
    assert app.dev_vars["battery_health"].get() == "—"


def test_disconnect_clears_stale_battery_report(root, monkeypatch, tmp_path):
    """A stale battery_report from phone A must not survive into phone B's
    session -- otherwise phone B's receipt can print phone A's battery health."""
    _wire(gui, monkeypatch, tmp_path)
    app = gui.AdCleanerApp(root)
    pump(root, 1.5)
    app.battery_report = {"top_drainers": [], "health_pct": 77}
    app._disconnect("No phone connected", "grey")
    assert app.battery_report is None


def test_receipt_most_used_strips_package_suffix_from_label(root, monkeypatch, tmp_path):
    """Receipt most_used line strips the package suffix from prettified labels."""
    from scanner import prettify_label
    _wire(gui, monkeypatch, tmp_path)
    app = gui.AdCleanerApp(root)
    pump(root, 1.5)
    # Create an app with a prettified label like "Flashlight (com.foo.flashlight)"
    prettified_label = prettify_label("com.foo.flashlight")
    a = App(package="com.foo.flashlight", label=prettified_label, used_min=62)
    app.apps = [a]
    receipt_path = app._save_receipt({})  # empty result dict, we only care about most_used
    assert receipt_path, "receipt should be written"
    text = receipt_path.read_text(encoding="utf-8")
    # Should contain "Flashlight (62 min)", NOT "Flashlight (com.foo.flashlight) (62 min)"
    assert "Flashlight (62 min)" in text, f"expected 'Flashlight (62 min)' in receipt, got:\n{text}"
    assert "flashlight) (62 min)" not in text, f"should not have doubled parens, got:\n{text}"
