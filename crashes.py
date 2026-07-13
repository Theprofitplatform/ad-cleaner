"""Read Android crash / freeze / restart diagnostics over ADB, in plain English.

Read-only, no root. Two sources:
  * getprop <boot reason>     -- why the phone last restarted
  * dumpsys dropbox --print   -- persisted crashes/ANRs/tombstones (survive reboots)

Pure parsers (unit-tested against a fixture) plus a thin read_crash_report that
drives an Adb object. All device I/O lives in adb.py.
"""

import re
from dataclasses import dataclass

# DropBox tag -> (plain-English label, is_fault). Tags not listed are ignored as
# noise (e.g. Samsung's tiny "clockpackage" alarm-state notes, SYSTEM_AUDIT).
TAGS = {
    "SYSTEM_TOMBSTONE": ("A system/hardware service crashed", True),
    "SYSTEM_RESTART": ("Android restarted itself", True),
    "system_server_crash": ("Android system crashed (restart)", True),
    "system_server_watchdog": ("Android froze and force-restarted", True),
    "system_app_crash": ("A built-in app crashed", True),
    "system_app_anr": ("A built-in app froze", True),
    "data_app_crash": ("An app crashed", True),
    "data_app_native_crash": ("An app crashed", True),
    "data_app_anr": ("An app froze (stopped responding)", True),
    "SYSTEM_BOOT": ("Phone started up", False),
}

# Known raw boot reasons -> what it means for the user.
BOOT_REASONS = {
    "kernel_panic": "The system hit a fatal error and restarted (kernel panic).",
    "watchdog": "The system froze and force-restarted itself.",
    "shutdown,thermal": "It got too hot and shut down to protect itself.",
    "thermal": "It got too hot and shut down to protect itself.",
    "shutdown,battery": "The battery ran flat and it powered off.",
    "shutdown,lpm": "It was powered off, then turned back on.",
    "reboot,userrequested": "You restarted it (normal).",
    "reboot,longkey": "Restarted by holding the power button.",
    "reboot,ota": "It restarted to install a software update (normal).",
    "reboot": "It restarted normally.",
}


@dataclass
class CrashEvent:
    when: str      # "2026-07-11 20:25:52"
    label: str     # plain-English what-happened
    detail: str    # app package or service name
    is_fault: bool


