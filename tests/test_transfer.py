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
    saved, skipped = gui._pull_media(adb, tmp_path / "out")
    assert saved == ["DCIM", "Download"]                       # kept order
    assert set(skipped) == set(gui.TRANSFER_FOLDERS) - {"DCIM", "Download"}
    assert (tmp_path / "out" / "DCIM" / "file.jpg").exists()


def test_push_media_sends_only_subfolders(tmp_path):
    src = tmp_path / "saved"
    (src / "DCIM").mkdir(parents=True)
    (src / "Music").mkdir(parents=True)
    (src / "note.txt").write_text("stray file, must be ignored")
    adb = FakePullPush(present=set())
    pushed, failed = gui._push_media(adb, src)
    assert set(pushed) == {"DCIM", "Music"}
    assert failed == []
