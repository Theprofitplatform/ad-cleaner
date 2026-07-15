"""Device actions + undo log (BUILD_PLAN 4.3, 4.4, 4.6).

Every action: guard against protected packages -> execute -> verify by
re-querying device state -> append to the append-only undo log.
"""

import json
import re
from datetime import datetime
from pathlib import Path

import playstore
import scanner
from adb import AdbError, data_dir
from protected import is_protected
from scanner import REASONS, STOCK_ROLE_HOLDERS, parse_disabled

UNDOABLE = {"pause", "uninstall", "block-popup", "fix-role", "block-notifications", "debloat",
            "restrict-data"}


class ProtectedAppError(Exception):
    """Raised on any attempt to act on a protected package (BUILD_PLAN 4.5, AC#7)."""


def _guard(app):
    if is_protected(app.package, app.installer, app.is_system):
        raise ProtectedAppError(f"{app.package} is a protected system app")


# --- Undo log ---------------------------------------------------------------

class ActionLog:
    def __init__(self, path=None):
        self.path = Path(path) if path else data_dir() / "action_log.json"
        self.entries = self._load()

    def _load(self):
        if self.path.exists():
            try:
                return json.loads(self.path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                return []
        return []

    def _save(self):
        self.path.write_text(json.dumps(self.entries, indent=2), encoding="utf-8")

    def append(self, serial, package, action, previous, command, result):
        entry = {
            "time": datetime.now().isoformat(timespec="seconds"),
            "serial": serial,
            "package": package,
            "action": action,
            "previous": previous,
            "command": " ".join(command) if isinstance(command, list) else command,
            "result": result,
        }
        self.entries.append(entry)
        self._save()
        return entry

    def recent(self):
        """Newest first."""
        return list(reversed(self.entries))


# --- Verification helpers ---------------------------------------------------

def _is_disabled(adb, package):
    return package in parse_disabled(adb.shell_text(["pm", "list", "packages", "-d"]))


def _is_installed(adb, package):
    out = adb.shell_text(["pm", "list", "packages"])
    return any(line.strip() == "package:" + package for line in out.splitlines())


# --- Actions ----------------------------------------------------------------

def pause(adb, app, log):
    """Freeze: `pm disable-user --user 0`. Instant, reversible."""
    _guard(app)
    cmd = ["pm", "disable-user", "--user", "0", app.package]
    adb.shell_text(cmd)
    ok = _is_disabled(adb, app.package)
    if ok:
        app.enabled = False
    log.append(adb.serial, app.package, "pause", "enabled", cmd, "ok" if ok else "failed")
    return ok


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


def resume(adb, app, log):
    """Un-freeze: `pm enable` (falls back to non --user form)."""
    cmd = ["pm", "enable", "--user", "0", app.package]
    try:
        adb.shell_text(cmd)
    except AdbError:
        cmd = ["pm", "enable", app.package]
        adb.shell_text(cmd)
    ok = not _is_disabled(adb, app.package)
    if ok:
        app.enabled = True
    log.append(adb.serial, app.package, "resume", "paused", cmd, "ok" if ok else "failed")
    return ok


def uninstall(adb, app, log):
    """Remove for user 0. Auto-neutralises accessibility + device-admin first."""
    _guard(app)
    if getattr(app, "active_accessibility", False):
        try:
            disable_accessibility(adb, app.package, log)  # stop it re-granting/blocking
        except AdbError:
            pass
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
               "ok" if ok else "failed")
    return ok


def stop_all(adb, apps, log, block_popups=False, progress=None):
    """Force-stop every enabled, non-protected third-party app (BUILD_PLAN 4.3).

    Returns (stopped_count, attempted_count).
    """
    targets = [a for a in apps if a.enabled and not a.protected]
    stopped = 0
    for i, app in enumerate(targets, 1):
        try:
            adb.shell_text(["am", "force-stop", app.package])
            stopped += 1
            app.stopped = True
            log.append(adb.serial, app.package, "force-stop", "running",
                       ["am", "force-stop", app.package], "ok")
        except AdbError:
            log.append(adb.serial, app.package, "force-stop", "running",
                       ["am", "force-stop", app.package], "failed")
        if progress:
            progress(i, len(targets), app.package)

    if block_popups:
        for app in apps:
            if app.overlay and not app.protected:
                cmd = ["appops", "set", app.package, "SYSTEM_ALERT_WINDOW", "deny"]
                try:
                    adb.shell_text(cmd)
                    app.overlay = False
                    log.append(adb.serial, app.package, "block-popup", "allow", cmd, "ok")
                except AdbError:
                    log.append(adb.serial, app.package, "block-popup", "allow", cmd, "failed")
    return stopped, len(targets)


SUSPICIOUS = ("HIGH", "Medium")

