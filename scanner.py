"""App inventory + suspicion scoring (BUILD_PLAN 4.2).

Pure parse/score functions (tested against fixtures) plus a thin
`build_inventory` that drives an Adb object. All device I/O lives in adb.py.
"""

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from device import parse_data_use, parse_usage_minutes
from protected import (
    extend_blocklist, is_blocked, is_protected, is_spoof, looks_like_junk,
    reset_blocklist,
)
from stalkerware import is_stalkerware

# --- Scoring knobs: tune here. (BUILD_PLAN 4.2) -----------------------------
WEIGHTS = {
    "overlay": 40,             # allowed to draw over other apps (the popup mechanism)
    "sideloaded": 25,          # installer is null or not a known store
    "active_accessibility": 25,  # accessibility service is switched ON (controls phone)
    "hidden": 20,              # installed but has no icon in the app drawer
    "device_admin": 20,        # active device administrator
    "recent_install": 15,      # first installed within RECENT_DAYS
    "role_hijack": 15,         # took over a system default (home/browser/sms/dialer)
    "request_install": 10,     # holds REQUEST_INSTALL_PACKAGES
    "accessibility": 10,       # holds a BIND_ACCESSIBILITY_SERVICE grant (declared only)
    "sensitive_data": 10,      # can read SMS / call log / contacts
    "random_name": 10,         # package name has a random-looking segment
    "nuisance": 30,            # junk cleaner/booster/optimizer or fake-app name
    "notif_spam": 10,          # floods the notification shade
    "boot_receiver": 10,       # restarts itself on every reboot (RECEIVE_BOOT_COMPLETED)
    "notif_listener": 25,      # notification listener is switched ON (reads every notification)
}
REASONS = {
    "overlay": "Can draw pop-ups over other apps",
    "sideloaded": "Installed from outside an app store (sideloaded)",
    "active_accessibility": "Accessibility control is switched ON (can tap/read the screen)",
    "hidden": "Hidden — no icon in the app drawer",
    "device_admin": "Is a device administrator",
    "recent_install": "Installed in the last 30 days",
    "role_hijack": "Took over a system default",
    "request_install": "Can install other apps",
    "accessibility": "Uses accessibility access",
    "sensitive_data": "Can read your texts, calls, or contacts",
    "random_name": "Has a random-looking package name",
    "nuisance": "Looks like a junk cleaner/booster/optimizer app",
    "notif_spam": "Floods the phone with notifications",
    "boot_receiver": "Restarts itself when the phone reboots",
    "notif_listener": "Can read every notification (texts and bank codes included)",
}
BLOCKED_REASON = "On the known-bad app blocklist"

# Android role -> plain-English name (BUILD_PLAN risk mgmt). cmd role holders <role>.
ROLES = {
    "android.app.role.HOME": "home screen",
    "android.app.role.BROWSER": "browser",
    "android.app.role.SMS": "text messages",
    "android.app.role.DIALER": "phone dialer",
}
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

# Sensitive permissions shown in the detail pane (BUILD_PLAN 4.2 / risk mgmt).
SENSITIVE_PERMS = [
    ("SEND_SMS", "Send text messages"),
    ("READ_SMS", "Read your text messages"),
    ("RECEIVE_SMS", "Read incoming texts"),
    ("READ_CALL_LOG", "Read your call history"),
    ("CALL_PHONE", "Make phone calls"),
    ("READ_CONTACTS", "Read your contacts"),
    ("ACCESS_FINE_LOCATION", "Track your location"),
    ("RECORD_AUDIO", "Use the microphone"),
    ("CAMERA", "Use the camera"),
    ("READ_PHONE_STATE", "Read phone info (number, IMEI)"),
    ("MANAGE_EXTERNAL_STORAGE", "Access all your files"),
]
# Holding any of these counts as sensitive personal-data access.
_PERSONAL_DATA = ("SEND_SMS", "READ_SMS", "RECEIVE_SMS", "READ_CALL_LOG", "READ_CONTACTS")
SPOOF_REASON = "Pretends to be a system app"
STALKER_REASON = "Hidden tracking app (stalkerware)"
HIGH_THRESHOLD = 55
MEDIUM_THRESHOLD = 30
RECENT_DAYS = 30
NOISY_THRESHOLD = 5        # active notifications at scan time -> "notif_spam" signal
# ---------------------------------------------------------------------------


