"""Tkinter UI (BUILD_PLAN 4.7).

All device work runs on background threads; results are marshalled back to the
Tk main thread through a queue (Tkinter is not thread-safe). The UI never
freezes and never crashes on an ADB failure -- errors land in the status bar.
"""

import json
import queue
import threading
import tkinter as tk
import webbrowser
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from adb import Adb, AdbError, data_dir, find_adb
from actions import (
    ActionLog, DNS_PROVIDERS, ProtectedAppError, backup_apk, block_notifications, can_undo,
    clean_risky, clear_caches, clear_private_dns, debloat, fix_role, pause, read_private_dns,
    reboot, reset_app_data, restrict_background, resume, set_private_dns, stop_all, undo,
    uninstall, will_clean,
)
from bloatware import find_bloat
from crashes import read_crash_report, summarize
from device import read_battery_report, read_device_stats
from report import render_history_html, render_receipt_html
from scanner import ROLE_IDS, STALKER_REASON, build_inventory
from setup_helper import download_platform_tools

import appicon
import playstore

# --- palette ---------------------------------------------------------------
FONT = "Segoe UI"
BASE = "#ffffff"       # window / table background
INK = "#111827"        # primary text
MUTED = "#6b7280"      # secondary text
PANEL = "#f8fafc"      # card background (wizard / details)
HEADER_BG = "#0f172a"  # dark header band
HEADER_INK = "#f8fafc"
HEADER_MUTED = "#94a3b8"
GREEN, GREEN_HOT = "#16a34a", "#15803d"
RED, RED_HOT = "#dc2626", "#b91c1c"
SLATE, SLATE_HOT = "#334155", "#475569"
AMBER, AMBER_HOT = "#d97706", "#b45309"
BTN_OFF = "#cbd5e1"    # disabled button background
RISK_BG = {"HIGH": "#fee2e2", "Medium": "#fef3c7", "Low": "#ffffff"}
RISK_FG = {"HIGH": "#991b1b", "Medium": "#92400e", "Low": INK}
RISK_DOT = {"HIGH": "🔴", "Medium": "🟠", "Low": "🟢"}  # colour-independent risk cue
DOT = {"grey": "#94a3b8", "orange": "#f59e0b", "green": "#22c55e"}
# Verdict-banner tints: kind -> (background, foreground).
BANNER = {"info": (PANEL, SLATE), "warn": ("#fef3c7", "#92400e"),
          "alert": ("#fee2e2", "#991b1b"), "good": ("#dcfce7", "#166534")}
STATUS_FG = {"info": INK, "good": GREEN_HOT, "error": RED}
COLUMNS = ("app", "package", "risk", "why", "installed", "source", "status")
HEADINGS = ("App name", "App ID", "Risk", "Why flagged", "Installed", "Source", "Status")
# Show plain-English columns first (name, verdict, reason); techie ones trail.
DISPLAY = ("app", "risk", "why", "status", "installed", "source", "package")
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


# --- old-phone -> new-phone file transfer ----------------------------------
# The user's own folders live under /sdcard/ (== /storage/emulated/0/). We only
# ever copy files — never contacts/messages/apps, which Android blocks over ADB
# (that's what the phone's built-in "Copy apps & data" wizard is for).
REMOTE_BASE = "/sdcard/"
TRANSFER_FOLDERS = ("DCIM", "Pictures", "Movies", "Music", "Download", "Documents")
# ponytail: one big timeout, no per-file progress. A phone full of photos can
# take minutes; adb buffers its own progress so we only report totals at the end.
# If users want a live bar, stream `adb pull` stderr instead of capturing it.
TRANSFER_TIMEOUT = 3600
# Written into a save folder only after every folder pulled cleanly; its absence
# means the save was interrupted (crash, cable, timeout) and may be partial.
TRANSFER_DONE_MARK = ".complete"


def _pull_media(adb, dest):
    """Copy the standard user folders off the phone into `dest`.

    Returns (saved, skipped, failed) folder-name lists. A folder that isn't on
    this phone is skipped — not an error. Any OTHER failure (cable pulled,
    timeout, disk full) lands in `failed`; a failed folder may be PARTIALLY
    copied on disk, so callers must never report success while `failed` is
    non-empty — a tech may wipe the old phone on that signal.
    """
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    saved, skipped, failed = [], [], []
    for name in TRANSFER_FOLDERS:
        try:
            adb.pull(REMOTE_BASE + name, str(dest), timeout=TRANSFER_TIMEOUT)
            saved.append(name)
        except AdbError as e:
            # adb's message for a missing remote folder is
            # "remote object '/sdcard/X' does not exist" — anything else is real.
            (skipped if "does not exist" in str(e).lower() else failed).append(name)
    return saved, skipped, failed


def _push_media(adb, src):
    """Copy each subfolder of a saved transfer folder back onto the phone."""
    src = Path(src)
    pushed, failed = [], []
    for child in sorted(src.iterdir()):
        if not child.is_dir():
            continue  # ignore stray files; we only restore folders we saved
        try:
            adb.push(str(child), REMOTE_BASE, timeout=TRANSFER_TIMEOUT)
            pushed.append(child.name)
        except AdbError:
            failed.append(child.name)
    return pushed, failed