# The nuisance signal is a NAME heuristic; on its own it means "worth a look",
# not "act" -- real apps match it (AVG's package id is literally
# com.antivirus). score_app encodes "name was the only signal" as exactly this
# reasons list. ponytail: list comparison, not a new App flag.
_NUISANCE_ONLY = [REASONS["nuisance"]]


def will_clean(app):
    """True if one-click clean (big green button / Shop mode) may auto-act on
    this app: suspicious and not protected -- EXCEPT a Medium whose only
    *scored* evidence is a junk-looking name. That stays flagged in the Apps
    tab for a human to bulk-pause/uninstall deliberately, but is never cleaned
    unattended. The Play-lookup reason (not-listed) is display-only and must
    not defeat the fence, so the check is membership-based rather than an
    exact-list comparison: a Medium fences whenever its reasons are a subset
    of {nuisance, not-listed} and the nuisance reason is present.
    """
    if app.risk not in SUSPICIOUS or app.protected:
        return False
    nuisance_fenced = (
        app.risk == "Medium"
        and _NUISANCE_ONLY[0] in app.reasons
        and not (set(app.reasons) - {_NUISANCE_ONLY[0], playstore.NOT_LISTED_REASON})
    )
    if nuisance_fenced:
        return False
    # Victim safety: auto-pausing a hidden tracking app can alert the abuser
    # who installed it. The detail pane tells the tech to talk to the
    # customer privately first, so stalkerware is never cleaned unattended.
    if scanner.STALKER_REASON in app.reasons:
        return False
    return True


def clean_risky(adb, apps, log, progress=None, remove=False):
    """One-click clean (used by the big green button / Shop mode).

    Closes every downloaded app and blocks pop-up permissions on all of them,
    then acts on every suspicious (HIGH or Medium) non-protected app -- Medium
    catches the Play-Store "cleaner/booster" pop-up apps that aren't sideloaded.
    The one exception: nuisance-name-only Mediums are skipped (see will_clean).

    remove=False -> PAUSE the apps (reversible, nothing deleted).
    remove=True  -> UNINSTALL them (restorable via History / install-existing).

    Returns {'stopped', 'acted', 'removed': bool, 'packages': [acted-on pkgs], 'popups_blocked': int}.
    """
    overlay_targets = [a for a in apps if a.overlay and not a.protected]
    stopped, _ = stop_all(adb, apps, log, block_popups=True, progress=progress)
    # stop_all clears .overlay only when the deny actually succeeded, so this
    # counts confirmed blocks, not attempts.
    popups = sum(1 for a in overlay_targets if not a.overlay)
    acted = []
    for app in apps:
        if not will_clean(app):
            continue
        try:
            if remove:
                if uninstall(adb, app, log):
                    acted.append(app.package)
            elif app.enabled and pause(adb, app, log):
                acted.append(app.package)
        except (ProtectedAppError, AdbError):
            pass
    return {"stopped": stopped, "acted": len(acted), "removed": remove, "packages": acted, "popups_blocked": popups}


def disable_accessibility(adb, package, log=None):
    """Switch OFF an app's accessibility service so it can't block its own removal.

    Reads the enabled list, drops the offender's entries, writes the rest back
    (shell holds WRITE_SECURE_SETTINGS — no root). Returns True on success.
    """
    cur = adb.shell_text(["settings", "get", "secure", "enabled_accessibility_services"])
    kept = [s for s in (cur or "").strip().split(":")
            if s and s != "null" and not s.startswith(package + "/")]
    value = ":".join(kept) if kept else "null"
    adb.shell_text(["settings", "put", "secure", "enabled_accessibility_services", value])
    if not kept:
        adb.shell_text(["settings", "put", "secure", "accessibility_enabled", "0"])
    if log is not None:
        log.append(adb.serial, package, "disable-accessibility", "on",
                   ["settings", "put", "secure", "enabled_accessibility_services"], "ok")
    return True


def reset_app_data(adb, app, log):
    """Wipe an app's data (`pm clear`) — fixes hijacked browser/launcher without removing it."""
    _guard(app)
    cmd = ["pm", "clear", "--user", "0", app.package]
    adb.shell_text(cmd)
    log.append(adb.serial, app.package, "reset-data", app.status, cmd, "ok")
    return True


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


def restrict_background(adb, package, uid, log):
    """Block the app's background mobile data (same as the per-app 'Background
    data' toggle -- Wi-Fi unaffected). Refuses non-app uids; otherwise safe + reversible."""
    if uid < 10000:
        raise ProtectedAppError(f"refusing to restrict uid {uid} (not an app uid)")
    cmd = ["cmd", "netpolicy", "add", "restrict-background-blacklist", str(uid)]
    adb.shell_text(cmd)
    log.append(adb.serial, package, "restrict-data", str(uid), cmd, "ok")
    return True