@dataclass
class App:
    package: str
    label: str = ""
    installer: str | None = None
    is_system: bool = False           # scanned apps are all third-party
    enabled: bool = True              # False => paused/disabled
    overlay: bool = False
    device_admin: bool = False
    admin_component: str | None = None
    first_install: datetime | None = None
    request_install: bool = False
    accessibility: bool = False
    boot_receiver: bool = False       # holds RECEIVE_BOOT_COMPLETED
    hidden: bool = False              # no launcher icon on the phone
    sensitive_data: bool = False      # can read SMS / calls / contacts
    sensitive_perms: list = field(default_factory=list)  # human-readable perm labels
    active_accessibility: bool = False   # accessibility service is enabled (not just declared)
    hijacked_roles: list = field(default_factory=list)   # role names it holds (home/browser/…)
    stopped: bool = False             # transient: force-stopped this session
    notif_count: int = 0              # active notifications at scan time
    notif_titles: list = field(default_factory=list)  # titles it's showing right now
    notif_listener: bool = False      # notification-listener access is switched ON
    data_mb: int = 0                  # background+foreground data used, MB (dumpsys netstats)
    uid: int = 0                      # app uid, e.g. 10231 (0 if unknown)
    used_min: int = 0                 # foreground usage minutes (dumpsys usagestats)
    score: int = 0
    risk: str = "Low"
    reasons: list = field(default_factory=list)
    play: dict | None = None          # Google Play lookup; filled by the GUI post-scan

    @property
    def protected(self):
        return is_protected(self.package, self.installer, self.is_system)

    @property
    def source(self):
        if self.installer in (None, "", "null"):
            return "Sideloaded"
        return self.installer

    @property
    def status(self):
        if not self.enabled:
            return "Paused"
        return "Stopped" if self.stopped else "Running"


# Names a customer actually knows, for packages whose id guesses wrong
# ("Katana" is Facebook) plus the system processes that top the hog lists.
# ponytail: curated dict, not manifest parsing — extend as odd ones show up.
KNOWN_LABELS = {
    "com.facebook.katana": "Facebook",
    "com.facebook.orca": "Messenger",
    "com.facebook.lite": "Facebook Lite",
    "com.instagram.android": "Instagram",
    "com.zhiliaoapp.musically": "TikTok",
    "com.ss.android.ugc.trill": "TikTok",
    "com.whatsapp": "WhatsApp",
    "com.snapchat.android": "Snapchat",
    "com.twitter.android": "X (Twitter)",
    "com.aliexpresshd": "AliExpress",
    "com.sec.android.app.shealth": "Samsung Health",
    "com.sec.android.app.sbrowser": "Samsung Internet",
    "com.sec.android.app.music": "Samsung Music",
    "com.sec.android.app.launcher": "Samsung home screen",
    "com.sec.android.sdhms": "Samsung device care",
    "com.samsung.android.wallpaper.live": "Samsung live wallpaper",
    "com.samsung.android.smartsuggestions": "Samsung smart suggestions",
    "com.samsung.android.offline.languagemodel": "Samsung AI language model",
    "com.android.systemui": "Android interface (System UI)",
    "com.android.chrome": "Chrome",
    "com.android.vending": "Google Play Store",
    "com.google.android.gms": "Google Play services",
    "com.google.android.googlequicksearchbox": "Google Search",
    "com.google.android.youtube": "YouTube",
    "com.google.android.apps.photos": "Google Photos",
    "com.google.android.aicore": "Android AI Core",
}


