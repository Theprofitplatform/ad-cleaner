"""Parser tests for crashes.py, seeded from a real Galaxy S26 Ultra dropbox dump."""
from crashes import boot_reason_text, friendly_process, parse_dropbox, summarize

# Trimmed from an actual `dumpsys dropbox --print` on the connected device.
REAL = (
    "2026-07-11 07:43:28 SYSTEM_BOOT (text, 528 bytes)\n"
    "Boot reason: reboot\n"
    "2026-07-11 20:25:52 SYSTEM_TOMBSTONE_PROTO_WITH_HEADERS (compressed data, 27792 bytes)\n"
    "<binary twin, ignored>\n"
    "2026-07-11 20:25:52 SYSTEM_TOMBSTONE (compressed text, 6573 bytes)\n"
    "Cmdline: /vendor/bin/hw/vendor.samsung.hardware.camera.provider-service_64\n"
    "pid: 1996, tid: 1996, name: binder:1996_2  >>> /vendor/bin/hw/vendor.samsung.hardware.camera.provider-service_64 <<<\n"
    "      #00 pc 000000000008a69c  /apex/com.android.runtime/lib64/bionic/libc.so (raise+124)\n"
    "2026-07-11 20:25:52 SYSTEM_TOMBSTONE (compressed text, 6429 bytes)\n"
    "pid: 1996  >>> /vendor/bin/hw/vendor.samsung.hardware.camera.provider-service_64 <<<\n"
    "2026-07-12 10:50:14 clockpackage (text, 43 bytes)\n"
    "benign alarm note\n"
    "2026-07-12 17:52:22 data_app_anr (compressed text, 18561 bytes)\n"
    "Subject: Input dispatching timed out (bc0 org.telegram.messenger/org.telegram.ui.LaunchActivity"
    " is not responding. Waited 10000ms for MotionEvent).\n"
)


def test_parses_camera_tombstone_deduped():
    events = parse_dropbox(REAL)
    cams = [e for e in events if e.detail == "Camera service"]
    assert len(cams) == 1                       # 2 text tombstones collapse to one
    assert cams[0].is_fault
    assert cams[0].when == "2026-07-11 20:25:52"
    assert "crashed" in cams[0].label.lower()


def test_parses_app_anr_package():
    events = parse_dropbox(REAL)
    anr = [e for e in events if "froze" in e.label.lower()]
    assert anr and anr[0].detail == "org.telegram.messenger"


def test_boot_is_not_a_fault_and_noise_dropped():
    events = parse_dropbox(REAL)
    boots = [e for e in events if e.label == "Phone started up"]
    assert boots and not boots[0].is_fault
    assert not any("clock" in e.detail.lower() or e.label == "clockpackage" for e in events)


def test_newest_first():
    events = parse_dropbox(REAL)
    assert events == sorted(events, key=lambda e: e.when, reverse=True)


def test_friendly_process_names():
    assert friendly_process("/vendor/bin/hw/vendor.samsung.hardware.camera.provider-service_64") \
        == "Camera service"
    assert friendly_process("/system/bin/audioserver") == "Audio service"
    assert friendly_process("/vendor/bin/hw/some.random.daemon") == "some.random.daemon"


def test_boot_reason_text():
    assert boot_reason_text("shutdown,lpm").startswith("It was powered off")
    assert "too hot" in boot_reason_text("shutdown,thermal")
    assert boot_reason_text("") == "Unknown."


def test_summary_clean_vs_faults():
    assert summarize([])[1] == "good"
    events = parse_dropbox(REAL)
    text, kind = summarize(events)
    assert kind == "warn" and "event(s)" in text
