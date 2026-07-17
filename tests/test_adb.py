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


from adb import wifi_connect


class WifiFake:
    def __init__(self, pair_out="Successfully paired to 192.168.1.9:41567 [guid=x]",
                 connect_out="connected to 192.168.1.9:37099"):
        self.calls = []
        self.pair_out, self.connect_out = pair_out, connect_out

    def run(self, args, timeout=10):
        self.calls.append(list(args))
        if args[0] == "pair":
            return self.pair_out
        if args[0] == "connect":
            return self.connect_out
        return ""


def test_wifi_connect_pairs_then_connects():
    fake = WifiFake()
    ok, msg = wifi_connect(fake, "192.168.1.9:37099", "192.168.1.9:41567", "123456")
    assert ok and "connected" in msg
    assert fake.calls[0] == ["pair", "192.168.1.9:41567", "123456"]
    assert fake.calls[1] == ["connect", "192.168.1.9:37099"]


def test_wifi_connect_skips_pairing_when_blank():
    fake = WifiFake()
    ok, _ = wifi_connect(fake, "192.168.1.9:37099")
    assert ok and fake.calls == [["connect", "192.168.1.9:37099"]]


def test_wifi_connect_rejects_half_filled_pairing():
    fake = WifiFake()
    ok, msg = wifi_connect(fake, "192.168.1.9:37099", "192.168.1.9:41567")
    assert not ok and "both" in msg and fake.calls == []
    ok, msg = wifi_connect(fake, "192.168.1.9:37099", "", "123456")
    assert not ok and fake.calls == []


def test_wifi_connect_reports_connect_failure():
    fake = WifiFake(connect_out="failed to connect to 192.168.1.9:37099")
    ok, msg = wifi_connect(fake, "192.168.1.9:37099")
    assert not ok and "failed" in msg


def test_wifi_connect_reports_pair_failure():
    fake = WifiFake(pair_out="Failed: Wrong password or connection was dropped")
    ok, msg = wifi_connect(fake, "192.168.1.9:37099", "192.168.1.9:41567", "000000")
    assert not ok and len(fake.calls) == 1   # never tries to connect


def test_wifi_connect_already_connected_is_ok():
    fake = WifiFake(connect_out="already connected to 192.168.1.9:37099")
    ok, _ = wifi_connect(fake, "192.168.1.9:37099")
    assert ok