def prettify_label(package):
    """`com.foo.flashlight` -> `Flashlight (com.foo.flashlight)`."""
    known = KNOWN_LABELS.get(package)
    if known:
        return f"{known} ({package})"
    seg = package.split(".")[-1] or package
    return f"{seg[:1].upper()}{seg[1:]} ({package})"


# --- Parsers ----------------------------------------------------------------

def parse_third_party(output):
    """`pm list packages -3 -i` -> {package: installer or None}."""
    result = {}
    for line in output.splitlines():
        line = line.strip()
        if not line.startswith("package:"):
            continue
        m = re.match(r"package:(\S+)(?:\s+installer=(\S+))?", line)
        if not m:
            continue
        pkg = m.group(1)
        inst = m.group(2)
        if inst in (None, "null"):
            inst = None
        result[pkg] = inst
    return result


def parse_disabled(output):
    """`pm list packages -d` -> set of disabled packages."""
    return {
        line.strip()[len("package:"):]
        for line in output.splitlines()
        if line.strip().startswith("package:")
    }


def parse_overlay_allowed(output):
    """`appops query-op SYSTEM_ALERT_WINDOW allow` -> set of packages.

    Handles both the bare-list form and the `Package com.x:` grouped form.
    """
    pkgs = set()
    for line in output.splitlines():
        line = line.strip()
        m = re.match(r"(?:Package\s+)?([a-zA-Z][\w.]*\.[\w.]+):?$", line)
        if m and "." in m.group(1):
            pkgs.add(m.group(1))
    return pkgs


def parse_device_admins(output):
    """`dumpsys device_policy` -> {package: 'package/component'}."""
    admins = {}
    for m in re.finditer(r"ComponentInfo\{([\w.]+)/([\w.$]+)\}", output):
        pkg, comp = m.group(1), m.group(2)
        admins[pkg] = f"{pkg}/{comp}"
    return admins


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


_INSTALL_RE = re.compile(r"firstInstallTime=(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")


def parse_first_install(dump_text):
    """Pull firstInstallTime from a `dumpsys package <pkg>` dump."""
    m = _INSTALL_RE.search(dump_text)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def parse_perms(dump_text):
    """Return the permission flags of interest present in a package dump."""
    sensitive = [label for key, label in SENSITIVE_PERMS
                 if f"permission.{key}" in dump_text]
    return {
        "request_install": "REQUEST_INSTALL_PACKAGES" in dump_text,
        "accessibility": "BIND_ACCESSIBILITY_SERVICE" in dump_text,
        "overlay_perm": "SYSTEM_ALERT_WINDOW" in dump_text,  # old-Android fallback
        "boot_receiver": "RECEIVE_BOOT_COMPLETED" in dump_text,
        "sensitive_perms": sensitive,
        "sensitive_data": any(f"permission.{k}" in dump_text for k in _PERSONAL_DATA),
    }


def parse_launcher_packages(output):
    """`cmd package query-activities … LAUNCHER` -> set of packages with an icon."""
    return {m.group(1) for m in re.finditer(r"([\w.]+)/[\w.$]+", output)}


def parse_enabled_accessibility(output):
    """`settings get secure enabled_accessibility_services` -> set of packages.

    Value is 'pkg1/svc1:pkg2/svc2' or 'null'/empty when none are enabled.
    """
    out = (output or "").strip()
    if not out or out == "null":
        return set()
    return {seg.split("/")[0] for seg in out.split(":") if "/" in seg}


def parse_role_holders(output):
    """`cmd role get-role-holders <role>` -> list of holder packages.
    AOSP prints multiple holders ';'-joined on one line, so split on that too."""
    return [pkg for ln in (output or "").splitlines() if " " not in ln.strip()
            for pkg in ln.strip().split(";") if pkg and "." in pkg]


