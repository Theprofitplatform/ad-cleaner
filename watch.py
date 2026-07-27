"""Live pop-up attribution: catch the app that draws over the screen.

Every other signal in scanner.py is circumstantial -- permissions, install
source, notification churn. This one is evidence. While the operator uses the
phone normally, we poll the window manager and record which package actually
put a window over the screen at the moment the ad appeared.

Read-only, no root, nothing installed on the phone: the on-device "popup ad
detector" apps need an accessibility service to see this, adb reads the same
window state from outside.

Two kinds of event, deliberately different confidence:
  * overlay -- a non-system app has a live overlay window up. Unambiguous: that
    is the pop-up mechanism itself, so it is recorded as evidence and scores.
  * focus   -- the foreground app changed. Timeline only. The operator may have
    opened that app themselves; they can promote one to evidence by hand.

Pure parsers (fixture-tested) plus a WatchSession that turns successive polls
into events. All device I/O lives in adb.py; the caller owns the poll loop.
"""

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from protected import is_protected

POLL_SECONDS = 1.0     # ponytail: polling, not a logcat stream -- see read_windows
KEEP_DAYS = 7          # evidence older than this stops counting against an app
MAX_PER_PKG = 20       # cap the log so a chatty phone can't grow it forever

# Window types that put something over other apps. Whitelist, not blacklist:
# TOAST and INPUT_METHOD are legitimate and constant, so they stay out.
# Numeric forms appear on older dumps that don't spell the type out.
OVERLAY_TYPES = frozenset({
    "APPLICATION_OVERLAY", "SYSTEM_ALERT", "SYSTEM_OVERLAY", "SYSTEM_ERROR",
    "PHONE", "PRIORITY_PHONE",
    "2002", "2003", "2006", "2007", "2010", "2038",
})

KINDS = {
    "overlay": "Drew a pop-up over the screen",
    "focus": "Jumped to the front",
    "flagged": "Marked as the ad",
}
EVIDENCE = ("overlay", "flagged")   # kinds that count in the score


@dataclass
class WatchEvent:
    when: datetime
    package: str
    kind: str

    @property
    def what(self):
        return KINDS.get(self.kind, self.kind)


# --- Parsers ----------------------------------------------------------------

_FOCUS_RE = re.compile(r"mCurrentFocus=Window\{\S+\s+\S+\s+([\w.]+)/")
_FOCUSED_APP_RE = re.compile(r"mFocusedApp=\S*ActivityRecord\{\S+\s+\S+\s+([\w.]+)/")
_WINDOW_HEAD = re.compile(r"^\s*Window #\d+ Window\{")
_PACKAGE_RE = re.compile(r"\bpackage=([\w.]+)")
_TYPE_RE = re.compile(r"\bty=(\w+)")


def parse_focus(text):
    """`dumpsys window` -> the package that owns the focused window, or None.

    Falls back to mFocusedApp: during a transition mCurrentFocus can be a
    system window (the shade, the lock screen) with no package in it.
    """
    for rx in (_FOCUS_RE, _FOCUSED_APP_RE):
        m = rx.search(text or "")
        if m and "." in m.group(1):
            return m.group(1)
    return None


def parse_overlays(text):
    """`dumpsys window windows` -> {packages showing an overlay right now}.

    A window is only counted while it is actually on screen -- adware parks
    invisible overlay windows, and those aren't what the customer is seeing.

    ponytail: reads `package=`, not `mOwnerUid=`. Every Android from 8 up
    prints the package on the window; the uid path (resolve via
    scanner.parse_pkg_uids) is the upgrade if a skin ever drops it.
    """
    found, cur = set(), {}

    def flush():
        if (cur.get("pkg") and cur.get("ty") in OVERLAY_TYPES
                and cur.get("shown", True)):
            found.add(cur["pkg"])

    for line in (text or "").splitlines():
        if _WINDOW_HEAD.match(line):
            flush()
            cur.clear()
            continue
        m = _PACKAGE_RE.search(line)
        if m:
            cur["pkg"] = m.group(1)
        m = _TYPE_RE.search(line)
        if m:
            cur["ty"] = m.group(1)
        if "mHasSurface=false" in line or "isReadyForDisplay()=false" in line:
            cur["shown"] = False
    flush()
    return found


