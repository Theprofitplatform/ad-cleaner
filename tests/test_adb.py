import sys

import adb
from adb import _friendly, parse_devices


def test_parse_devices():
    out = ("List of devices attached\n"
           "R58N1  device usb:1-3 product:o1sxx model:SM_G991B device:o1s transport_id:1\n"
           "EFGH   unauthorized usb:1-4 transport_id:2\n")
    devs = parse_devices(out)
    assert devs[0] == {"serial": "R58N1", "state": "device", "model": "SM G991B"}
    assert devs[1]["state"] == "unauthorized"


def test_friendly_messages():
    assert "not authorized" in _friendly("error: device unauthorized").lower()
    assert "offline" in _friendly("device offline").lower()
    assert "no longer installed" in _friendly("Failure [not installed for 0]").lower()


def test_run_survives_non_utf8_output():
    """A real phone can emit bytes the Windows cp1252 locale can't decode
    (non-Latin app names). run() must decode UTF-8 with replacement, not crash.
    Regression: 'charmap codec can't decode byte 0x81' killed the scan thread."""
    code = r"import sys; sys.stdout.buffer.write(b'app\x81name'); sys.exit(0)"
    out = adb.Adb(sys.executable).run(["-c", code])
    assert "app" in out and "name" in out   # decoded, no UnicodeDecodeError


def test_find_adb_prefers_bundled_meipass(tmp_path, monkeypatch):
    """The packaged exe ships ADB in sys._MEIPASS; find_adb must use it."""
    bundle = tmp_path / "platform-tools"
    bundle.mkdir()
    (bundle / "adb.exe").write_text("stub")
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    found = adb.find_adb(base=tmp_path / "nonexistent")
    assert found == str(bundle / "adb.exe")