def parse_pkg_uids(output):
    """`pm list packages -3 -U` -> {package: uid} (raw int, e.g. 10231)."""
    return {m.group(1): int(m.group(2))
            for m in re.finditer(r"package:(\S+)\s+uid:(\d+)", output or "")}


def parse_notification_counts(output):
    """`dumpsys notification --noredact` -> {package: active notification count}.

    ponytail: [^)\n]* not [^)]* -- pkg= is always on the NotificationRecord( line,
    and real dumps don't reliably close the paren on that same line, so an
    unbounded [^)]* backtracks across the whole remaining dump on the first
    record and only captures the last pkg= in the file.
    """
    counts = {}
    for m in re.finditer(r"NotificationRecord\([^)\n]*\bpkg=([\w.]+)", output or ""):
        counts[m.group(1)] = counts.get(m.group(1), 0) + 1
    return counts


def parse_notification_titles(output):
    """`dumpsys notification --noredact` -> {package: [distinct titles]}.

    Shows WHAT each app is pushing, so the app posting the ads the customer
    complains about can be identified by its own words.
    """
    titles = {}
    pkg = None
    for line in (output or "").splitlines():
        line = line.strip()
        m = re.match(r"NotificationRecord\([^)]*\bpkg=([\w.]+)", line)
        if m:
            pkg = m.group(1)
            continue
        m = re.match(r"android\.title=\w*String \((.+)\)$", line)
        if m and pkg and m.group(1) not in titles.setdefault(pkg, []):
            titles[pkg].append(m.group(1))
    return titles


# --- Scoring ----------------------------------------------------------------

def looks_random(package):
    """Naive gibberish check on package segments.
    ponytail: heuristic, tune WEIGHTS/here if it mislabels; not a classifier.
    """
    for seg in package.split("."):
        if len(seg) < 8 or not seg.isalnum():
            continue
        letters = [c for c in seg.lower() if c.isalpha()]
        if any(c.isdigit() for c in seg) and letters:
            return True
        if letters:
            vowels = sum(c in "aeiou" for c in letters)
            if vowels / len(letters) < 0.2:  # ponytail: consonant-heavy = gibberish
                return True
    return False


def score_app(app, now):
    """Set app.score/risk/reasons from its signals. `now` is injected for tests."""
    if app.protected:
        # Genuine system/OEM app: never risky, never actioned -- don't score it.
        # (Real preloads have a null installer, which otherwise reads as
        # 'sideloaded' and produced dangerous false positives on real hardware.)
        app.score, app.risk, app.reasons = 0, "Low", []
        return app
    signals = {
        "overlay": app.overlay,
        "sideloaded": app.installer is None,
        "hidden": app.hidden,
        "recent_install": bool(
            app.first_install and now - app.first_install <= timedelta(days=RECENT_DAYS)
        ),
        "device_admin": app.device_admin,
        "request_install": app.request_install,
        "accessibility": app.accessibility,
        "sensitive_data": app.sensitive_data,
        "random_name": looks_random(app.package),
        "active_accessibility": app.active_accessibility,
        "role_hijack": bool(app.hijacked_roles),
        "nuisance": looks_like_junk(app.package, app.label),
        "notif_spam": app.notif_count >= NOISY_THRESHOLD,
        "boot_receiver": app.boot_receiver,
        "notif_listener": app.notif_listener,
    }
    app.score = sum(WEIGHTS[k] for k, on in signals.items() if on)
    app.reasons = [REASONS[k] for k in WEIGHTS if signals[k]]
    if app.hijacked_roles:  # name the specific defaults it seized
        detail = "Took over a system default (" + ", ".join(app.hijacked_roles) + ")"
        app.reasons = [detail if r == REASONS["role_hijack"] else r for r in app.reasons]

    spoof = is_spoof(app.package, app.installer, app.is_system)
    if spoof:
        app.reasons.insert(0, SPOOF_REASON)

    blocked = is_blocked(app.package)
    if blocked:
        app.reasons.insert(0, BLOCKED_REASON)

    stalker = is_stalkerware(app.package)
    if stalker:
        app.reasons.insert(0, STALKER_REASON)

    if spoof or blocked or stalker or app.score >= HIGH_THRESHOLD:
        app.risk = "HIGH"
    elif app.score >= MEDIUM_THRESHOLD:
        app.risk = "Medium"
    else:
        app.risk = "Low"
    return app


