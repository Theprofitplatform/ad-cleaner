import sys

import pytest

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


def test_run_recovers_from_offline_device(monkeypatch):
    """A Samsung drops to `offline` on its own; run() must kick it with
    `adb reconnect offline` and retry once, not fail the whole action.
    Field regression (SM-S731B): the scan worked, the phone went offline, and
    every later Pause/Uninstall died before it reached the undo log."""
    calls = []

    class Proc:
        def __init__(self, rc, err=""):
            self.returncode, self.stdout, self.stderr = rc, "ok\n", err

    def fake_run(cmd, **kw):
        calls.append(cmd[1:])                       # drop the adb path
        if cmd[-1] == "boom" and len(calls) == 1:   # first real attempt
            return Proc(1, "adb.exe: device offline")
        return Proc(0)

    monkeypatch.setattr(adb.subprocess, "run", fake_run)
    assert adb.Adb("adb", serial="R5C").run(["shell", "boom"]) == "ok\n"
    assert calls == [
        ["-s", "R5C", "shell", "boom"],             # fails offline
        ["-s", "R5C", "reconnect", "offline"],      # kick
        ["-s", "R5C", "wait-for-device"],
        ["-s", "R5C", "shell", "boom"],             # retried, succeeds
    ]


def test_run_offline_gives_up_after_one_retry(monkeypatch):
    """A phone that is genuinely unplugged must raise, not loop forever."""
    n = []

    class Proc:
        returncode, stdout, stderr = 1, "", "adb.exe: device offline"

    monkeypatch.setattr(adb.subprocess, "run",
                        lambda cmd, **kw: (n.append(1), Proc())[1])
    with pytest.raises(adb.AdbError):
        adb.Adb("adb").run(["shell", "boom"])
    # attempt + reconnect + one retry. The kick errors too here, so reconnect()
    # bails before wait-for-device -- either way the call count is bounded.
    assert len(n) == 3


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


from adb import AdbError, mdns_discover, parse_mdns_services


def test_parse_mdns_services_classifies_connect_and_pairing():
    out = ("List of discovered mdns services\n"
           "adb-R58N1-QhSlvJ\t_adb-tls-connect._tcp\t192.168.1.9:37099\n"
           "adb-R58N1-QhSlvJ\t_adb-tls-pairing._tcp\t192.168.1.9:41234\n")
    assert parse_mdns_services(out) == {"connect": ["192.168.1.9:37099"],
                                        "pairing": ["192.168.1.9:41234"]}


def test_parse_mdns_services_tolerates_variants_and_dedupes():
    out = ("adb-x\t_adb-tls-connect._tcp.\t192.168.1.9:37099\n"     # trailing dot
           "adb-x\t_adb._tcp.\t192.168.1.9:5555\n"                  # plain adbd tcpip
           "adb-x\t_adb-tls-connect._tcp\t192.168.1.9:37099\n"      # duplicate
           "garbage line with no address\n")
    got = parse_mdns_services(out)
    assert got["connect"] == ["192.168.1.9:37099", "192.168.1.9:5555"]
    assert got["pairing"] == []


def test_parse_mdns_services_empty():
    assert parse_mdns_services("") == {"connect": [], "pairing": []}
    assert parse_mdns_services("List of discovered mdns services\n") == \
        {"connect": [], "pairing": []}


def test_mdns_discover_swallows_adb_errors():
    class Broken:
        def run(self, args, timeout=10):
            raise AdbError("mdns not supported")
    assert mdns_discover(Broken()) == {"connect": [], "pairing": []}


def test_friendly_maps_install_failures_before_the_generic_rules():
    # The reason an old handset can't take a current APK — the message has to
    # say so, not "the phone refused that action".
    msg = adb._friendly("adb: failed to install x.apk: Failure [INSTALL_FAILED_OLDER_SDK]")
    assert "newer Android version" in msg
    # Codes containing PERMISSION / NOT_INSTALLED must not fall through to the
    # generic arms below them.
    assert "INSTALL_FAILED_PERMISSION_MODEL_DOWNGRADE" in adb._friendly(
        "Failure [INSTALL_FAILED_PERMISSION_MODEL_DOWNGRADE]")
    assert "space" in adb._friendly("Failure [INSTALL_FAILED_INSUFFICIENT_STORAGE]")


def test_install_treats_exit_zero_failure_as_an_error():
    """Old adb prints Failure but still exits 0 — success is read from the text."""
    class _Old:
        def run(self, args, timeout=600):
            return "Failure [INSTALL_FAILED_OLDER_SDK]"
    a = adb.Adb.__new__(adb.Adb)
    a.run = _Old().run
    with pytest.raises(adb.AdbError, match="newer Android version"):
        a.install("x.apk")
