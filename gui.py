"""Tkinter UI (BUILD_PLAN 4.7).

All device work runs on background threads; results are marshalled back to the
Tk main thread through a queue (Tkinter is not thread-safe). The UI never
freezes and never crashes on an ADB failure -- errors land in the status bar.
"""

import queue
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from adb import Adb, AdbError, find_adb
from actions import (
    ActionLog, ProtectedAppError, can_undo, clean_risky, pause, resume, stop_all,
    undo, uninstall,
)
from scanner import build_inventory
from setup_helper import download_platform_tools

RISK_BG = {"HIGH": "#ffd6d6", "Medium": "#ffe9c7", "Low": "#ffffff"}
DOT = {"grey": "#9e9e9e", "orange": "#ff9800", "green": "#4caf50"}
COLUMNS = ("app", "package", "risk", "why", "installed", "source", "status")
HEADINGS = ("App name", "Package", "Risk", "Why flagged", "Installed", "Source", "Status")
SUSPICIOUS = {"HIGH", "Medium"}

STOP_ALL_MSG = ("This will close every downloaded app on the phone.\n\n"
                "Your photos, messages, and system apps are not affected.\n\nContinue?")

HELP_TEXT = """HOW TO CONNECT YOUR PHONE

1. On the phone, open Settings > About phone.
2. Tap "Build number" seven times until it says "You are now a developer".
3. Go back to Settings > Developer options and turn on "USB debugging".
4. Plug the phone into this computer with a USB cable.
5. On the phone, tap "Allow" when it asks about USB debugging
   (tick "Always allow from this computer").

The phone's model and a green light will appear at the top when it's connected.

IF POP-UP ADS ARE BLOCKING THE SCREEN

Restart the phone into Safe Mode first -- Safe Mode stops downloaded apps from
running, so the ads can't cover the screen while you work:

  * Hold the Power button.
  * Press and hold "Power off" on the screen.
  * Tap "Safe Mode" when it appears.

Then connect as above and use this program to Pause or Uninstall the bad apps.
Restart normally when you're done.

WHAT THE BUTTONS DO

  * STOP ALL APPS NOW - instantly closes every downloaded app.
  * Pause - freezes one app so it can't run (fully reversible with Resume).
  * Uninstall - removes an app for you (restore it later from the History tab).

TROUBLESHOOTING

  * "Unauthorized" - tap Allow on the phone screen.
  * Nothing detected - try a different USB cable (some cables only charge),
    a different USB port, and make sure USB debugging is on.
  * Samsung phones - installing "Samsung USB drivers" on this PC can help.
    Any brand - a "universal ADB driver" also works.
"""


# Where "Build number" and "USB debugging" live, by brand (BUILD_PLAN 4.1).
BRAND_STEPS = {
    "Other / not sure": "Open Settings and search for “Build number”. Tap it 7 times. "
                        "Then find “Developer options” and turn on “USB debugging”.",
    "Samsung": "Settings → About phone → Software information → tap “Build number” 7 times. "
               "Then Settings → Developer options → turn on “USB debugging”.",
    "Google Pixel": "Settings → About phone → tap “Build number” 7 times. "
                    "Then Settings → System → Developer options → “USB debugging”.",
    "Xiaomi / Redmi / POCO": "Settings → About phone → tap “MIUI version” 7 times. "
                             "Then Settings → Additional settings → Developer options → “USB debugging”.",
    "Oppo / Realme / OnePlus": "Settings → About device → tap “Version / Build number” 7 times. "
                               "Then Settings → Additional settings → Developer options → “USB debugging”.",
    "Motorola / Nokia / other": "Settings → About phone → tap “Build number” 7 times. "
                                "Then Settings → System → Developer options → “USB debugging”.",
}