# --- Orchestration (thin; device I/O via adb) -------------------------------

def _safe(fn, default=""):
    try:
        return fn()
    except Exception:
        return default


def _load_user_blocklist():
    """Rebuild the blocklist as seed + adcleaner_data/blocklist.txt (user-editable,
    one id per line), so edits AND deletions take effect on the next scan.
    Silent if the file is absent or unreadable -- the bundled seed still applies.
    utf-8-sig: Windows editors (and PowerShell redirects) love BOMs.
    """
    from adb import data_dir  # local import: keep the parse/score core adb-free
    reset_blocklist()
    path = data_dir() / "blocklist.txt"
    if path.exists():
        try:
            extend_blocklist(path.read_text(encoding="utf-8-sig").splitlines())
        except (OSError, UnicodeDecodeError):
            pass


def build_inventory(adb, progress=None, now=None):
    """Scan the connected device and return scored Apps, highest risk first."""
    now = now or datetime.now()
    _load_user_blocklist()
    installers = parse_third_party(_safe(lambda: adb.shell_text(
        ["pm", "list", "packages", "-3", "-i"])))
    disabled = parse_disabled(_safe(lambda: adb.shell_text(
        ["pm", "list", "packages", "-d"])))
    overlay_allowed = parse_overlay_allowed(_safe(lambda: adb.shell_text(
        ["appops", "query-op", "SYSTEM_ALERT_WINDOW", "allow"])))
    admins = parse_device_admins(_safe(lambda: adb.shell_text(
        ["dumpsys", "device_policy"])))
    launchers = parse_launcher_packages(_safe(lambda: adb.shell_text(
        ["cmd", "package", "query-activities", "--brief",
         "-a", "android.intent.action.MAIN",
         "-c", "android.intent.category.LAUNCHER"])))
    have_launchers = bool(launchers)  # skip hidden-detection if the query failed
    a11y_on = parse_enabled_accessibility(_safe(lambda: adb.shell_text(
        ["settings", "get", "secure", "enabled_accessibility_services"])))
    role_owner = {}  # package -> [role names it holds]
    for role, friendly in ROLES.items():
        for pkg in parse_role_holders(_safe(lambda: adb.shell_text(
                ["cmd", "role", "get-role-holders", role]))):
            role_owner.setdefault(pkg, []).append(friendly)
    notif_dump = _safe(lambda: adb.shell_text(
        ["dumpsys", "notification", "--noredact"]))
    notif = parse_notification_counts(notif_dump)
    notif_titles = parse_notification_titles(notif_dump)
    # Same 'pkg/svc:pkg/svc' format as accessibility services -- reuse the parser.
    listeners = parse_enabled_accessibility(_safe(lambda: adb.shell_text(
        ["settings", "get", "secure", "enabled_notification_listeners"])))
    pkg_uids = parse_pkg_uids(_safe(lambda: adb.shell_text(
        ["pm", "list", "packages", "-3", "-U"])))
    data_use = parse_data_use(_safe(lambda: adb.shell_text(
        ["dumpsys", "netstats"])))
    usage = parse_usage_minutes(_safe(lambda: adb.shell_text(
        ["dumpsys", "usagestats"])))

    apps = []
    packages = sorted(installers)
    total = len(packages)
    for i, pkg in enumerate(packages, 1):
        if progress:
            progress(i, total, pkg)
        dump = _safe(lambda: adb.shell_text(["dumpsys", "package", pkg]))
        perms = parse_perms(dump)
        uid = pkg_uids.get(pkg, 0)
        app = App(
            package=pkg,
            label=prettify_label(pkg),
            installer=installers[pkg],
            enabled=pkg not in disabled,
            overlay=pkg in overlay_allowed or (
                not overlay_allowed and perms["overlay_perm"]),
            device_admin=pkg in admins,
            admin_component=admins.get(pkg),
            first_install=parse_first_install(dump),
            request_install=perms["request_install"],
            accessibility=perms["accessibility"],
            boot_receiver=perms["boot_receiver"],
            hidden=have_launchers and pkg not in launchers,
            sensitive_data=perms["sensitive_data"],
            sensitive_perms=perms["sensitive_perms"],
            active_accessibility=pkg in a11y_on,
            hijacked_roles=role_owner.get(pkg, []),
            notif_count=notif.get(pkg, 0),
            notif_titles=notif_titles.get(pkg, []),
            notif_listener=pkg in listeners,
            uid=uid,
            # uid 0 means "unresolved" here (the -U lookup failed for this
            # package), not root -- attributing root's netstats bucket to
            # every unresolved app would inflate all of their data_mb.
            data_mb=(data_use.get(uid, 0) // (1024 * 1024)) if uid else 0,
            used_min=usage.get(pkg, 0),
        )
        score_app(app, now)
        apps.append(app)

    apps.sort(key=lambda a: a.score, reverse=True)
    return apps


def demo():
    now = datetime(2024, 3, 1)
    # Sideloaded overlay adware installed yesterday -> HIGH.
    adware = App(package="com.random.freegift", installer=None, overlay=True,
                 first_install=datetime(2024, 2, 28))
    score_app(adware, now)
    assert adware.risk == "HIGH", adware.score
    assert REASONS["overlay"] in adware.reasons
    assert REASONS["sideloaded"] in adware.reasons

    # Clean Play Store app -> Low.
    clean = App(package="com.spotify.music", installer="com.android.vending",
                first_install=datetime(2020, 1, 1))
    score_app(clean, now)
    assert clean.risk == "Low", clean.score

    # First-party name from the generic sideload installer -> forced HIGH + note.
    spoof = App(package="com.google.android.fakecore",
                installer="com.google.android.packageinstaller",
                first_install=datetime(2020, 1, 1))
    score_app(spoof, now)
    assert spoof.risk == "HIGH"
    assert SPOOF_REASON in spoof.reasons

    # Real Samsung preload (first-party name, null installer) -> protected, Low.
    preload = App(package="com.sec.android.app.kidshome", installer=None,
                  first_install=datetime(2020, 1, 1))
    score_app(preload, now)
    assert preload.protected and preload.risk == "Low" and not preload.reasons

    # Play-Store cleaner holding no dangerous perms -> nuisance signal -> Medium.
    cleaner = App(package="com.phone.cleaner.shineapps", installer="com.android.vending",
                  label="cleaner", first_install=datetime(2020, 1, 1))
    score_app(cleaner, now)
    assert cleaner.risk == "Medium", cleaner.score
    assert REASONS["nuisance"] in cleaner.reasons

    # Junk app wearing a system-style name -> NOT protected, flagged.
    fake_sys = App(package="com.sec.reclean", installer="com.android.vending",
                   first_install=datetime(2020, 1, 1))
    score_app(fake_sys, now)
    assert not fake_sys.protected and fake_sys.risk != "Low"

    # Blocklisted id -> forced HIGH regardless of other signals.
    blocked = App(package="com.cleanmaster.mguard", installer="com.android.vending",
                  first_install=datetime(2020, 1, 1))
    score_app(blocked, now)
    assert blocked.risk == "HIGH" and BLOCKED_REASON in blocked.reasons

    owners = parse_owners("Device Owner:\n  admin=ComponentInfo{com.mdm.x/.A}\n")
    assert owners == {"device": "com.mdm.x", "profile": None}

    print("scanner.py demo OK")


if __name__ == "__main__":
    demo()