# --- Session ----------------------------------------------------------------

class WatchSession:
    """Successive window snapshots -> events, edge-triggered.

    Only changes are reported: an overlay that is already up when watching
    starts counts (it's the ad the customer is staring at), but it isn't
    re-reported on every poll for as long as it stays up.
    """

    def __init__(self, ignore=None):
        # Genuine system/OEM packages own overlays all day (status bar, keyboard
        # popups, screen dimmers) -- they are noise, never the culprit.
        self._ignore = ignore or (lambda pkg: is_protected(pkg))
        self.focus = None
        self.overlays = frozenset()
        self.started = False

    def update(self, text, now=None):
        now = now or datetime.now()
        events = []
        overlays = frozenset(p for p in parse_overlays(text) if not self._ignore(p))
        for pkg in sorted(overlays - self.overlays):
            events.append(WatchEvent(now, pkg, "overlay"))
        focus = parse_focus(text)
        # The app already open when watching starts isn't an event -- only moves.
        if (focus and focus != self.focus and self.started
                and not self._ignore(focus)):
            events.append(WatchEvent(now, focus, "focus"))
        self.overlays, self.started = overlays, True
        if focus:
            self.focus = focus
        return events


def read_windows(adb, timeout=15):
    """One window-manager snapshot, as text.

    ponytail: one dumpsys per poll (~1s), not a `logcat -b events` stream. No
    Popen lifetime to manage on Windows and no event-tag drift across Android
    versions. Ceiling: a pop-up that comes and goes inside one second can be
    missed -- switch to the log stream if that shows up in the field.
    """
    text = adb.shell_text(["dumpsys", "window", "windows"], timeout=timeout)
    if "Window #" not in (text or ""):      # newer builds fold it into `dumpsys window`
        text = adb.shell_text(["dumpsys", "window"], timeout=timeout)
    return text


# --- Evidence log -----------------------------------------------------------

def log_path():
    from adb import data_dir   # local import: keeps the parsers adb-free
    return data_dir() / "watch_log.json"


def _read(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _prune(data, now, days=KEEP_DAYS):
    cutoff = (now - timedelta(days=days)).isoformat(timespec="seconds")
    out = {}
    for pkg, entries in data.items():
        kept = [e for e in entries if isinstance(e, dict) and e.get("ts", "") >= cutoff]
        if kept:
            out[pkg] = kept[-MAX_PER_PKG:]
    return out


def record(events, now=None, path=None):
    """Persist the evidence-grade events so the next scan can score them.

    Focus events are timeline-only -- the operator may simply have opened that
    app -- so they are dropped here unless promoted to "flagged" by hand.
    """
    keep = [e for e in events if e.kind in EVIDENCE]
    if not keep:
        return
    now = now or datetime.now()
    path = path or log_path()
    data = _prune(_read(path), now)
    for e in keep:
        data.setdefault(e.package, []).append(
            {"ts": e.when.isoformat(timespec="seconds"), "kind": e.kind})
    data = _prune(data, now)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=1), encoding="utf-8")
    except OSError:
        pass   # a read-only data dir must not kill a watch session


def load_caught(now=None, days=KEEP_DAYS, path=None):
    """-> {package: datetime of the most recent evidence}. Empty on any error."""
    now = now or datetime.now()
    try:
        data = _prune(_read(path or log_path()), now, days)
    except Exception:
        return {}
    out = {}
    for pkg, entries in data.items():
        stamps = []
        for e in entries:
            try:
                stamps.append(datetime.fromisoformat(e["ts"]))
            except (KeyError, TypeError, ValueError):
                pass
        if stamps:
            out[pkg] = max(stamps)
    return out