class AdCleanerApp:
    def __init__(self, root):
        self.root = root
        root.title("Ad Cleaner")
        root.geometry("1080x740")
        root.minsize(900, 600)

        self.adb = None
        self.serial = None
        self.model = ""
        self.android = ""
        self.apps = []
        self.selected = None
        self._last_transfer_dir = None  # set after a Step-1 save this session
        self.log = ActionLog()
        self.ui_queue = queue.Queue()
        self.alive = True
        self.busy = False
        self._pending_clean = False
        self.battery_report = None
        self._settings = self._load_settings()
        self.shop_mode = tk.BooleanVar(value=self._settings.get("shop_mode", False))
        self.uninstall_mode = tk.BooleanVar(
            value=self._settings.get("uninstall_mode", False))  # False=pause, True=remove

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

    # --- remembered settings (Shop mode / Uninstall mode / phone brand) -----

    def _load_settings(self):
        try:
            return json.loads((data_dir() / "settings.json").read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_settings(self):
        try:
            (data_dir() / "settings.json").write_text(json.dumps({
                "shop_mode": self.shop_mode.get(),
                "uninstall_mode": self.uninstall_mode.get(),
                "brand": self.brand_var.get(),
            }, indent=2), encoding="utf-8")
        except Exception:
            pass

    # --- theme + buttons ----------------------------------------------------

    def _apply_theme(self):
        self._btn_palette = {}
        self.root.configure(bg=BASE)
        st = ttk.Style()
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        st.configure(".", font=(FONT, 10), background=BASE, foreground=INK)
        st.configure("TFrame", background=BASE)
        st.configure("TLabel", background=BASE, foreground=INK)
        st.configure("Muted.TLabel", background=BASE, foreground=MUTED)
        # Status bar carries every outcome + error, so keep it dark and legible.
        st.configure("Status.TLabel", background=PANEL, foreground=INK, font=(FONT, 11))
        st.configure("Header.TFrame", background=HEADER_BG)
        st.configure("Header.TLabel", background=HEADER_BG, foreground=HEADER_INK,
                     font=(FONT, 11, "bold"))
        st.configure("HeaderMuted.TLabel", background=HEADER_BG, foreground=HEADER_MUTED,
                     font=(FONT, 10))
        st.configure("Panel.TFrame", background=PANEL, borderwidth=1, relief="solid")
        st.configure("PanelFlat.TFrame", background=PANEL, borderwidth=0)
        st.configure("Panel.TLabel", background=PANEL, foreground=INK)
        st.configure("PanelMuted.TLabel", background=PANEL, foreground=MUTED)
        st.configure("PanelWarn.TLabel", background=PANEL, foreground=RED)
        st.configure("PanelInfo.TLabel", background=PANEL, foreground=SLATE)
        st.configure("PanelAmber.TLabel", background=PANEL, foreground=AMBER_HOT)
        st.configure("TCheckbutton", background=BASE)
        st.map("TCheckbutton", background=[("active", BASE)])
        st.configure("TButton", font=(FONT, 10), padding=(10, 6))
        st.configure("Action.TButton", font=(FONT, 10, "bold"), padding=(12, 7))
        st.configure("TNotebook", background=BASE, borderwidth=0, tabmargins=(6, 6, 6, 0))
        st.configure("TNotebook.Tab", font=(FONT, 10, "bold"), padding=(18, 8),
                     background="#e2e8f0", foreground=MUTED)
        st.map("TNotebook.Tab", background=[("selected", BASE)],
               foreground=[("selected", INK)])
        st.configure("Treeview", font=(FONT, 10), rowheight=30, background=BASE,
                     fieldbackground=BASE, borderwidth=0)
        st.configure("Treeview.Heading", font=(FONT, 10, "bold"), padding=(6, 6),
                     background="#e2e8f0", foreground=INK, relief="flat")
        st.map("Treeview", background=[("selected", "#dbeafe")],
               foreground=[("selected", INK)])
        st.configure("TProgressbar", background=GREEN, troughcolor="#e2e8f0")

    def _flat_button(self, parent, text, cmd, bg, hot, font=(FONT, 10, "bold"),
                     padx=12, pady=6):
        btn = tk.Button(parent, text=text, command=cmd, bg=bg, fg="white",
                        activebackground=hot, activeforeground="white", font=font,
                        relief="flat", bd=0, padx=padx, pady=pady, cursor="hand2",
                        disabledforeground="#eef2f7")
        self._btn_palette[btn] = (bg, hot)
        btn.bind("<Enter>", lambda e: self._btn_hover(btn, True))
        btn.bind("<Leave>", lambda e: self._btn_hover(btn, False))
        return btn

    def _btn_hover(self, btn, on):
        if str(btn["state"]) == "disabled":
            return
        normal, hot = self._btn_palette[btn]
        btn.config(bg=hot if on else normal)

    def _enable_btn(self, btn, on):
        normal, _ = self._btn_palette[btn]
        btn.config(state="normal" if on else "disabled", bg=normal if on else BTN_OFF)

    # --- UI construction ----------------------------------------------------

    def _build_ui(self):
        self._apply_theme()

        header = ttk.Frame(self.root, style="Header.TFrame", padding=(14, 10))
        header.pack(fill="x")
        ttk.Label(header, text="🧹  Ad Cleaner", style="Header.TLabel",
                  font=(FONT, 13, "bold")).grid(row=0, column=0, padx=(0, 18))
        self.dot = tk.Canvas(header, width=18, height=18, highlightthickness=0, bg=HEADER_BG)
        self.dot.grid(row=0, column=1, padx=(0, 6))
        self._draw_dot("grey")
        self.status_var = tk.StringVar(value="Starting…")
        ttk.Label(header, textvariable=self.status_var, style="Header.TLabel").grid(
            row=0, column=2, sticky="w")
        self.model_var = tk.StringVar(value="")
        ttk.Label(header, textvariable=self.model_var, style="HeaderMuted.TLabel").grid(
            row=0, column=3, sticky="w", padx=12)
        header.columnconfigure(4, weight=1)
        self.rescan_btn = self._flat_button(header, "🔄  Rescan", self.on_rescan,
                                            SLATE, SLATE_HOT)
        self.rescan_btn.grid(row=0, column=5, padx=(0, 8))
        self.clean_btn = self._flat_button(header, "✨  CLEAN MY PHONE", self.on_clean,
                                           GREEN, GREEN_HOT, font=(FONT, 12, "bold"),
                                           padx=16, pady=8)
        self.clean_btn.grid(row=0, column=6, padx=(0, 8))
        # Secondary/emergency action — kept slate so the green CLEAN button is the
        # single loud call to action (red is reserved for the destructive confirm).
        self.stop_btn = self._flat_button(header, "⏹  STOP ALL", self.on_stop_all,
                                          SLATE, SLATE_HOT)
        self.stop_btn.grid(row=0, column=7)
        for b in (self.rescan_btn, self.clean_btn, self.stop_btn):
            self._enable_btn(b, False)
        # Make CLEAN + the verdict banner say what they'll actually do (pause vs. remove).
        self.uninstall_mode.trace_add(
            "write", lambda *_: (self._sync_clean_label(), self._show_summary(self.apps)))
        self._sync_clean_label()

        self._build_wizard()

        nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True, padx=10, pady=(6, 2))
        self._build_apps_tab(nb)
        self._build_history_tab(nb)
        self._build_device_tab(nb)
        self._build_move_tab(nb)
        self._build_crashes_tab(nb)
        self._build_help_tab(nb)
        self.notebook = nb

        self.statusbar = ttk.Label(self.root, text="Ready.", style="Status.TLabel",
                                   anchor="w", padding=(12, 6))
        self.statusbar.pack(fill="x", side="bottom")
        ttk.Separator(self.root).pack(fill="x", side="bottom")

    def _build_apps_tab(self, nb):
        tab = ttk.Frame(nb, padding=(4, 6))
        nb.add(tab, text="Apps")

        bar = ttk.Frame(tab, padding=(6, 6))
        bar.pack(fill="x")
        ttk.Label(bar, text="🔎").pack(side="left", padx=(2, 4))
        self.filter_var = tk.StringVar()
        self.filter_var.trace_add("write", lambda *_: self._render_table())
        ttk.Entry(bar, textvariable=self.filter_var, width=30).pack(side="left", padx=4)
        self.suspicious_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="Show risky apps only", variable=self.suspicious_var,
                        command=self._render_table).pack(side="left", padx=12)
        # Bulk actions: Select all ticks every row currently shown (so it honours the
        # "risky only" filter + search), then Pause/Uninstall act on the selection.
        self.selectall_btn = self._flat_button(bar, "☑  Select all", self.on_select_all,
                                               SLATE, SLATE_HOT)
        self.selectall_btn.pack(side="left", padx=(4, 4))
        self.bulk_pause_btn = self._flat_button(bar, "⏸  Pause", self.on_pause,
                                                AMBER, AMBER_HOT)
        self.bulk_pause_btn.pack(side="left", padx=4)
        self.bulk_uninstall_btn = self._flat_button(bar, "🗑  Uninstall", self.on_uninstall,
                                                    RED, RED_HOT)
        self.bulk_uninstall_btn.pack(side="left", padx=4)
        self.bulk_btns = (self.selectall_btn, self.bulk_pause_btn, self.bulk_uninstall_btn)
        for b in self.bulk_btns:
            self._enable_btn(b, False)
        ttk.Checkbutton(bar, text="🔁  Shop mode (auto-clean each phone)",
                        variable=self.shop_mode,
                        command=self._toggle_shop).pack(side="right", padx=8)
        ttk.Checkbutton(bar, text="🗑  Uninstall the junk (instead of pausing)",
                        variable=self.uninstall_mode,
                        command=self._save_settings).pack(side="right", padx=8)
        self.progress = ttk.Progressbar(bar, mode="determinate", length=220)

        self.summary = tk.Label(tab, text="", anchor="w", font=(FONT, 12, "bold"),
                                bg=BASE, fg=MUTED, padx=12, pady=9)
        self.summary.pack(fill="x", padx=6, pady=(2, 0))
        self._show_summary(None)

        mid = ttk.Frame(tab)
        mid.pack(fill="both", expand=True, padx=6)
        # displaycolumns puts plain-English columns first (name, risk, why); the
        # techie App ID / Source trail behind so a nervous user reads meaning first.
        self.tree = ttk.Treeview(mid, columns=COLUMNS, displaycolumns=DISPLAY,
                                 show="headings", selectmode="extended")
        widths = (190, 150, 112, 240, 92, 120, 84)  # COLUMNS order; 'why' flexes
        for col, head, w in zip(COLUMNS, HEADINGS, widths):
            self.tree.heading(col, text=head)
            self.tree.column(col, width=w, anchor="w", stretch=(col == "why"))
        self.tree_empty = ttk.Label(mid, style="Muted.TLabel", anchor="center",
                                    font=(FONT, 12), justify="center")
        for risk in RISK_BG:
            self.tree.tag_configure(risk, background=RISK_BG[risk], foreground=RISK_FG[risk])
        vsb = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        hsb = ttk.Scrollbar(mid, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        mid.rowconfigure(0, weight=1)
        mid.columnconfigure(0, weight=1)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.bind("<Delete>", lambda e: self.on_uninstall())
        self.tree.bind("<Button-3>", self._popup_menu)
        self._row_menu = tk.Menu(self.tree, tearoff=0)
        for lbl, cmd in (("⏸  Pause", self.on_pause), ("▶  Resume", self.on_resume),
                         ("🗑  Uninstall", self.on_uninstall), (None, None),
                         ("↺  Reset data", self.on_reset_data),
                         ("💾  Backup APK", self.on_backup_apk),
                         ("📋  Copy app ID", self._copy_pkg)):
            if lbl is None:
                self._row_menu.add_separator()
            else:
                self._row_menu.add_command(label=lbl, command=cmd)

        detail = ttk.Frame(tab, style="Panel.TFrame", padding=14)
        detail.pack(fill="x", padx=6, pady=(8, 2))
        ttk.Label(detail, text="DETAILS", style="PanelMuted.TLabel",
                  font=(FONT, 10, "bold")).pack(anchor="w")
        head = ttk.Frame(detail, style="PanelFlat.TFrame")
        head.pack(anchor="w", fill="x", pady=(2, 0))
        self.detail_icon = ttk.Label(head, style="Panel.TLabel")
        self.detail_icon.pack(side="left")
        self._icon_img = None      # keep a ref or Tk garbage-collects the image
        self._icon_cache = {}      # package -> icon path (or None = both sources failed)
        self._icon_for = None      # package whose icon fetch is in flight
        self.detail_title = ttk.Label(head, text="Select an app to see details.",
                                      style="Panel.TLabel", font=(FONT, 12, "bold"))
        self.detail_title.pack(side="left", padx=(6, 0))
        self.detail_reasons = ttk.Label(detail, text="", style="Panel.TLabel",
                                        justify="left", wraplength=940)
        self.detail_reasons.pack(anchor="w", pady=(3, 10))
        btns = ttk.Frame(detail, style="PanelFlat.TFrame")
        btns.pack(anchor="w")
        self.pause_btn = self._flat_button(btns, "⏸  Pause", self.on_pause,
                                           AMBER, AMBER_HOT)
        self.resume_btn = self._flat_button(btns, "▶  Resume", self.on_resume,
                                            SLATE, SLATE_HOT)
        self.uninstall_btn = self._flat_button(btns, "🗑  Uninstall", self.on_uninstall,
                                               RED, RED_HOT)
        self.reset_btn = self._flat_button(btns, "↺  Reset data", self.on_reset_data,
                                           SLATE, SLATE_HOT)
        self.backup_btn = self._flat_button(btns, "💾  Backup APK", self.on_backup_apk,
                                            SLATE, SLATE_HOT)
        self.fixrole_btn = self._flat_button(btns, "🛠  Restore default apps", self.on_fix_roles,
                                             GREEN, GREEN_HOT)
        self.notif_btn = self._flat_button(btns, "🔕  Stop its notifications",
                                           self.on_block_notifs, AMBER, AMBER_HOT)
        self.data_btn = self._flat_button(btns, "📵  Block background data",
                                          self.on_restrict_data, AMBER, AMBER_HOT)
        self.detail_btns = (self.pause_btn, self.resume_btn, self.uninstall_btn,
                            self.reset_btn, self.backup_btn, self.fixrole_btn, self.notif_btn,
                            self.data_btn)
        for b in self.detail_btns:
            b.pack(side="left", padx=(0, 8))
            self._enable_btn(b, False)

        ttk.Label(tab, style="Muted.TLabel", wraplength=980, justify="left",
                  text="Risk score — higher means more likely junk.    "
                       "🔴 HIGH: remove it     🟠 Medium: worth a look     "
                       "🟢 Low: probably fine.").pack(anchor="w", padx=10, pady=(6, 0))

    def _build_history_tab(self, nb):
        tab = ttk.Frame(nb, padding=(4, 6))
        nb.add(tab, text="History / Undo")
        wrap = ttk.Frame(tab)
        wrap.pack(fill="both", expand=True, padx=6, pady=6)
        cols = ("time", "package", "action", "result")
        self.hist = ttk.Treeview(wrap, columns=cols, show="headings", selectmode="browse")
        for c, w in zip(cols, (160, 300, 130, 90)):
            self.hist.heading(c, text=c.title())
            self.hist.column(c, width=w, anchor="w")
        self.hist.pack(side="left", fill="both", expand=True)
        vsb = ttk.Scrollbar(wrap, orient="vertical", command=self.hist.yview)
        self.hist.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        row = ttk.Frame(tab)
        row.pack(side="bottom", pady=8)
        self.undo_btn = self._flat_button(row, "↩  Undo selected", self.on_undo,
                                          SLATE, SLATE_HOT)
        self.undo_btn.pack(side="left", padx=6)
        self.export_btn = self._flat_button(row, "📄  Export report", self.on_export,
                                            SLATE, SLATE_HOT)
        self.export_btn.pack(side="left", padx=6)
        for b in (self.undo_btn, self.export_btn):
            self._enable_btn(b, True)  # validate on click
        self._refresh_history()

    def _build_device_tab(self, nb):
        tab = ttk.Frame(nb, padding=18)
        nb.add(tab, text="Device")
        ttk.Label(tab, text="Device maintenance", font=(FONT, 14, "bold")).pack(anchor="w")
        ttk.Label(tab, text="Storage, memory and temperature for the connected phone.",
                  style="Muted.TLabel").pack(anchor="w", pady=(2, 14))

        self.dev_vars = {k: tk.StringVar(value="—")
                         for k in ("storage", "ram", "temp", "battery",
                                   "battery_health", "top_drainer")}
        self.dev_labels = {}
        grid = ttk.Frame(tab)
        grid.pack(anchor="w")
        rows = [("💾  Storage", "storage"), ("🧠  Memory (RAM)", "ram"),
                ("🌡️  Battery temperature", "temp"), ("🔋  Battery level", "battery"),
                ("🔋  Battery health", "battery_health"),
                ("⚡  Top battery user", "top_drainer")]
        for i, (label, key) in enumerate(rows):
            ttk.Label(grid, text=label, font=(FONT, 11, "bold")).grid(
                row=i, column=0, sticky="w", padx=(0, 24), pady=6)
            lbl = ttk.Label(grid, textvariable=self.dev_vars[key], font=(FONT, 11))
            lbl.grid(row=i, column=1, sticky="w", pady=6)
            self.dev_labels[key] = lbl

        btns = ttk.Frame(tab)
        btns.pack(anchor="w", pady=(18, 0))
        self.dev_refresh_btn = self._flat_button(btns, "🔄  Refresh",
                                                 self.on_dev_refresh, SLATE, SLATE_HOT)
        self.dev_refresh_btn.pack(side="left", padx=(0, 8))
        self.cache_btn = self._flat_button(btns, "🧹  Clear app caches",
                                           self.on_clear_caches, GREEN, GREEN_HOT)
        self.cache_btn.pack(side="left", padx=(0, 8))
        self.shot_btn = self._flat_button(btns, "📷  Screenshot",
                                          self.on_screenshot, SLATE, SLATE_HOT)
        self.shot_btn.pack(side="left", padx=(0, 8))
        self.reboot_btn = self._flat_button(btns, "🔌  Reboot phone",
                                            self.on_reboot, SLATE, SLATE_HOT)
        self.reboot_btn.pack(side="left")
        self.popups_btn = self._flat_button(btns, "🚫  Stop fake virus pop-ups (Chrome)",
                                            self.on_chrome_popups, AMBER, AMBER_HOT)
        self.popups_btn.pack(side="left", padx=(8, 0))
        self.bloat_btn = self._flat_button(btns, "💤  Disable preinstalled junk",
                                           self.on_debloat, AMBER, AMBER_HOT)
        self.bloat_btn.pack(side="left", padx=(8, 0))
        self.dev_btns = (self.dev_refresh_btn, self.cache_btn, self.shot_btn,
                         self.reboot_btn, self.popups_btn, self.bloat_btn)
        for b in self.dev_btns:
            self._enable_btn(b, False)
        ttk.Label(tab, text="Clearing caches frees space and can fix misbehaving apps. "
                            "It never deletes your photos, messages or accounts.",
                  style="Muted.TLabel", wraplength=760).pack(anchor="w", pady=(12, 0))

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
        ttk.Label(tab, text="Use “Custom” only if you know the DNS address — a wrong one "
                            "can stop the phone's internet (undo with Turn off).",
                  style="Muted.TLabel", wraplength=760).pack(anchor="w", pady=(2, 0))
        for b in (self.dns_on_btn, self.dns_off_btn):
            self._enable_btn(b, False)
        self.dns_btns = (self.dns_on_btn, self.dns_off_btn)
        self._sync_dns_custom()

    def _build_move_tab(self, nb):
        tab = ttk.Frame(nb, padding=18)
        nb.add(tab, text="Move to new phone")
        ttk.Label(tab, text="Move to a new phone", font=(FONT, 14, "bold")).pack(anchor="w")
        ttk.Label(tab, text="Copies your photos, videos, music and downloads from an old "
                            "phone to a new one, using this PC in between. Nothing is "
                            "deleted from either phone.",
                  style="Muted.TLabel", wraplength=820).pack(anchor="w", pady=(2, 14))

        ttk.Label(tab, text="Step 1 — plug in the OLD phone, then press Save",
                  font=(FONT, 12, "bold")).pack(anchor="w")
        ttk.Label(tab, text="Copies DCIM (camera), Pictures, Movies, Music, Downloads and "
                            "Documents onto this computer.",
                  style="Muted.TLabel", wraplength=820).pack(anchor="w", pady=(2, 6))
        self.move_save_btn = self._flat_button(
            tab, "⬇  Save photos & files to this PC", self.on_move_save, GREEN, GREEN_HOT)
        self.move_save_btn.pack(anchor="w", pady=(0, 16))

        ttk.Label(tab, text="Step 2 — unplug the old phone, plug in the NEW phone, press Copy",
                  font=(FONT, 12, "bold")).pack(anchor="w")
        ttk.Label(tab, text="Puts the saved photos and files onto the new phone.",
                  style="Muted.TLabel", wraplength=820).pack(anchor="w", pady=(2, 6))
        self.move_copy_btn = self._flat_button(
            tab, "⬆  Copy onto the new phone", self.on_move_copy, SLATE, SLATE_HOT)
        self.move_copy_btn.pack(anchor="w", pady=(0, 8))
        self.move_status = tk.StringVar(value="")
        ttk.Label(tab, textvariable=self.move_status, style="Muted.TLabel",
                  wraplength=820, justify="left").pack(anchor="w")

        self.move_btns = (self.move_save_btn, self.move_copy_btn)
        for b in self.move_btns:
            self._enable_btn(b, False)

        ttk.Separator(tab, orient="horizontal").pack(fill="x", pady=(18, 12))
        ttk.Label(tab, text="📇  Contacts, messages & apps",
                  font=(FONT, 12, "bold")).pack(anchor="w")
        ttk.Label(tab, text="These can't move over the USB cable — Android protects them. "
                            "The easy, built-in way:\n"
                            "•  Contacts & calendar: sign into the same Google account on the "
                            "new phone and they come back on their own.\n"
                            "•  Apps, texts and the rest: use the new phone's own “Copy apps "
                            "& data” wizard during setup (on Samsung it's “Smart Switch”). "
                            "It moves the lot, with a cable between the two phones — no PC "
                            "needed.",
                  style="Muted.TLabel", wraplength=820, justify="left").pack(
            anchor="w", pady=(2, 8))
        self._flat_button(tab, "🔗  How to move contacts & apps (opens a guide)",
                          self.on_move_guide, SLATE, SLATE_HOT).pack(anchor="w")

    def on_move_save(self):
        if self.busy or not self.serial:
            return
        base = self.model or self.serial or "phone"
        safe = "".join(c if c.isalnum() else "_" for c in base).strip("_") or "phone"
        dest = data_dir() / "transfers" / f"{safe}_{datetime.now():%Y%m%d_%H%M%S}"
        self.busy = True
        for b in self.move_btns:
            self._enable_btn(b, False)
        self.move_status.set("Copying photos and files to this PC… this can take several "
                             "minutes for a phone full of photos. Please leave it plugged in.")
        self.status_line("Saving files from the phone…")

        def work():
            try:
                saved, skipped, failed = _pull_media(self.adb, dest)
                if not failed:
                    (dest / TRANSFER_DONE_MARK).write_text("ok", encoding="utf-8")
                self._post(self._move_save_done, dest, saved, failed, None)
            except Exception as e:
                self._post(self._move_save_done, dest, [], [], str(e))

        self._run_bg(work)

    def _move_save_done(self, dest, saved, failed, err):
        self.busy = False
        for b in self.move_btns:
            self._enable_btn(b, bool(self.serial))
        if err:
            self.move_status.set("")
            self.status_line("Couldn't save the files. " + self._friendly(err), "error")
            return
        if failed:
            # Partial save: never show the ✅ a tech would wipe the old phone on.
            self.move_status.set(
                f"⚠ Couldn't finish — {', '.join(failed)} did not copy"
                + (f" ({', '.join(saved)} did)" if saved else "") + ".\n"
                "Do NOT wipe or trade in the old phone yet. Check the cable and "
                "press Save again — a fresh folder will be made.")
            self.status_line("Saving did not finish. Check the cable and try again.",
                             "error")
            return
        if not saved:
            self.move_status.set("Nothing was found to copy on this phone.")
            self.status_line("No photos or files found to copy.", "error")
            return
        self._last_transfer_dir = dest
        self.move_status.set(f"✅ Saved {', '.join(saved)} to:\n{dest}\n\n"
                             "Now unplug this phone, plug in the NEW phone, and press "
                             "“Copy onto the new phone”.")
        self.status_line(f"✅ Saved {len(saved)} folder(s) to {dest}", "good")

    def on_move_copy(self):
        if self.busy or not self.serial:
            return
        src = self._last_transfer_dir
        if not src:
            (data_dir() / "transfers").mkdir(parents=True, exist_ok=True)
            chosen = filedialog.askdirectory(
                title="Pick the folder you saved in Step 1",
                initialdir=str(data_dir() / "transfers"))
            if not chosen:
                return
            src = Path(chosen)
        # Guard the picker path: a Step-1 folder only ever contains the standard
        # folder names. Picking the transfers/ ROOT (every past customer's saves)
        # or some random huge folder must not end up on the phone.
        try:
            subdirs = [d.name for d in src.iterdir() if d.is_dir()]
        except OSError:
            subdirs = []
        if not subdirs or any(name not in TRANSFER_FOLDERS for name in subdirs):
            messagebox.showwarning(
                "Pick the saved folder",
                "That doesn't look like a folder saved in Step 1.\n\n"
                "Open the “transfers” folder and pick ONE of the dated folders "
                "inside it (the name starts with the old phone's model).")
            return
        warn = ("" if (src / TRANSFER_DONE_MARK).exists() else
                "\n\n⚠ This save may not have finished — some files could be missing.")
        if not messagebox.askyesno(
                "Copy onto the new phone",
                f"Copy the photos and files from\n\n{src}\n\nonto "
                f"“{self.model or self.serial}” (the phone plugged in now)?\n\n"
                f"Make sure this is the NEW phone.{warn}", default="yes"):
            return
        self.busy = True
        for b in self.move_btns:
            self._enable_btn(b, False)
        self.move_status.set("Copying onto the new phone… this can take several minutes. "
                             "Please leave the phone plugged in.")
        self.status_line("Copying files onto the phone…")

        def work():
            try:
                pushed, failed = _push_media(self.adb, src)
                self._post(self._move_copy_done, pushed, failed, None)
            except Exception as e:
                self._post(self._move_copy_done, [], [], str(e))

        self._run_bg(work)

    def _move_copy_done(self, pushed, failed, err):
        self.busy = False
        for b in self.move_btns:
            self._enable_btn(b, bool(self.serial))
        if err:
            self.move_status.set("")
            self.status_line("Couldn't copy onto the phone. " + self._friendly(err), "error")
            return
        if not pushed:
            self.move_status.set("Nothing was copied — the saved folder was empty."
                                 if not failed else
                                 "Couldn't copy " + ", ".join(failed) + " — try again.")
            self.status_line("Nothing was copied.", "error")
            return
        note = (f"  ⚠ Couldn't copy: {', '.join(failed)} — press Copy again to retry."
                if failed else "")
        if not failed:
            self._last_transfer_dir = None  # done; next customer gets the picker
        self.move_status.set(f"✅ Copied {', '.join(pushed)} onto the new phone.{note} "
                             "Open the Gallery on the phone to check your photos.")
        self.status_line(f"✅ Copied {len(pushed)} folder(s) onto the new phone.",
                         "good" if not failed else "info")

    def on_move_guide(self):
        webbrowser.open(
            "https://www.google.com/search?q=copy+apps+and+data+to+new+android+phone")

    def _build_crashes_tab(self, nb):
        tab = ttk.Frame(nb, padding=18)
        nb.add(tab, text="Crashes")
        ttk.Label(tab, text="Why did my phone crash?",
                  font=(FONT, 14, "bold")).pack(anchor="w")
        ttk.Label(tab, text="Reads the phone's own crash, freeze and restart records and "
                            "explains them in plain English. Nothing is changed.",
                  style="Muted.TLabel", wraplength=820).pack(anchor="w", pady=(2, 12))

        self.crash_summary = tk.Label(tab, text="Press “Check for crashes” below.",
                                      anchor="w", font=(FONT, 12, "bold"),
                                      bg=BASE, fg=MUTED, padx=12, pady=9)
        self.crash_summary.pack(fill="x", pady=(0, 4))
        self.crash_boot = ttk.Label(tab, text="", style="Muted.TLabel", wraplength=820)
        self.crash_boot.pack(anchor="w", pady=(0, 8))

        wrap = ttk.Frame(tab)
        wrap.pack(fill="both", expand=True)
        cols = ("when", "what", "detail")
        self.crash_tree = ttk.Treeview(wrap, columns=cols, show="headings",
                                       selectmode="browse")
        for c, head, w in zip(cols, ("When", "What happened", "App / service"),
                              (150, 260, 300)):
            self.crash_tree.heading(c, text=head)
            self.crash_tree.column(c, width=w, anchor="w", stretch=(c == "detail"))
        self.crash_tree.tag_configure("fault", foreground=RED)
        vsb = ttk.Scrollbar(wrap, orient="vertical", command=self.crash_tree.yview)
        self.crash_tree.configure(yscrollcommand=vsb.set)
        self.crash_tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        self.crash_btn = self._flat_button(tab, "🔎  Check for crashes",
                                           self.on_check_crashes, SLATE, SLATE_HOT)
        self.crash_btn.pack(anchor="w", pady=(10, 0))
        self._enable_btn(self.crash_btn, False)

    def on_check_crashes(self):
        if self.busy or not self.serial:
            return
        self.busy = True
        self._enable_btn(self.crash_btn, False)
        self.crash_summary.config(
            text="Reading the phone's crash records… (this can take a moment)",
            bg=BASE, fg=MUTED)

        def work():
            try:
                report = read_crash_report(self.adb)
                self._post(self._show_crashes, report, None)
            except Exception as e:
                self._post(self._show_crashes, None, str(e))

        self._run_bg(work)

    def _show_crashes(self, report, err):
        self.busy = False
        if self.serial:
            self._enable_btn(self.crash_btn, True)
        if err:
            bg, fg = BANNER["alert"]
            self.crash_summary.config(
                text="Couldn't read crash records. " + self._friendly(err), bg=bg, fg=fg)
            return
        text, kind = summarize(report["events"])
        bg, fg = BANNER[kind]
        self.crash_summary.config(text=text, bg=bg, fg=fg)
        self.crash_boot.config(text="Last restart:  " + report["boot_text"])
        self.crash_tree.delete(*self.crash_tree.get_children())
        for i, e in enumerate(report["events"]):
            self.crash_tree.insert("", "end", iid=str(i),
                                   tags=(("fault",) if e.is_fault else ()),
                                   values=(e.when, e.label, e.detail))
        if not report["events"]:
            self.crash_tree.insert("", "end", values=("", "No crash records found.", ""))

    def _build_help_tab(self, nb):
        tab = ttk.Frame(nb)
        nb.add(tab, text="Help")
        txt = tk.Text(tab, wrap="word", padx=18, pady=16, font=(FONT, 10), relief="flat",
                      bg=BASE, fg=INK, highlightthickness=0, borderwidth=0)
        txt.insert("1.0", HELP_TEXT)
        txt.configure(state="disabled")
        txt.pack(fill="both", expand=True, padx=6, pady=6)

    # --- connect wizard (shown until the phone is connected) ----------------

    def _build_wizard(self):
        self.wizard = ttk.Frame(self.root, style="Panel.TFrame", padding=(20, 16))
        self.wizard.pack(fill="x", padx=10, pady=(8, 2))
        ttk.Label(self.wizard, text="📱  Let's connect your phone", style="Panel.TLabel",
                  font=(FONT, 15, "bold")).pack(anchor="w")
        self.wiz_status = tk.StringVar(value="Looking for your phone…")
        self.wiz_status_lbl = ttk.Label(self.wizard, textvariable=self.wiz_status,
                                        style="PanelInfo.TLabel", font=(FONT, 12, "bold"))
        self.wiz_status_lbl.pack(anchor="w", pady=(3, 12))

        steps = [
            "Turn on “USB debugging” on the phone",
            "Plug the phone into this computer with a USB cable",
            "On the phone, tap “Allow” (tick “Always allow from this computer”)",
        ]
        self.step_icons = []
        for i, text in enumerate(steps):
            row = ttk.Frame(self.wizard, style="PanelFlat.TFrame")
            row.pack(fill="x", pady=3)
            icon = ttk.Label(row, text="⬜", style="Panel.TLabel", font=(FONT, 14), width=2)
            icon.pack(side="left", padx=(0, 10))
            self.step_icons.append(icon)
            ttk.Label(row, text=text, style="Panel.TLabel", font=(FONT, 11)).pack(side="left")
            if i == 0:
                brow = ttk.Frame(self.wizard, style="PanelFlat.TFrame")
                brow.pack(fill="x", padx=36, pady=(2, 0))
                ttk.Label(brow, text="Which phone?", style="PanelMuted.TLabel").pack(
                    side="left", padx=(0, 8))
                self.brand_var = tk.StringVar(
                    value=self._settings.get("brand", "Other / not sure"))
                ttk.OptionMenu(brow, self.brand_var, self.brand_var.get(),
                               *BRAND_STEPS, command=lambda *_: self._show_brand()).pack(
                    side="left")
                self.brand_help = ttk.Label(
                    self.wizard, text=BRAND_STEPS["Other / not sure"], wraplength=900,
                    justify="left", style="PanelMuted.TLabel")
                self.brand_help.pack(anchor="w", padx=36, pady=(3, 6))
        self._set_wizard_state("searching")

    def _show_brand(self):
        self.brand_help.config(text=BRAND_STEPS[self.brand_var.get()])
        self._save_settings()

    def _mark_steps(self, current, done=0):
        for i, icon in enumerate(self.step_icons):
            icon.config(text="✅" if i < done else ("👉" if i == current else "⬜"))

    def _set_wizard_state(self, state):
        if state == "connected":
            if self.wizard.winfo_manager():
                self.wizard.pack_forget()
            return
        if not self.wizard.winfo_manager():
            self.wizard.pack(fill="x", padx=10, pady=(8, 2), before=self.notebook)
        if state == "unauthorized":
            self.wiz_status_lbl.config(style="PanelAmber.TLabel")
            self.wiz_status.set("Almost there!  Now tap “Allow” on the phone screen.")
            self._mark_steps(current=2, done=2)
        else:  # searching
            self.wiz_status_lbl.config(style="PanelInfo.TLabel")
            self.wiz_status.set("Looking for your phone…  plug it in with a USB cable.")
            self._mark_steps(current=0, done=0)

    def _draw_dot(self, color):
        self.dot.delete("all")
        self.dot.create_oval(3, 3, 16, 16, fill=DOT[color], outline="")

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
            self._disconnect("No phone connected", "grey")
            self._set_wizard_state("searching")
        elif unauth and not ready:
            self._disconnect("Tap “Allow” on the phone", "orange")
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
        self.android = android
        self._set_status("green", "Connected")
        self._set_wizard_state("connected")
        extra = f"Android {android}" if android else ""
        self.model_var.set(f"{model}   {extra}".strip())
        self._enable_btn(self.rescan_btn, True)
        self._enable_btn(self.clean_btn, True)
        self._enable_btn(self.stop_btn, True)
        for b in self.bulk_btns + self.dev_btns + self.dns_btns + self.move_btns:
            self._enable_btn(b, True)
        self._enable_btn(self.crash_btn, True)
        self._refresh_device()
        self._refresh_dns()
        self.status_line("Phone connected. Scanning apps…")
        self.on_rescan()

    def _disconnect(self, message, color):
        was = self.serial
        self.serial = None
        if self.adb:
            self.adb = Adb(self.adb.adb_path)  # drop the -s binding
        self._set_status(color, message)
        self.model_var.set("")
        self._enable_btn(self.rescan_btn, False)
        self._enable_btn(self.clean_btn, False)
        self._enable_btn(self.stop_btn, False)
        for b in self.bulk_btns + self.dev_btns + self.dns_btns + self.move_btns:
            self._enable_btn(b, False)
        self._enable_btn(self.crash_btn, False)
        for v in self.dev_vars.values():
            v.set("—")
        self.battery_report = None  # don't let phone A's health survive into phone B's session
        if was:
            self.status_line("Phone disconnected.")
            self.apps = []
            self._render_table()
            self._show_summary(None)
            self._clear_detail()

    # --- scanning -----------------------------------------------------------

    def on_rescan(self):
        if self.busy or not self.serial:
            return
        self.busy = True
        self._enable_btn(self.rescan_btn, False)
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
            self._enable_btn(self.rescan_btn, True)
        risky = sum(a.risk in SUSPICIOUS for a in apps)
        self.suspicious_var.set(risky > 0)   # auto-focus the risky ones if any exist
        self._render_table()
        self._show_summary(apps)
        self._start_play_checks(apps)
        self.status_line(f"Scan complete: {len(apps)} downloaded apps, {risky} flagged.",
                         "good" if risky == 0 else "info")
        if self._pending_clean:
            self._pending_clean = False
            self._start_clean()
        elif self.shop_mode.get():
            if any(will_clean(a) for a in apps):
                self._start_clean()
            else:
                self._set_summary("✅  Clean — unplug and connect the next phone.", "good")
                self.status_line("✅ Nothing risky found — this phone looks clean. "
                                 "Unplug and connect the next one.", "good")

    def _scan_failed(self, err):
        self.busy = False
        self.progress.pack_forget()
        if self.serial:
            self._enable_btn(self.rescan_btn, True)
        self.status_line("Couldn't scan. " + self._friendly(err), "error")

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

    def _row_values(self, a):
        installed = a.first_install.strftime("%Y-%m-%d") if a.first_install else ""
        why = (a.reasons[0] + (f"   +{len(a.reasons) - 1} more"
                               if len(a.reasons) > 1 else "")) if a.reasons else ""
        name = ("🔒 " if a.protected else "") + a.label.split(" (")[0]
        risk = f"{RISK_DOT.get(a.risk, '')} {a.risk} ({a.score})"
        return (name, a.package, risk, why, installed, a.source, a.status)

    def _render_table(self):
        self.tree.delete(*self.tree.get_children())
        for a in self._visible_apps():
            self.tree.insert("", "end", iid=a.package, tags=(a.risk,),
                             values=self._row_values(a))
        # Never leave a blank grid — a clean phone must not look like a failure.
        if self.tree.get_children():
            self.tree_empty.place_forget()
        else:
            self.tree_empty.config(text=self._empty_message())
            self.tree_empty.place(relx=0.5, rely=0.4, anchor="center")

    def _set_summary(self, text, kind):
        """Verdict banner above the table. `text` empty -> clear it."""
        if not text:
            self.summary.config(text="", bg=BASE, fg=MUTED)
            return
        bg, fg = BANNER[kind]
        self.summary.config(text=text, bg=bg, fg=fg)

    def _show_summary(self, apps):
        if not apps:
            self._set_summary("", None)
            return
        highs = sum(a.risk == "HIGH" for a in apps)
        meds = sum(a.risk == "Medium" for a in apps)
        if highs + meds == 0:
            self._set_summary("✅  No risky apps found — this phone looks clean.", "good")
            return
        parts = ([f"{highs} HIGH"] if highs else []) + ([f"{meds} Medium"] if meds else [])
        verb = "uninstall" if self.uninstall_mode.get() else "pause"
        self._set_summary(
            f"⚠️  {highs + meds} risky app(s) found  ({', '.join(parts)})  —  "
            f"press CLEAN MY PHONE to {verb} them.",
            "alert" if highs else "warn")

    def _empty_message(self):
        if self.busy:
            return "Checking this phone…"
        if not self.serial:
            return "No phone connected — follow the steps above to connect it."
        if self.filter_var.get().strip():
            return "No apps match your search."
        if self.suspicious_var.get():
            return "Good news — no risky apps found on this phone. 🎉"
        return "No downloaded apps found.\nPress 🔄 Rescan to check again."

    def _app_by_pkg(self, pkg):
        return next((a for a in self.apps if a.package == pkg), None)

    def _on_select(self, _evt=None):
        sel = self.tree.selection()
        self.selected = self._app_by_pkg(sel[0]) if sel else None
        self._update_detail()

    def on_select_all(self):
        """Tick every row currently shown in the table (respects the active filter)."""
        items = self.tree.get_children()
        if not items:
            return
        self.tree.selection_set(items)
        self.tree.focus(items[0])
        self._on_select()
        self.status_line(f"Selected {len(items)} app(s). Press Pause or Uninstall.", "info")

    def _clear_detail(self):
        self.selected = None
        self.detail_title.config(text="Select an app to see details.")
        self.detail_reasons.config(text="")
        self._set_detail_icon(None)
        for b in self.detail_btns:
            self._enable_btn(b, False)

    def _update_detail(self):
        a = self.selected
        if not a:
            self._clear_detail()
            return
        self.detail_title.config(text=f"{a.label}  —  Risk: {a.risk} ({a.score})")
        self._fetch_icon(a)
        if a.protected:
            self.detail_reasons.config(text="🔒 Protected system app — this one is kept safe "
                                            "and cannot be changed.")
            for b in self.detail_btns:
                self._enable_btn(b, False)
            return
        lines = ["• " + r for r in a.reasons] or ["Nothing suspicious found."]
        if a.play and a.play.get("listed") and a.play.get("name"):
            lines.append(f"✔ On Google Play as “{a.play['name']}” — compare that name "
                         "and icon with what the phone shows.")
        elif a.play and not a.play.get("listed"):
            lines.append("• " + playstore.NOT_LISTED_REASON)
        if a.sensitive_perms:
            lines.append("")
            lines.append("Permissions it has:  " + ", ".join(a.sensitive_perms))
        if a.data_mb >= 1:
            lines.append("")
            lines.append(f"Data used: {a.data_mb} MB")
        if a.used_min:
            lines.append("")
            lines.append(f"Used about {a.used_min} min recently")
        if STALKER_REASON in a.reasons:
            lines.append("")
            lines.append("⚠ This looks like a hidden tracking app. Ask the customer "
                          "privately whether they expected it — removing it can alert "
                          "whoever installed it.")
        self.detail_reasons.config(text="\n".join(lines))
        self._enable_btn(self.pause_btn, a.enabled)
        self._enable_btn(self.resume_btn, not a.enabled)
        self._enable_btn(self.uninstall_btn, True)
        self._enable_btn(self.reset_btn, True)
        self._enable_btn(self.backup_btn, True)
        self._enable_btn(self.fixrole_btn, bool(a.hijacked_roles))
        self._enable_btn(self.notif_btn, a.notif_count > 0)
        self._enable_btn(self.data_btn, a.uid >= 10000)

    # --- Google Play check + app icons (best effort, display-only) -----------

    def _start_play_checks(self, apps):
        """Ask Google Play about every scanned package, in the background.
        Disk-cached, so repeat phones are instant; offline just means unknown.
        Display-only: never touches score or reasons, so it can't defeat
        will_clean's unattended nuisance-only fence.
        ponytail: one sequential worker thread; pool it if the trickle annoys."""
        def work():
            for a in apps:
                info = playstore.lookup(a.package)
                if info:
                    self._post(self._apply_play, a, info)
        self._run_bg(work)

    def _apply_play(self, a, info):
        if a not in self.apps:          # a rescan replaced the list meanwhile
            return
        # Store the verdict on a.play only -- NOT a.reasons. reasons feeds
        # will_clean's unattended nuisance fence (an exact-list check there),
        # and it's read by the clean worker thread while this runs on the UI
        # thread, so mutating it here would be an unguarded cross-thread write.
        a.play = info
        if self.tree.exists(a.package):
            self.tree.item(a.package, values=self._row_values(a))
        if self.selected is a:
            if self._icon_cache.get(a.package) is None:
                self._icon_cache.pop(a.package, None)   # Play icon may work now
            self._update_detail()

    def _fetch_icon(self, a):
        pkg = a.package
        if pkg in self._icon_cache:
            self._set_detail_icon(self._icon_cache[pkg])
            return
        self._set_detail_icon(None)
        if self._icon_for == pkg:       # fetch already in flight
            return
        self._icon_for = pkg
        adb, play = self.adb, a.play
        def work():
            path = appicon.device_icon(adb, pkg) if adb else None
            if path is None and play and play.get("icon"):
                data = playstore.fetch_icon(play["icon"])
                if data:
                    path = appicon.save_play_icon(pkg, data)
            self._post(self._icon_done, pkg, path)
        self._run_bg(work)

    def _icon_done(self, pkg, path):
        self._icon_cache[pkg] = path
        if self._icon_for == pkg:
            self._icon_for = None
        if self.selected and self.selected.package == pkg:
            self._set_detail_icon(path)

    def _set_detail_icon(self, path):
        img = None
        if path:
            try:
                img = tk.PhotoImage(file=str(path))
                # Pillow-less fallback icons come APK-sized; shrink to ~64px.
                f = max(img.width(), img.height()) // 65 + 1
                if f > 1:
                    img = img.subsample(f, f)
            except tk.TclError:
                img = None
        self._icon_img = img
        self.detail_icon.config(image=img or "")

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

    def _actionable_selection(self):
        """Non-protected apps currently selected in the table (multi-select aware)."""
        if not self.serial or self.busy:
            return []
        ids = self.tree.selection()
        apps = ([self._app_by_pkg(i) for i in ids] if ids
                else ([self.selected] if self.selected else []))
        apps = [a for a in apps if a]
        actionable = [a for a in apps if not a.protected]
        if apps and not actionable:
            messagebox.showinfo("Protected app",
                                "Protected system apps are kept safe and can't be changed.")
        return actionable

    def _confirm_bulk(self, verb, apps, note):
        names = "\n".join("     •  " + a.label.split(" (")[0] for a in apps[:10])
        if len(apps) > 10:
            names += f"\n     •  …and {len(apps) - 10} more"
        # Select all with the risky filter off ticks EVERYTHING, so call out how
        # many of these the scan thinks are fine (WhatsApp, banking, ...).
        safe = sum(1 for a in apps if a.risk not in SUSPICIOUS)
        warn = (f"\n\n⚠ {safe} of these look SAFE (Low risk). Uncheck them "
                "unless you mean it." if safe else "")
        return messagebox.askyesno(
            f"{verb} {len(apps)} app(s)",
            f"{verb} these {len(apps)} app(s)?\n\n{names}{warn}\n\n{note}", default="no")

    def on_pause(self):
        apps = [a for a in self._actionable_selection() if a.enabled]
        if not apps:
            return
        if not self._confirm_bulk("Pause", apps,
                                  "They stop running until you press Resume."):
            return
        self._do_bulk(lambda a: pause(self.adb, a, self.log), apps, "Paused")

    def on_resume(self):
        apps = [a for a in self._actionable_selection() if not a.enabled]
        if apps:
            self._do_bulk(lambda a: resume(self.adb, a, self.log), apps, "Resumed")

    def on_uninstall(self):
        apps = self._actionable_selection()
        if not apps:
            return
        if not self._confirm_bulk("Uninstall", apps,
                                  "Removed apps can be restored from the History tab."):
            return
        self._do_bulk(lambda a: uninstall(self.adb, a, self.log), apps, "Uninstalled",
                      removes=True)

    def on_reset_data(self):
        a = self._guarded()
        if not a:
            return
        if not messagebox.askyesno(
                "Reset app data",
                f"Erase all saved data for \"{a.label}\"?\n\n"
                "Fixes a hijacked browser or home screen without uninstalling — the app "
                "stays installed but is reset to fresh.", default="no"):
            return
        self._do_action(lambda: reset_app_data(self.adb, a, self.log), a, "Reset")

    def on_fix_roles(self):
        a = self.selected
        if not a or self.busy or not self.serial:
            return

        def work():
            restored = []
            try:
                for friendly in list(a.hijacked_roles):
                    role_id = ROLE_IDS.get(friendly)
                    if role_id:
                        pkg = fix_role(self.adb, role_id, a.package, self.log)
                        if pkg:
                            restored.append(friendly)
            except AdbError:
                pass
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

    def on_block_notifs(self):
        a = self.selected
        if not a or self.busy or not self.serial:
            return
        label = a.label.split(" (")[0]

        def work():
            try:
                block_notifications(self.adb, a.package, self.log)
                self._post(self._block_notifs_done, label, None)
            except AdbError as e:
                self._post(self._block_notifs_done, label, str(e))

        self.busy = True
        self._run_bg(work)

    def _block_notifs_done(self, label, err):
        self.busy = False
        if err:
            self.status_line("Couldn't stop notifications. " + self._friendly(err), "error")
        else:
            self.status_line(f"✅ Notifications stopped for {label}", "good")

    def on_restrict_data(self):
        a = self.selected
        if not a or self.busy or not self.serial:
            return
        label = a.label.split(" (")[0]

        def work():
            try:
                restrict_background(self.adb, a.package, a.uid, self.log)
                self._post(self._restrict_data_done, label, None)
            except (AdbError, ProtectedAppError) as e:
                self._post(self._restrict_data_done, label, str(e))

        self.busy = True
        self._run_bg(work)

    def _restrict_data_done(self, label, err):
        self.busy = False
        if err:
            self.status_line("Couldn't block background data. " + self._friendly(err), "error")
        else:
            self.status_line(f"✅ Background data blocked for {label}", "good")

    def on_backup_apk(self):
        a = self.selected
        if not a or not self.serial or self.busy:
            return
        dest = data_dir() / "apk_backups"
        self.busy = True
        self.status_line(f"Backing up {a.label.split(' (')[0]}…")

        def work():
            try:
                saved = backup_apk(self.adb, a, dest)
                self._post(self._backup_done, len(saved), str(dest), None)
            except Exception as e:
                self._post(self._backup_done, 0, "", str(e))

        self._run_bg(work)

    def _backup_done(self, n, dest, err):
        self.busy = False
        if err:
            self.status_line("Backup failed. " + self._friendly(err), "error")
        else:
            self.status_line(f"✅ Saved {n} APK file(s) to {dest}", "good")

    def _do_bulk(self, fn, apps, verb, removes=False):
        self.busy = True
        total = len(apps)

        def work():
            done, removed = 0, []
            for i, a in enumerate(apps, 1):
                self._post(self.status_line,
                           f"{verb} {i} of {total}: {a.label.split(' (')[0]}…")
                try:
                    if fn(a):
                        done += 1
                        if removes:
                            removed.append(a.package)
                except Exception:
                    pass
            self._post(self._bulk_done, verb, done, total, removed)

        self._run_bg(work)

    def _bulk_done(self, verb, done, total, removed):
        self.busy = False
        if removed:
            gone = set(removed)
            self.apps = [a for a in self.apps if a.package not in gone]
            self.selected = None
        self._refresh_history()
        self._render_table()
        self._show_summary(self.apps)
        self._update_detail()
        self.status_line(f"{verb} {done} of {total} app(s).",
                         "good" if done else "error")

    def _copy_pkg(self):
        if self.selected:
            self.root.clipboard_clear()
            self.root.clipboard_append(self.selected.package)
            self.status_line(f"Copied {self.selected.package}")

    def _popup_menu(self, event):
        row = self.tree.identify_row(event.y)
        if row and row not in self.tree.selection():
            self.tree.selection_set(row)
        self._on_select()
        try:
            self._row_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self._row_menu.grab_release()

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
            self.status_line("Couldn't finish. " + self._friendly(err), "error")
            self._update_detail()
            return
        if not ok:
            self.status_line(f"{verb}? The phone didn't confirm the change.", "error")
        else:
            self.status_line(f"{verb}: {app.label.split(' (')[0]}.", "good")
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

    def _free_gb(self):
        """Free space on /data in GB, or 0.0 if it can't be read.
        ponytail: reuses read_device_stats (0.1 GB granularity); sub-100 MB
        cache trims read as 0 freed, which is fine for a receipt.
        """
        try:
            return read_device_stats(self.adb).get("storage_free_gb", 0) or 0.0
        except Exception:
            return 0.0

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
            if self.battery_report and self.battery_report["health_pct"]:
                receipt["battery_health"] = (
                    f"{self.battery_report['health_pct']}% of original capacity")
            top_used = sorted((a for a in self.apps if a.used_min > 0),
                               key=lambda a: -a.used_min)[:3]
            if top_used:
                receipt["most_used"] = ", ".join(
                    f"{a.label.split(' (')[0] or a.package} ({a.used_min} min)" for a in top_used)
            folder = data_dir() / "reports"
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"receipt_{datetime.now():%Y%m%d_%H%M%S}.html"
            path.write_text(render_receipt_html(receipt), encoding="utf-8")
            return path
        except Exception:
            return None

    def _start_clean(self):
        risky = [a for a in self.apps if will_clean(a)]
        n = len(risky)
        remove = self.uninstall_mode.get()
        verb = "Uninstall" if remove else "Pause"
        note = ("Removed apps can be restored later from the History tab." if remove
                else "Nothing is deleted — you can undo anything from the History tab.")
        names = "\n".join("     •  " + a.label.split(" (")[0] for a in risky[:8])
        if n > 8:
            names += f"\n     •  …and {n - 8} more"
        if not names:
            names = "     (none — just closing apps and blocking pop-ups)"
        if not self.shop_mode.get() and not messagebox.askyesno(
                "Clean this phone",
                "Ad Cleaner will now close every downloaded app, block pop-ups, and\n"
                f"{verb.lower()} these {n} junk / pop-up app(s):\n\n"
                f"{names}\n\n"
                f"{note}\n\n"
                "Go ahead?  (press Enter for Yes)",
                default="yes"):
            return
        self.busy = True
        self.status_line("Cleaning your phone…")

        def progress(i, total, pkg):
            self._post(self.status_line, f"Closing apps… {i} of {total}")

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

        self._run_bg(work)

    def _clean_done(self, res, err):
        self.busy = False
        if err:
            self._refresh_history()
            self.status_line("Couldn't finish cleaning. " + self._friendly(err), "error")
            messagebox.showwarning("Couldn't finish", self._friendly(err))
            return
        if res["removed"]:  # drop the uninstalled apps from the list
            gone = set(res["packages"])
            self.apps = [a for a in self.apps if a.package not in gone]
            self.selected = None
        self._refresh_history()
        self._render_table()
        self._update_detail()
        verb = "removed" if res["removed"] else "paused"
        summary = f"Closed {res['stopped']} app(s) and {verb} {res['acted']} risky one(s)."
        receipt_path = self._save_receipt(res)
        if self.shop_mode.get():
            # Hands-off: a loud on-screen cue (+ chime) for the next phone; no modal.
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

    # --- device maintenance -------------------------------------------------

    def on_dev_refresh(self):
        if self.serial:
            self._refresh_device()

    def _refresh_device(self):
        if not self.serial:
            return

        def work():
            try:
                stats = read_device_stats(self.adb)
                self._post(self._show_device, stats)
            except Exception:
                pass
            try:
                report = read_battery_report(self.adb)
                self._post(self._show_battery_report, report)
            except Exception:
                pass

        self._run_bg(work)

    def _show_device(self, s):
        AMBER, RED_T, OK = "#b45309", "#b91c1c", INK

        def paint(key, color):
            self.dev_labels[key].config(foreground=color)

        if s["storage_total_gb"]:
            self.dev_vars["storage"].set(
                f"{s['storage_used_gb']} GB used of {s['storage_total_gb']} GB   "
                f"({s['storage_free_gb']} GB free)")
            paint("storage", RED_T if s["storage_pct"] > 95
                  else AMBER if s["storage_pct"] > 85 else OK)
        else:
            self.dev_vars["storage"].set("— couldn't read")
            paint("storage", MUTED)

        if s["ram_total_gb"]:
            self.dev_vars["ram"].set(
                f"{s['ram_used_gb']} GB used of {s['ram_total_gb']} GB   ({s['ram_pct']}%)")
            paint("ram", RED_T if s["ram_pct"] > 95 else AMBER if s["ram_pct"] > 90 else OK)
        else:
            self.dev_vars["ram"].set("— couldn't read")
            paint("ram", MUTED)

        if s["battery_temp_c"] is not None:
            self.dev_vars["temp"].set(f"{s['battery_temp_c']} °C")
            paint("temp", RED_T if s["battery_temp_c"] > 45
                  else AMBER if s["battery_temp_c"] > 40 else OK)
        else:
            self.dev_vars["temp"].set("— couldn't read")
            paint("temp", MUTED)

        self.dev_vars["battery"].set(
            f"{s['battery_level']}%" if s["battery_level"] is not None else "— couldn't read")

    def _show_battery_report(self, report):
        self.battery_report = report
        health = report["health_pct"]
        self.dev_vars["battery_health"].set(
            f"{health}% of original capacity" if health else "—")
        top = report["top_drainers"]
        self.dev_vars["top_drainer"].set(
            f"{top[0][0]} ({top[0][1]:g} mAh since last charge)" if top else "—")

    def on_clear_caches(self):
        if self.busy or not self.serial:
            return
        if not messagebox.askyesno(
                "Clear app caches",
                "Clear the temporary cache files for all apps?\n\n"
                "This frees up space and can fix misbehaving apps. It does NOT delete "
                "your photos, messages, accounts, or app data.\n\n"
                "Go ahead?", default="yes"):
            return
        self.busy = True
        self.status_line("Clearing app caches…")

        def work():
            try:
                before = read_device_stats(self.adb)["storage_free_gb"]
                clear_caches(self.adb, self.log)
                after = read_device_stats(self.adb)
                self._post(self._cache_done, after,
                           round(after["storage_free_gb"] - before, 1), None)
            except Exception as e:
                self._post(self._cache_done, None, 0, str(e))

        self._run_bg(work)

    def _cache_done(self, stats, freed, err):
        self.busy = False
        self._refresh_history()
        if err:
            self.status_line("Couldn't clear caches. " + self._friendly(err), "error")
            return
        if stats:
            self._show_device(stats)
        self.status_line(f"✅ Caches cleared. Freed about {freed} GB." if freed > 0
                         else "✅ Caches cleared.", "good")

    def on_screenshot(self):
        if self.busy or not self.serial:
            return
        self.status_line("Taking a screenshot…")

        def work():
            try:
                png = self.adb.screencap()
                self._post(self._show_screenshot, png, None)
            except Exception as e:
                self._post(self._show_screenshot, None, str(e))

        self._run_bg(work)

    def _show_screenshot(self, png, err):
        if err or not png:
            self.status_line("Couldn't take a screenshot. " + self._friendly(err or ""),
                             "error")
            return
        # Save a copy for the customer record.
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        folder = data_dir() / "screenshots"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{self.serial}_{stamp}.png"
        try:
            path.write_bytes(png)
        except Exception:
            path = None
        win = tk.Toplevel(self.root)
        win.title("Phone screen")
        win.configure(bg=BASE)
        try:
            img = tk.PhotoImage(data=png)
            while img.height() > 780 or img.width() > 460:  # shrink tall phone shots
                img = img.subsample(2, 2)
            lbl = tk.Label(win, image=img, bg=BASE)
            lbl.image = img  # keep a reference alive
            lbl.pack(padx=8, pady=8)
        except tk.TclError:
            tk.Label(win, text="(Couldn't display this image format.)",
                     bg=BASE, padx=20, pady=20).pack()
        if path:
            ttk.Label(win, text=f"Saved to {path}", style="Muted.TLabel").pack(pady=(0, 8))
        self.status_line("Screenshot captured." + (f" Saved to {path}" if path else ""),
                         "good")

    def on_reboot(self):
        if self.busy or not self.serial:
            return
        if not messagebox.askyesno(
                "Reboot phone",
                "Restart the phone now?\n\nIt will disconnect and come back in a minute.",
                default="no"):
            return
        try:
            reboot(self.adb, self.log)
            self._refresh_history()
            self.status_line("Rebooting the phone…", "good")
        except Exception as e:
            self.status_line("Couldn't reboot. " + self._friendly(str(e)), "error")

    def on_chrome_popups(self):
        """One-click fix for fake-virus site-notification spam: Chrome is the only
        no-root path for these, so this silences ALL its notifications at once."""
        if self.busy or not self.serial:
            return
        if not messagebox.askyesno(
                "Stop fake virus pop-ups",
                "This silences ALL Chrome notifications (including sites the customer "
                "wants). They can re-enable in Android Settings. Continue?", default="no"):
            return
        self.busy = True
        self.status_line("Stopping Chrome notifications…")

        def work():
            try:
                block_notifications(self.adb, "com.android.chrome", self.log)
                self._post(self._chrome_popups_done, None)
            except AdbError as e:
                self._post(self._chrome_popups_done, str(e))

        self._run_bg(work)

    def _chrome_popups_done(self, err):
        self.busy = False
        if err:
            self.status_line("Couldn't stop Chrome notifications. " + self._friendly(err),
                             "error")
        else:
            self.status_line("✅ Chrome notifications stopped.", "good")

    def on_debloat(self):
        """Find + disable preinstalled junk (carrier installers, OEM ad
        services, Facebook preload stubs) from the curated bloatware list.
        Disable-only, never uninstall -- see bloatware.py. Undo works from
        the History tab like every other logged action."""
        if self.busy or not self.serial:
            return
        self.busy = True
        self.status_line("Checking for preinstalled junk…")

        def work():
            try:
                found = find_bloat(self.adb)
                self._post(self._debloat_found, found, None)
            except Exception as e:
                self._post(self._debloat_found, None, str(e))

        self._run_bg(work)

    def _debloat_found(self, found, err):
        self.busy = False
        if err:
            self.status_line("Couldn't check for preinstalled junk. " + self._friendly(err),
                             "error")
            return
        if not found:
            self.status_line("No known preinstalled junk on this phone.", "good")
            return
        names = "\n".join("     •  " + p for p in found[:10])
        if len(found) > 10:
            names += f"\n     •  …and {len(found) - 10} more"
        if not messagebox.askyesno(
                f"Disable {len(found)} preinstalled app(s)",
                f"Disable these {len(found)} preinstalled app(s)?\n\n{names}\n\n"
                "They stop running immediately. Reversible any time from the History tab.",
                default="no"):
            return
        self.busy = True
        self.status_line(f"Disabling {len(found)} preinstalled app(s)…")

        def work():
            done = 0
            for pkg in found:
                try:
                    if debloat(self.adb, pkg, self.log):
                        done += 1
                except Exception:
                    pass
            self._post(self._debloat_done, done)

        self._run_bg(work)

    def _debloat_done(self, done):
        self.busy = False
        self.status_line(f"✅ Disabled {done} preinstalled junk app(s).", "good")

    # --- Private DNS (system-wide ad blocking) -------------------------------

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
                self._post(self._after_dns, None, "Ad blocking is on.")
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
                self._post(self._after_dns, None, "Ad blocking is off.")
            except Exception as e:
                self._post(self._after_dns, self._friendly(str(e)))

        self._run_bg(work)

    def _after_dns(self, err, ok_msg=None):
        self._refresh_history()
        if err:
            self.status_line("Couldn't change ad blocking. " + err, "error")
        elif ok_msg:
            self.status_line(ok_msg, "good")
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
        cancel = ttk.Button(row, text="Cancel", command=win.destroy)
        cancel.pack(side="left", padx=6)
        ttk.Button(row, text="Yes, stop all",
                   command=lambda: (result.update(go=True), win.destroy())).pack(
            side="left", padx=6)
        win.bind("<Escape>", lambda e: win.destroy())   # Esc = cancel
        cancel.focus_set()                              # safe default on a disruptive action
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

    FRIENDLY_ACTION = {
        "pause": "Paused", "resume": "Resumed", "uninstall": "Uninstalled",
        "force-stop": "Closed", "block-popup": "Blocked pop-ups",
        "clear-cache": "Cleared caches", "fix-role": "Restored defaults",
        "block-notifications": "Stopped notifications",
        "debloat": "Disabled preinstalled app",
        "restrict-data": "Blocked background data",
    }

    def _refresh_history(self):
        self.hist.delete(*self.hist.get_children())
        self.hist.tag_configure("failed", foreground=RED)
        for i, e in enumerate(self.log.recent()):
            act = e["action"]
            label = (self.FRIENDLY_ACTION.get(act)
                     or ("Undid " + self.FRIENDLY_ACTION.get(act[5:], act[5:]).lower()
                         if act.startswith("undo:") else act))
            tags = ("failed",) if e.get("result") == "failed" else ()
            self.hist.insert("", "end", iid=str(i), tags=tags,
                             values=(e["time"], e["package"], label, e["result"]))

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

    def status_line(self, text, kind="info"):
        self.statusbar.config(text=text, foreground=STATUS_FG.get(kind, INK))

    def _friendly(self, err):
        """Turn a raw ADB error into one plain sentence with a next step."""
        e = (err or "").lower()
        if any(k in e for k in ("offline", "no devices", "not found", "disconnect",
                                "closed", "device '", "cannot connect", "device offline")):
            return ("The phone disconnected. Re-plug the USB cable, wait for the green "
                    "light at the top, then try again.")
        if "unauthorized" in e:
            return "Tap “Allow” on the phone screen, then try again."
        return "Something went wrong. Re-plug the phone and press 🔄 Rescan."

    def _toggle_shop(self):
        """Confirm hands-off cleaning once when Shop mode is switched on."""
        self._save_settings()
        if not self.shop_mode.get():
            return
        act = ("uninstalled (restorable from the History tab)" if self.uninstall_mode.get()
               else "paused (fully reversible)")
        if not messagebox.askyesno(
                "Turn on Shop mode?",
                "Shop mode cleans each phone automatically the moment it's scanned, "
                "with no further prompts.\n\n"
                f"Risky apps will be {act}.\n\n"
                "Turn it on?", default="yes"):
            self.shop_mode.set(False)
            self._save_settings()

    def _sync_clean_label(self):
        """Keep the CLEAN button honest about what it will do."""
        self.clean_btn.config(text="🗑  CLEAN & REMOVE" if self.uninstall_mode.get()
                              else "✨  CLEAN MY PHONE")

    def _on_close(self):
        self.alive = False
        self.root.destroy()
