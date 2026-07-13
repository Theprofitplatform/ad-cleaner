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


def test_find_adb_prefers_bundled_meipass(tmp_path, monkeypatch):
    """The packaged exe ships ADB in sys._MEIPASS; find_adb must use it."""
    bundle = tmp_path / "platform-tools"
    bundle.mkdir()
    (bundle / "adb.exe").write_text("stub")
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    found = adb.find_adb(base=tmp_path / "nonexistent")
    assert found == str(bundle / "adb.exe")