class AdCleanerApp:
    def __init__(self, root):
        self.root = root
        root.title("Ad Cleaner")
        root.geometry("1000x700")
        root.minsize(820, 560)

        self.adb = None
        self.serial = None
        self.model = ""
        self.apps = []
        self.selected = None
        self.log = ActionLog()
        self.ui_queue = queue.Queue()
        self.alive = True
        self.busy = False
        self._pending_clean = False

        self._build_ui()
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._pump_queue()
        self._locate_adb()

    # --- cross-thread plumbing ---------------------------------------------

    def _post(self, fn, *args):
        """Called from worker threads; runs fn(*args) on the main thread."""
        self.ui_queue.put((fn, args))

    def _pump_queue(self):
        if not self.alive:
            return
        try:
            while True:
                fn, args = self.ui_queue.get_nowait()
                try:
                    fn(*args)
                except Exception:
                    pass
        except queue.Empty:
            pass
        self.root.after(50, self._pump_queue)

    def _run_bg(self, fn):
        threading.Thread(target=fn, daemon=True).start()

    # --- UI construction ----------------------------------------------------

    def _build_ui(self):
        top = ttk.Frame(self.root, padding=(10, 8))
        top.pack(fill="x")
        self.dot = tk.Canvas(top, width=16, height=16, highlightthickness=0)
        self.dot.grid(row=0, column=0, padx=(0, 6))
        self._draw_dot("grey")
        self.status_var = tk.StringVar(value="Starting…")
        ttk.Label(top, textvariable=self.status_var, font=("Segoe UI", 11, "bold")).grid(
            row=0, column=1, sticky="w")
        self.model_var = tk.StringVar(value="")
        ttk.Label(top, textvariable=self.model_var, foreground="#555").grid(
            row=0, column=2, sticky="w", padx=12)
        top.columnconfigure(3, weight=1)
        self.rescan_btn = ttk.Button(top, text="🔄 Rescan", command=self.on_rescan,
                                     state="disabled")
        self.rescan_btn.grid(row=0, column=4, padx=6)
        self.clean_btn = tk.Button(
            top, text="✨ CLEAN MY PHONE", command=self.on_clean,
            bg="#2e7d32", fg="white", activebackground="#1b5e20",
            activeforeground="white", font=("Segoe UI", 12, "bold"),
            relief="raised", padx=14, pady=7, state="disabled", cursor="hand2")
        self.clean_btn.grid(row=0, column=5, padx=(0, 8))
        self.stop_btn = tk.Button(
            top, text="⏹ STOP ALL", command=self.on_stop_all,
            bg="#d32f2f", fg="white", activebackground="#b71c1c",
            activeforeground="white", font=("Segoe UI", 11, "bold"),
            relief="raised", padx=12, pady=6, state="disabled", cursor="hand2")
        self.stop_btn.grid(row=0, column=6)

        self._build_wizard()

        nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True, padx=10, pady=(0, 4))
        self._build_apps_tab(nb)
        self._build_history_tab(nb)
        self._build_help_tab(nb)
        self.notebook = nb

        self.statusbar = ttk.Label(self.root, text="", relief="sunken", anchor="w",
                                   padding=(8, 3))
        self.statusbar.pack(fill="x", side="bottom")

    def _build_apps_tab(self, nb):
        tab = ttk.Frame(nb)
        nb.add(tab, text="Apps")

        bar = ttk.Frame(tab, padding=(8, 6))
        bar.pack(fill="x")
        ttk.Label(bar, text="Filter:").pack(side="left")
        self.filter_var = tk.StringVar()
        self.filter_var.trace_add("write", lambda *_: self._render_table())
        ttk.Entry(bar, textvariable=self.filter_var, width=28).pack(side="left", padx=6)
        self.suspicious_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="Show suspicious only", variable=self.suspicious_var,
                        command=self._render_table).pack(side="left", padx=10)
        self.progress = ttk.Progressbar(bar, mode="determinate", length=200)

        mid = ttk.Frame(tab)
        mid.pack(fill="both", expand=True, padx=8)
        self.tree = ttk.Treeview(mid, columns=COLUMNS, show="headings", selectmode="browse")
        widths = (200, 230, 70, 240, 90, 150, 80)
        for col, head, w in zip(COLUMNS, HEADINGS, widths):
            self.tree.heading(col, text=head)
            self.tree.column(col, width=w, anchor="w")
        for risk, bg in RISK_BG.items():
            self.tree.tag_configure(risk, background=bg)
        vsb = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self._on_select)

        detail = ttk.LabelFrame(tab, text="Details", padding=8)
        detail.pack(fill="x", padx=8, pady=6)
        self.detail_title = ttk.Label(detail, text="Select an app to see details.",
                                      font=("Segoe UI", 10, "bold"))
        self.detail_title.pack(anchor="w")
        self.detail_reasons = ttk.Label(detail, text="", justify="left", wraplength=940)
        self.detail_reasons.pack(anchor="w", pady=(2, 6))
        btns = ttk.Frame(detail)
        btns.pack(anchor="w")
        self.pause_btn = ttk.Button(btns, text="⏸ Pause", command=self.on_pause,
                                    state="disabled")
        self.resume_btn = ttk.Button(btns, text="▶ Resume", command=self.on_resume,
                                     state="disabled")
        self.uninstall_btn = ttk.Button(btns, text="🗑 Uninstall", command=self.on_uninstall,
                                        state="disabled")
        for b in (self.pause_btn, self.resume_btn, self.uninstall_btn):
            b.pack(side="left", padx=(0, 8))

    def _build_history_tab(self, nb):
        tab = ttk.Frame(nb)
        nb.add(tab, text="History / Undo")
        cols = ("time", "package", "action", "result")
        self.hist = ttk.Treeview(tab, columns=cols, show="headings", selectmode="browse")
        for c, w in zip(cols, (150, 300, 120, 90)):
            self.hist.heading(c, text=c.title())
            self.hist.column(c, width=w, anchor="w")
        self.hist.pack(fill="both", expand=True, padx=8, pady=8, side="left")
        vsb = ttk.Scrollbar(tab, orient="vertical", command=self.hist.yview)
        self.hist.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self.undo_btn = ttk.Button(tab, text="↩ Undo selected", command=self.on_undo)
        self.undo_btn.pack(side="bottom", pady=6)
        self._refresh_history()

    def _build_help_tab(self, nb):
        tab = ttk.Frame(nb)
        nb.add(tab, text="Help")
        txt = tk.Text(tab, wrap="word", padx=14, pady=12, font=("Segoe UI", 10),
                      relief="flat")
        txt.insert("1.0", HELP_TEXT)
        txt.configure(state="disabled")
        txt.pack(fill="both", expand=True, padx=8, pady=8)

    # --- connect wizard (shown until the phone is connected) ----------------

    def _build_wizard(self):
        self.wizard = ttk.Frame(self.root, padding=(16, 10))
        self.wizard.pack(fill="x")
        ttk.Label(self.wizard, text="Let's connect your phone",
                  font=("Segoe UI", 15, "bold")).pack(anchor="w")
        self.wiz_status = tk.StringVar(value="Looking for your phone…")
        ttk.Label(self.wizard, textvariable=self.wiz_status, font=("Segoe UI", 12),
                  foreground="#c62828").pack(anchor="w", pady=(2, 10))

        steps = [
            "Turn on “USB debugging” on the phone",
            "Plug the phone into this computer with a USB cable",
            "On the phone, tap “Allow” (tick “Always allow from this computer”)",
        ]
        self.step_icons = []
        for i, text in enumerate(steps):
            row = ttk.Frame(self.wizard)
            row.pack(fill="x", pady=2)
            icon = ttk.Label(row, text="⬜", font=("Segoe UI", 13), width=2)
            icon.pack(side="left", padx=(0, 8))
            self.step_icons.append(icon)
            ttk.Label(row, text=text, font=("Segoe UI", 11)).pack(side="left")
            if i == 0:
                brow = ttk.Frame(self.wizard)
                brow.pack(fill="x", padx=34, pady=(2, 0))
                ttk.Label(brow, text="Which phone?").pack(side="left", padx=(0, 6))
                self.brand_var = tk.StringVar(value="Other / not sure")
                ttk.OptionMenu(brow, self.brand_var, "Other / not sure",
                               *BRAND_STEPS, command=lambda *_: self._show_brand()).pack(
                    side="left")
                self.brand_help = ttk.Label(
                    self.wizard, text=BRAND_STEPS["Other / not sure"], wraplength=900,
                    justify="left", foreground="#555")
                self.brand_help.pack(anchor="w", padx=34, pady=(2, 8))
        self._set_wizard_state("searching")

    def _show_brand(self):
        self.brand_help.config(text=BRAND_STEPS[self.brand_var.get()])

    def _mark_steps(self, current, done=0):
        for i, icon in enumerate(self.step_icons):
            icon.config(text="✅" if i < done else ("👉" if i == current else "⬜"))

    def _set_wizard_state(self, state):
        if state == "connected":
            if self.wizard.winfo_manager():
                self.wizard.pack_forget()
            return
        if not self.wizard.winfo_manager():
            self.wizard.pack(fill="x", before=self.notebook)
        if state == "unauthorized":
            self.wiz_status.set("Almost there!  Now tap “Allow” on the phone screen.")
            self._mark_steps(current=2, done=2)
        else:  # searching
            self.wiz_status.set("Looking for your phone…  plug it in with a USB cable.")
            self._mark_steps(current=0, done=0)

    def _draw_dot(self, color):
        self.dot.delete("all")
        self.dot.create_oval(2, 2, 14, 14, fill=DOT[color], outline="")

    # --- connection ---------------------------------------------------------

    def _locate_adb(self):
        path = find_adb()
        if not path:
            # Only reached when running from source without ADB. The packaged exe
            # ships ADB inside it, so end users never see this.
            self._set_status("grey", "One-time setup…")
            self.status_line("Getting the phone tools ready (one-time, about 10 MB)…")
            self._download_adb()
            return
        self.adb = Adb(path)
        self._set_status("grey", "Looking for a phone…")
        self._run_bg(self._start_and_poll)

    def _download_adb(self):
        win = tk.Toplevel(self.root)
        win.title("Downloading ADB tools")
        win.geometry("360x110")
        win.transient(self.root)
        ttk.Label(win, text="Downloading Google Platform Tools…").pack(pady=(16, 6))
        bar = ttk.Progressbar(win, mode="determinate", length=300, maximum=1.0)
        bar.pack()

        def work():
            try:
                path = download_platform_tools(
                    progress=lambda f: self._post(bar.config, {"value": f}))
                self._post(self._after_download, win, path)
            except Exception as e:
                self._post(self._download_failed, win, str(e))

        self._run_bg(work)

    def _after_download(self, win, path):
        win.destroy()
        self.adb = Adb(path)
        self.status_line("ADB tools installed.")
        self._set_status("grey", "Looking for a phone…")
        self._run_bg(self._start_and_poll)

    def _download_failed(self, win, err):
        win.destroy()
        self._set_status("grey", "ADB download failed")
        messagebox.showerror(
            "Download failed",
            "Could not download the ADB tools.\n\n" + err +
            "\n\nYou can download 'platform-tools' manually from Google and put the "
            "platform-tools folder next to this program.")

    def _start_and_poll(self):
        try:
            self.adb.start_server()
        except Exception:
            pass
        self._post(self._poll_devices)

    def _poll_devices(self):
        if not self.alive or not self.adb:
            return
        self._run_bg(self._read_devices)

    def _read_devices(self):
        try:
            devices = self.adb.devices()
        except AdbError as e:
            self._post(self._on_devices, [], str(e))
            return
        self._post(self._on_devices, devices, None)

    def _on_devices(self, devices, err):
        if not self.alive:
            return
        ready = [d for d in devices if d["state"] == "device"]
        unauth = [d for d in devices if d["state"] == "unauthorized"]

        if not devices:
            self._disconnect("No phone detected — plug in your phone (see Help).", "grey")
            self._set_wizard_state("searching")
        elif unauth and not ready:
            self._disconnect("Phone found — tap 'Allow' on the phone to continue.", "orange")
            self._set_wizard_state("unauthorized")
        elif ready:
            serial = self._pick_serial(ready)
            if serial and serial != self.serial:
                self._connect(serial, next(d for d in ready if d["serial"] == serial))
        # keep polling
        self.root.after(2000, self._poll_devices)

    def _pick_serial(self, ready):
        if len(ready) == 1:
            return ready[0]["serial"]
        if self.serial in [d["serial"] for d in ready]:
            return self.serial
        return self._choose_device(ready)

    def _choose_device(self, ready):
        win = tk.Toplevel(self.root)
        win.title("Choose a phone")
        win.transient(self.root)
        win.grab_set()
        ttk.Label(win, text="More than one phone is connected. Pick one:").pack(
            padx=16, pady=(14, 8))
        chosen = {"serial": None}
        for d in ready:
            label = f"{d['model'] or 'Phone'}  ({d['serial']})"
            ttk.Button(win, text=label,
                       command=lambda s=d["serial"]: (chosen.update(serial=s), win.destroy())
                       ).pack(fill="x", padx=16, pady=3)
        win.wait_window()
        return chosen["serial"]

    def _connect(self, serial, dev):
        self.serial = serial
        self.adb = Adb(self.adb.adb_path, serial=serial)
        self._run_bg(lambda: self._read_device_info(dev))

    def _read_device_info(self, dev):
        model = dev.get("model") or ""
        android = ""
        try:
            model = self.adb.get_prop("ro.product.model") or model
            android = self.adb.get_prop("ro.build.version.release")
        except AdbError:
            pass
        self._post(self._on_connected, model, android)

    def _on_connected(self, model, android):
        self.model = model
        self._set_status("green", "Connected")
        self._set_wizard_state("connected")
        extra = f"Android {android}" if android else ""
        self.model_var.set(f"{model}   {extra}".strip())
        self.rescan_btn.config(state="normal")
        self.clean_btn.config(state="normal")
        self.stop_btn.config(state="normal")
        self.status_line("Phone connected. Scanning apps…")
        self.on_rescan()

    def _disconnect(self, message, color):
        was = self.serial
        self.serial = None
        if self.adb:
            self.adb = Adb(self.adb.adb_path)  # drop the -s binding
        self._set_status(color, message)
        self.model_var.set("")
        self.rescan_btn.config(state="disabled")
        self.clean_btn.config(state="disabled")
        self.stop_btn.config(state="disabled")
        if was:
            self.status_line("Phone disconnected.")
            self.apps = []
            self._render_table()
            self._clear_detail()

    # --- scanning -----------------------------------------------------------

    def on_rescan(self):
        if self.busy or not self.serial:
            return
        self.busy = True
        self.rescan_btn.config(state="disabled")
        self.progress.pack(side="right", padx=8)
        self.progress.config(value=0, maximum=100)
        self.status_line("Scanning…")
        self._run_bg(self._do_scan)

    def _do_scan(self):
        def progress(i, total, pkg):
            self._post(self._scan_progress, i, total)
        try:
            apps = build_inventory(self.adb, progress=progress)
        except Exception as e:
            self._post(self._scan_failed, str(e))
            return
        self._post(self._scan_done, apps)

    def _scan_progress(self, i, total):
        self.progress.config(maximum=max(total, 1), value=i)

    def _scan_done(self, apps):
        self.apps = apps
        self.busy = False
        self.progress.pack_forget()
        if self.serial:
            self.rescan_btn.config(state="normal")
        has_high = any(a.risk == "HIGH" for a in apps)
        self.suspicious_var.set(has_high)
        self._render_table()
        highs = sum(a.risk == "HIGH" for a in apps)
        self.status_line(f"Scan complete: {len(apps)} downloaded apps, {highs} high-risk.")
        if self._pending_clean:
            self._pending_clean = False
            self._start_clean()

    def _scan_failed(self, err):
        self.busy = False
        self.progress.pack_forget()
        if self.serial:
            self.rescan_btn.config(state="normal")
        self.status_line("Scan failed: " + err)

    # --- table + detail -----------------------------------------------------

    def _visible_apps(self):
        q = self.filter_var.get().strip().lower()
        out = []
        for a in self.apps:
            if self.suspicious_var.get() and a.risk not in SUSPICIOUS:
                continue
            if q and q not in a.label.lower() and q not in a.package.lower():
                continue
            out.append(a)
        return out

    def _render_table(self):
        self.tree.delete(*self.tree.get_children())
        for a in self._visible_apps():
            installed = a.first_install.strftime("%Y-%m-%d") if a.first_install else ""
            why = ", ".join(a.reasons)
            self.tree.insert("", "end", iid=a.package, tags=(a.risk,),
                             values=(a.label.split(" (")[0], a.package, a.risk, why,
                                     installed, a.source, a.status))

    def _app_by_pkg(self, pkg):
        return next((a for a in self.apps if a.package == pkg), None)

    def _on_select(self, _evt=None):
        sel = self.tree.selection()
        self.selected = self._app_by_pkg(sel[0]) if sel else None
        self._update_detail()

    def _clear_detail(self):
        self.selected = None
        self.detail_title.config(text="Select an app to see details.")
        self.detail_reasons.config(text="")
        for b in (self.pause_btn, self.resume_btn, self.uninstall_btn):
            b.config(state="disabled")

    def _update_detail(self):
        a = self.selected
        if not a:
            self._clear_detail()
            return
        self.detail_title.config(text=f"{a.label}  —  Risk: {a.risk}")
        if a.protected:
            self.detail_reasons.config(text="🔒 Protected system app — this one is kept safe "
                                            "and cannot be changed.")
            for b in (self.pause_btn, self.resume_btn, self.uninstall_btn):
                b.config(state="disabled")
            return
        reasons = "\n".join("• " + r for r in a.reasons) or "Nothing suspicious found."
        self.detail_reasons.config(text=reasons)
        self.pause_btn.config(state="normal" if a.enabled else "disabled")
        self.resume_btn.config(state="disabled" if a.enabled else "normal")
        self.uninstall_btn.config(state="normal")

    # --- actions ------------------------------------------------------------

    def _guarded(self):
        """Return the selected app if it's safe to act on, else None."""
        a = self.selected
        if not a or not self.serial or self.busy:
            return None
        if a.protected:
            messagebox.showinfo("Protected app", "That is a protected system app and "
                                                 "cannot be changed.")
            return None
        return a

    def on_pause(self):
        a = self._guarded()
        if not a:
            return
        if not messagebox.askyesno("Pause app", f"Freeze \"{a.label}\"?\n\n"
                                                 "It stops running until you press Resume."):
            return
        self._do_action(lambda: pause(self.adb, a, self.log), a, "Paused")

    def on_resume(self):
        a = self.selected
        if not a or not self.serial or self.busy:
            return
        self._do_action(lambda: resume(self.adb, a, self.log), a, "Resumed")

    def on_uninstall(self):
        a = self._guarded()
        if not a:
            return
        if not messagebox.askyesno(
                "Uninstall app",
                f"Remove \"{a.label}\" from the phone?\n\n"
                "It is removed for you but can be restored later from the History tab."):
            return
        self._do_action(lambda: uninstall(self.adb, a, self.log), a, "Uninstalled",
                        removes=True)

    def _do_action(self, fn, app, verb, removes=False):
        self.busy = True
        self.status_line(f"{verb[:-1]}ing {app.label.split(' (')[0]}…")

        def work():
            try:
                ok = fn()
                self._post(self._action_done, app, verb, ok, removes, None)
            except ProtectedAppError:
                self._post(self._action_done, app, verb, False, removes, "protected")
            except AdbError as e:
                self._post(self._action_done, app, verb, False, removes, str(e))
            except Exception as e:
                self._post(self._action_done, app, verb, False, removes, str(e))

        self._run_bg(work)

    def _action_done(self, app, verb, ok, removes, err):
        self.busy = False
        self._refresh_history()
        if err == "protected":
            self.status_line("Blocked: that app is protected.")
            return
        if err:
            self.status_line(f"Couldn't finish: {err}")
            self._update_detail()
            return
        if not ok:
            self.status_line(f"{verb}? The phone didn't confirm the change.")
        else:
            self.status_line(f"{verb}: {app.label.split(' (')[0]}.")
            if removes:
                self.apps = [a for a in self.apps if a.package != app.package]
                self.selected = None
        self._render_table()
        self._update_detail()

    # --- one-click clean ----------------------------------------------------

    def on_clean(self):
        if self.busy or not self.serial:
            return
        if not self.apps:                 # scan first, then clean automatically
            self._pending_clean = True
            self.on_rescan()
            return
        self._start_clean()

    def _start_clean(self):
        n = sum(a.risk == "HIGH" and not a.protected for a in self.apps)
        if not messagebox.askyesno(
                "Clean my phone",
                "Ad Cleaner will now:\n\n"
                "     •  Close every downloaded app\n"
                "     •  Block pop-up ads\n"
                f"     •  Pause the {n} app(s) most likely causing trouble\n\n"
                "Nothing is deleted — you can undo anything from the History tab.\n\n"
                "Go ahead?"):
            return
        self.busy = True
        self.status_line("Cleaning your phone…")

        def progress(i, total, pkg):
            self._post(self.status_line, f"Closing apps… {i} of {total}")

        def work():
            try:
                res = clean_risky(self.adb, self.apps, self.log, progress=progress)
                self._post(self._clean_done, res, None)
            except Exception as e:
                self._post(self._clean_done, None, str(e))

        self._run_bg(work)

    def _clean_done(self, res, err):
        self.busy = False
        self._refresh_history()
        self._render_table()
        self._update_detail()
        if err:
            self.status_line("Clean failed: " + err)
            return
        self.status_line(f"✅ Done! Closed {res['stopped']} app(s) and paused "
                         f"{res['paused']} risky app(s). Your phone should be usable now.")
        messagebox.showinfo(
            "All done",
            f"Closed {res['stopped']} downloaded app(s) and paused {res['paused']} "
            "risky app(s).\n\n"
            "Your photos, messages and system apps were not touched.\n\n"
            "Look through the list and press Uninstall on anything you don't want. "
            "You can undo anything from the History tab.")

    # --- STOP ALL -----------------------------------------------------------

    def on_stop_all(self):
        if self.busy or not self.serial:
            return
        proceed, block = self._confirm_stop_all()
        if not proceed:
            return
        self.busy = True
        self.status_line("Stopping all apps…")

        def progress(i, total, pkg):
            self._post(self.status_line, f"Stopped {i} of {total}…")

        def work():
            try:
                stopped, attempted = stop_all(self.adb, self.apps, self.log,
                                              block_popups=block, progress=progress)
                self._post(self._stop_done, stopped, attempted, None)
            except Exception as e:
                self._post(self._stop_done, 0, 0, str(e))

        self._run_bg(work)

    def _confirm_stop_all(self):
        win = tk.Toplevel(self.root)
        win.title("Stop all apps")
        win.transient(self.root)
        win.grab_set()
        ttk.Label(win, text=STOP_ALL_MSG, justify="left", padding=16).pack()
        block_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(win, text="Also block pop-ups instantly",
                        variable=block_var).pack(anchor="w", padx=16)
        result = {"go": False}
        row = ttk.Frame(win, padding=12)
        row.pack()
        ttk.Button(row, text="Cancel", command=win.destroy).pack(side="left", padx=6)
        ttk.Button(row, text="Yes, stop all",
                   command=lambda: (result.update(go=True), win.destroy())).pack(
            side="left", padx=6)
        win.wait_window()
        return result["go"], block_var.get()

    def _stop_done(self, stopped, attempted, err):
        self.busy = False
        self._refresh_history()
        self._render_table()
        self._update_detail()
        if err:
            self.status_line("Stop all failed: " + err)
        else:
            self.status_line(f"Stopped {stopped} of {attempted} downloaded apps. "
                             "System apps were left alone.")

    # --- history / undo -----------------------------------------------------

    def _refresh_history(self):
        self.hist.delete(*self.hist.get_children())
        for i, e in enumerate(self.log.recent()):
            self.hist.insert("", "end", iid=str(i),
                             values=(e["time"], e["package"], e["action"], e["result"]))

    def on_undo(self):
        sel = self.hist.selection()
        if not sel or self.busy:
            return
        entry = self.log.recent()[int(sel[0])]
        if not can_undo(entry):
            messagebox.showinfo("Undo", "This action can't be undone.")
            return
        if not self.serial:
            messagebox.showinfo("Undo", "Connect the phone first.")
            return
        self.busy = True
        self.status_line("Undoing…")

        def work():
            try:
                undo(self.adb, entry, self.log)
                self._post(self._undo_done, None)
            except Exception as e:
                self._post(self._undo_done, str(e))

        self._run_bg(work)

    def _undo_done(self, err):
        self.busy = False
        self._refresh_history()
        self.status_line("Undo failed: " + err if err else "Undo complete. Rescan to refresh.")

    # --- misc ---------------------------------------------------------------

    def _set_status(self, color, text):
        self._draw_dot(color)
        self.status_var.set(text)

    def status_line(self, text):
        self.statusbar.config(text=text)

    def _on_close(self):
        self.alive = False
        self.root.destroy()
