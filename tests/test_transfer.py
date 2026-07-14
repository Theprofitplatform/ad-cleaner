"""Old-phone -> new-phone file transfer helpers (_pull_media / _push_media).

Pure-function tests with a tiny fake ADB — no Tk, no device. They pin the two
things that can break: absent folders are skipped (not fatal), and only real
subfolders are pushed back.
"""

import pytest

pytest.importorskip("tkinter")
import gui
from adb import AdbError


class FakePullPush:
    """Mimics adb directory semantics: `pull /sdcard/DCIM dest` -> dest/DCIM/."""

    def __init__(self, present):
        self.present = set(present)   # folder names that "exist" on the phone
        self.pulled, self.pushed = [], []

    def pull(self, remote, local, timeout=120):
        from pathlib import Path
        name = remote.rstrip("/").split("/")[-1]
        if name not in self.present:
            raise AdbError("remote object does not exist")
        d = Path(local) / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "file.jpg").write_bytes(b"x")
        self.pulled.append(name)
        return "1 file pulled"

    def push(self, local, remote, timeout=120):
        from pathlib import Path
        self.pushed.append(Path(local).name)
        return "1 file pushed"


def test_pull_media_skips_absent_folders(tmp_path):
    adb = FakePullPush(present={"DCIM", "Download"})
    saved, skipped, failed = gui._pull_media(adb, tmp_path / "out")
    assert saved == ["DCIM", "Download"]                       # kept order
    assert set(skipped) == set(gui.TRANSFER_FOLDERS) - {"DCIM", "Download"}
    assert failed == []
    assert (tmp_path / "out" / "DCIM" / "file.jpg").exists()


def test_pull_media_real_errors_are_failed_not_skipped(tmp_path):
    # A disconnect/timeout/disk-full is NOT "folder doesn't exist" -- it must
    # land in `failed` (so the GUI never shows the ✅ a tech would wipe the old
    # phone on), and the remaining folders must still be attempted.
    class FlakyAdb(FakePullPush):
        def pull(self, remote, local, timeout=120):
            if remote.endswith("Pictures"):
                raise AdbError("Command timed out after 3600s")
            return super().pull(remote, local, timeout)

    adb = FlakyAdb(present={"DCIM", "Pictures", "Music"})
    saved, skipped, failed = gui._pull_media(adb, tmp_path / "out")
    assert failed == ["Pictures"]
    assert saved == ["DCIM", "Music"]                          # kept going
    assert "Pictures" not in skipped


def test_push_media_sends_only_subfolders(tmp_path):
    src = tmp_path / "saved"
    (src / "DCIM").mkdir(parents=True)
    (src / "Music").mkdir(parents=True)
    (src / "note.txt").write_text("stray file, must be ignored")
    adb = FakePullPush(present=set())
    pushed, failed = gui._push_media(adb, src)
    assert set(pushed) == {"DCIM", "Music"}
    assert failed == []
