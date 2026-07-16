from pathlib import Path

import mirror


def test_find_scrcpy_misses_in_empty_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(mirror.shutil, "which", lambda name: None)
    assert mirror.find_scrcpy(base=tmp_path) is None


def test_find_scrcpy_prefers_app_folder(tmp_path):
    exe = tmp_path / "scrcpy" / "scrcpy.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"")
    assert mirror.find_scrcpy(base=tmp_path) == str(exe)


def test_launch_points_scrcpy_at_our_adb(monkeypatch):
    seen = {}

    def fake_popen(cmd, env=None, **kw):
        seen["cmd"], seen["env"] = cmd, env
        return "proc"

    monkeypatch.setattr(mirror.subprocess, "Popen", fake_popen)
    assert mirror.launch("scrcpy.exe", r"C:\pt\adb.exe", "R5GL24XWASL",
                         title="S26") == "proc"
    assert seen["cmd"][:3] == ["scrcpy.exe", "--serial", "R5GL24XWASL"]
    # ADB env is the contract: without it scrcpy's own adb kills our server
    assert seen["env"]["ADB"] == r"C:\pt\adb.exe"