_HEADER = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\s+([A-Za-z_]+)\s+\(")
_PROC = re.compile(r">>>\s+(\S+)")
_ANR_PKG = re.compile(r"([a-zA-Z][\w.]+)/[\w.$]+ is not responding")


def boot_reason_text(reason):
    """Turn a raw boot reason into a sentence."""
    if not reason:
        return "Unknown."
    for key, text in BOOT_REASONS.items():
        if key in reason:
            return text
    return reason


def friendly_process(proc):
    """A crashed native process path -> a name a person recognises."""
    name = proc.rsplit("/", 1)[-1]
    low = name.lower()
    for needle, label in (("camera", "Camera service"), ("audio", "Audio service"),
                          ("media", "Media service"), ("codec", "Media service"),
                          ("system_server", "Android system"),
                          ("surfaceflinger", "Display service"),
                          ("bluetooth", "Bluetooth service"), ("netd", "Network service")):
        if needle in low:
            return label
    return name


def _detail_for(tag, body):
    low = tag.lower()
    if "anr" in low:
        m = _ANR_PKG.search(body)
        return m.group(1) if m else ""
    if "crash" in low or "tombstone" in low:
        m = _PROC.search(body)
        if m:
            return friendly_process(m.group(1))
        m = re.search(r"Process:\s*(\S+)", body)
        return m.group(1) if m else ""
    return ""


def parse_dropbox(text):
    """`dumpsys dropbox --print` -> [CrashEvent], newest first, deduped."""
    events, cur, body = [], None, []

    def flush():
        if cur:
            label, is_fault = TAGS[cur[1]]
            events.append(CrashEvent(cur[0], label, _detail_for(cur[1], "\n".join(body)),
                                     is_fault))

    for line in text.splitlines():
        m = _HEADER.match(line)
        if m:
            when, tag = m.group(1), m.group(2)
            flush()
            cur, body = None, []
            # The *_PROTO_WITH_HEADERS twins are binary duplicates -- skip them;
            # the plain text entry carries the readable backtrace.
            if tag in TAGS and not tag.endswith("_PROTO_WITH_HEADERS"):
                cur = (when, tag)
            continue
        if cur:
            body.append(line)
    flush()

    seen, out = set(), []
    for e in events:
        key = (e.when[:16], e.label, e.detail)  # collapse same-minute repeats
        if key not in seen:
            seen.add(key)
            out.append(e)
    out.sort(key=lambda e: e.when, reverse=True)
    return out


def summarize(events):
    """One-line verdict + banner kind ('good'/'warn') for a list of events."""
    faults = [e for e in events if e.is_fault]
    if not faults:
        return "✅  No crashes or freezes on record — the phone looks stable.", "good"
    counts = {}
    for e in faults:
        counts[e.label] = counts.get(e.label, 0) + 1
    top = max(counts, key=counts.get)
    return (f"⚠️  {len(faults)} crash/freeze event(s) on record. "
            f"Most common: {top.lower()}.", "warn")


def read_crash_report(adb):
    """Drive an Adb object; return {boot_reason, boot_text, events}."""
    boot = ""
    for prop in ("sys.boot.reason", "ro.boot.bootreason"):
        try:
            boot = adb.get_prop(prop)
        except Exception:
            boot = ""
        if boot:
            break
    try:
        raw = adb.shell_text(["dumpsys", "dropbox", "--print"], timeout=45)
    except Exception:
        raw = ""
    return {"boot_reason": boot, "boot_text": boot_reason_text(boot),
            "events": parse_dropbox(raw)}


def demo():
    sample = (
        "2026-07-11 07:43:28 SYSTEM_BOOT (text, 528 bytes)\n"
        "Boot info\n"
        "2026-07-11 20:25:52 SYSTEM_TOMBSTONE_PROTO_WITH_HEADERS (compressed data, 27792 bytes)\n"
        "2026-07-11 20:25:52 SYSTEM_TOMBSTONE (compressed text, 6573 bytes)\n"
        "Cmdline: /vendor/bin/hw/vendor.samsung.hardware.camera.provider-service_64\n"
        "pid: 1996, tid: 1996, name: binder  >>> /vendor/bin/hw/vendor.samsung.hardware.camera.provider-service_64 <<<\n"
        "  #00 pc raise\n"
        "2026-07-11 20:25:52 SYSTEM_TOMBSTONE (compressed text, 6429 bytes)\n"
        "pid: 1996  >>> /vendor/bin/hw/vendor.samsung.hardware.camera.provider-service_64 <<<\n"
        "2026-07-12 10:50:14 clockpackage (text, 43 bytes)\n"
        "noise noise\n"
        "2026-07-12 17:52:22 data_app_anr (compressed text, 18561 bytes)\n"
        "Subject: Input dispatching timed out (org.telegram.messenger/org.telegram.ui.LaunchActivity is not responding. Waited 10000ms).\n"
    )
    events = parse_dropbox(sample)
    labels = [(e.label, e.detail) for e in events]
    # Camera tombstone collapses 2 -> 1 and resolves to a friendly name.
    assert ("A system/hardware service crashed", "Camera service") in labels, labels
    # ANR resolves the frozen app's package.
    assert ("An app froze (stopped responding)", "org.telegram.messenger") in labels, labels
    # Boot is captured but not a fault; clockpackage noise is dropped.
    assert any(e.label == "Phone started up" and not e.is_fault for e in events)
    assert not any("clock" in e.detail.lower() for e in events)
    # Exactly one camera crash after dedupe.
    assert sum(d == "Camera service" for _, d in labels) == 1, labels
    assert boot_reason_text("shutdown,lpm").startswith("It was powered off")
    text, kind = summarize(events)
    assert kind == "warn" and "event(s)" in text
    print("crashes.py demo OK")


if __name__ == "__main__":
    demo()