def backup_apk(adb, app, dest_dir):
    """Pull the app's APK(s) to dest_dir before removal. Returns saved file paths."""
    out = adb.shell_text(["pm", "path", app.package])
    remotes = [ln.strip()[len("package:"):] for ln in out.splitlines()
               if ln.strip().startswith("package:")]
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    saved = []
    for i, remote in enumerate(remotes):
        name = app.package + (".apk" if i == 0 else f"-split{i}.apk")
        local = str(dest / name)
        adb.pull(remote, local)
        saved.append(local)
    return saved


def reboot(adb, log=None):
    adb.reboot()
    if log is not None:
        log.append(adb.serial, "(device)", "reboot", "-", ["reboot"], "ok")
    return True


def clear_caches(adb, log=None):
    """Free space by trimming every app's cache (safe: no user data touched).

    `pm trim-caches <huge>` tells Android to purge cached files across all apps.
    Returns True on success.
    """
    adb.shell_text(["pm", "trim-caches", "9999999999999"], timeout=60)
    if log is not None:
        log.append(adb.serial, "(all apps)", "clear-cache", "-",
                   ["pm", "trim-caches"], "ok")
    return True


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
    return mode, (host if mode == "hostname" else "")


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


# --- Undo -------------------------------------------------------------------

def can_undo(entry):
    return entry.get("action") in UNDOABLE


def undo(adb, entry, log):
    """Reverse a logged action. Returns True on success."""
    action, pkg = entry["action"], entry["package"]
    if action == "pause":
        cmd = ["pm", "enable", "--user", "0", pkg]
        try:
            adb.shell_text(cmd)
        except AdbError:
            cmd = ["pm", "enable", pkg]
            adb.shell_text(cmd)
    elif action == "debloat":
        cmd = ["pm", "enable", "--user", "0", pkg]
        try:
            adb.shell_text(cmd)
        except AdbError:
            cmd = ["pm", "enable", pkg]
            adb.shell_text(cmd)
    elif action == "uninstall":
        cmd = ["cmd", "package", "install-existing", pkg]
        adb.shell_text(cmd)
    elif action == "block-popup":
        cmd = ["appops", "set", pkg, "SYSTEM_ALERT_WINDOW", "allow"]
        adb.shell_text(cmd)
    elif action == "fix-role":
        role_id = entry["command"].split()[5]
        cmd = ["cmd", "role", "add-role-holder", "--user", "0", role_id, entry["previous"]]
        adb.shell_text(cmd)
    elif action == "block-notifications":
        if "appops" in entry["command"]:
            cmd = ["appops", "set", pkg, "POST_NOTIFICATION", "allow"]
        else:
            cmd = ["pm", "grant", pkg, "android.permission.POST_NOTIFICATIONS"]
        adb.shell_text(cmd)
    elif action == "restrict-data":
        cmd = ["cmd", "netpolicy", "remove", "restrict-background-blacklist", entry["previous"]]
        adb.shell_text(cmd)
    else:
        raise AdbError("This action can't be undone.")
    log.append(adb.serial, pkg, "undo:" + action, entry.get("result"), cmd, "ok")
    return True


def demo():
    from scanner import App

    class FakeAdb:
        serial = "TEST"

        def __init__(self):
            self.disabled = set()
            self.installed = {"com.random.adware", "com.google.android.gms"}

        def shell_text(self, args, timeout=10):
            if args[:3] == ["pm", "disable-user", "--user"]:
                self.disabled.add(args[-1]); return "disabled"
            if args[:2] == ["pm", "enable"]:
                self.disabled.discard(args[-1]); return "enabled"
            if args[:3] == ["pm", "uninstall", "--user"]:
                self.installed.discard(args[-1]); return "Success"
            if args[:2] == ["am", "force-stop"]:
                return ""
            if args == ["pm", "list", "packages", "-d"]:
                return "".join(f"package:{p}\n" for p in self.disabled)
            if args == ["pm", "list", "packages"]:
                return "".join(f"package:{p}\n" for p in self.installed)
            return ""

    import tempfile
    log = ActionLog(Path(tempfile.mkdtemp()) / "log.json")
    adb = FakeAdb()

    adware = App(package="com.random.adware", installer=None, overlay=True)
    protected = App(package="com.google.android.gms", installer="com.android.vending")

    # Protected app can never be paused / uninstalled / stopped.
    for fn in (pause, uninstall):
        try:
            fn(adb, protected, log)
            assert False, "protected app was actioned!"
        except ProtectedAppError:
            pass
    assert "com.google.android.gms" in adb.installed  # untouched

    # Adware pauses, verifies, and undoes.
    assert pause(adb, adware, log) is True
    assert adware.enabled is False
    assert undo(adb, log.recent()[0], log) is True
    assert "com.random.adware" not in adb.disabled

    # stop_all skips the protected app, hits only the running third-party one.
    running = App(package="com.random.adware", installer=None, enabled=True)
    stopped, attempted = stop_all(adb, [running, protected], log)
    assert attempted == 1 and stopped == 1, (stopped, attempted)
    print("actions.py demo OK")


if __name__ == "__main__":
    demo()