def forget(package, path=None):
    """Drop one package's evidence (operator decided it was a false alarm)."""
    path = path or log_path()
    data = _read(path)
    if data.pop(package, None) is None:
        return False
    try:
        path.write_text(json.dumps(data, indent=1), encoding="utf-8")
    except OSError:
        return False
    return True


SAMPLE = """\
  Window #0 Window{a1 u0 StatusBar}:
    mOwnerUid=1000 package=com.android.systemui appop=SYSTEM_ALERT_WINDOW
    mAttrs={(0,0)(fillx24) ty=STATUS_BAR fmt=TRANSLUCENT
    mHasSurface=true isReadyForDisplay()=true
  Window #1 Window{b2 u0 com.adware.pop}:
    mOwnerUid=10240 package=com.adware.pop appop=SYSTEM_ALERT_WINDOW
    mAttrs={(0,0)(fillxfill) ty=APPLICATION_OVERLAY fmt=TRANSLUCENT
    mHasSurface=true isReadyForDisplay()=true
  Window #2 Window{c3 u0 com.sneaky.hidden}:
    mOwnerUid=10241 package=com.sneaky.hidden
    mAttrs={(0,0)(fillxfill) ty=APPLICATION_OVERLAY fmt=TRANSLUCENT
    mHasSurface=false isReadyForDisplay()=false
  Window #3 Window{d4 u0 com.chat.app}:
    mOwnerUid=10242 package=com.chat.app
    mAttrs={(0,0)(wrapxwrap) ty=TOAST fmt=TRANSLUCENT
    mHasSurface=true
  mCurrentFocus=Window{e5 u0 com.android.chrome/com.google.android.apps.chrome.Main}
  mFocusedApp=ActivityRecord{f6 u0 com.android.chrome/.Main t9}
"""


def demo():
    # Live third-party overlay is found; system overlay, invisible window and
    # toast are not.
    assert parse_overlays(SAMPLE) == {"com.adware.pop"}, parse_overlays(SAMPLE)
    assert parse_focus(SAMPLE) == "com.android.chrome"
    assert parse_focus("nothing here") is None

    now = datetime(2026, 7, 27, 15, 42)
    s = WatchSession()
    evs = s.update(SAMPLE, now)
    # First poll: the overlay counts, the app already in front does not.
    assert [(e.package, e.kind) for e in evs] == [("com.adware.pop", "overlay")], evs
    # Still up next poll -> no repeat.
    assert s.update(SAMPLE, now) == []
    # Chrome is protected, so its focus never fires; a junk app's does.
    moved = SAMPLE.replace("com.android.chrome/com.google.android.apps.chrome.Main",
                           "com.random.gift/.Main")
    evs = s.update(moved, now)
    assert [(e.package, e.kind) for e in evs] == [("com.random.gift", "focus")], evs
    # Overlay drops off and comes back -> reported again.
    gone = SAMPLE.replace("package=com.adware.pop", "package=com.other.app")
    s.update(gone, now)
    assert [e.package for e in s.update(SAMPLE, now)] == ["com.adware.pop"]

    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "watch_log.json"
        record([WatchEvent(now, "com.adware.pop", "overlay"),
                WatchEvent(now, "com.random.gift", "focus")], now, p)
        caught = load_caught(now, path=p)
        # Overlay is evidence; a bare focus change is not.
        assert caught == {"com.adware.pop": now}, caught
        # Evidence expires.
        assert load_caught(now + timedelta(days=KEEP_DAYS + 1), path=p) == {}
        assert forget("com.adware.pop", p) and load_caught(now, path=p) == {}
        assert load_caught(now, path=Path(tmp) / "missing.json") == {}
    print("watch.py demo OK")


if __name__ == "__main__":
    demo()
