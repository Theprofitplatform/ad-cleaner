"""appicon.py: fish the launcher icon out of a pulled APK, cache it, never raise."""
import base64
import shutil
import zipfile

import adb
import appicon
from appicon import device_icon, pick_icon

TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M8AAAMBAQDJ/pLvAAAAAElFTkSuQmCC")


class FakeAdb:
    """pm path answers; pull copies a prepared 'APK' into place."""
    def __init__(self, apk_path):
        self.apk_path = apk_path
        self.pulls = 0

    def shell_text(self, args, timeout=10):
        assert args[:2] == ["pm", "path"]
        return "package:/data/app/base.apk\n"

    def pull(self, remote, local, timeout=120):
        self.pulls += 1
        shutil.copy(self.apk_path, local)
        return "pulled"


def make_apk(path, entries):
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return path


def test_pick_icon_prefers_mipmap_and_highest_density():
    names = ["classes.dex", "res/drawable/icon.png",
             "res/mipmap-hdpi/ic_launcher.png",
             "res/mipmap-xxxhdpi/ic_launcher.png",
             "res/mipmap-xxxhdpi/ic_launcher_foreground.png"]
    assert pick_icon(names) == "res/mipmap-xxxhdpi/ic_launcher.png"


def test_pick_icon_none_when_no_candidates():
    assert pick_icon(["classes.dex", "assets/logo.txt", "res/raw/song.mp3"]) is None


def test_device_icon_extracts_and_caches(monkeypatch, tmp_path):
    monkeypatch.setattr(adb, "data_dir", lambda: tmp_path)
    apk = make_apk(tmp_path / "a.apk",
                   {"res/mipmap-xxhdpi/ic_launcher.png": TINY_PNG})
    fake = FakeAdb(apk)
    out = device_icon(fake, "com.x")
    assert out is not None and out.exists() and out.suffix == ".png"
    assert device_icon(fake, "com.x") == out
    assert fake.pulls == 1                      # second call served from cache


def test_device_icon_garbage_apk_returns_none(monkeypatch, tmp_path):
    monkeypatch.setattr(adb, "data_dir", lambda: tmp_path)
    junk = tmp_path / "junk.apk"
    junk.write_text("not a zip")
    assert device_icon(FakeAdb(junk), "com.junk") is None


def test_device_icon_adb_failure_returns_none(monkeypatch, tmp_path):
    monkeypatch.setattr(adb, "data_dir", lambda: tmp_path)

    class DeadAdb:
        def shell_text(self, args, timeout=10):
            raise RuntimeError("device gone")

    assert device_icon(DeadAdb(), "com.x") is None


def test_save_play_icon(monkeypatch, tmp_path):
    monkeypatch.setattr(adb, "data_dir", lambda: tmp_path)
    out = appicon.save_play_icon("com.x", TINY_PNG)
    assert out is not None and out.exists()
    assert appicon.save_play_icon("com.x", b"ignored") == out   # cached
