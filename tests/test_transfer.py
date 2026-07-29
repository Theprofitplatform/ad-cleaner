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
        self.scanned = False

    def shell_text(self, args, timeout=10):
        # _push_media asks MediaStore to reindex; without it the restored
        # photos sit on disk invisible to the Gallery.
        if "scan_volume" in " ".join(args):
            self.scanned = True
            return "Result: Bundle[{}]"
        raise AdbError("unexpected: " + " ".join(args))

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


GB = 1024 ** 3


def test_space_warning_silent_when_there_is_room():
    assert gui.space_warning(10 * GB, 40 * GB) == ""


def test_space_warning_fires_when_phone_bigger_than_free_space():
    msg = gui.space_warning(50 * GB, 20 * GB)
    assert "50.0 GB" in msg and "20.0 GB" in msg


def test_space_warning_needs_headroom_not_just_equality():
    """Exactly-enough is not enough — adb writes temp files alongside."""
    assert gui.space_warning(20 * GB, 20 * GB) != ""


def test_space_warning_silent_when_either_side_unknown():
    # A failed df must never block a save; crying wolf trains techs to click past.
    assert gui.space_warning(0, 5 * GB) == ""
    assert gui.space_warning(5 * GB, None) == ""


def test_parse_df_accepts_any_mount_when_asked():
    """Shared storage is /storage/emulated on a modern phone and
    /storage/sdcard0 on an older one, so the caller can't name the mount."""
    from device import parse_df
    out = ("Filesystem      1K-blocks    Used Available Use% Mounted on\n"
           "/dev/fuse        24000000 2900000  21100000  13% /storage/emulated\n")
    assert parse_df(out) == (0, 0, 0)                 # default still wants /data
    total, used, free = parse_df(out, mount=None)
    assert (total, used, free) == (24000000 * 1024, 2900000 * 1024, 21100000 * 1024)


def test_push_media_sends_only_subfolders(tmp_path):
    src = tmp_path / "saved"
    (src / "DCIM").mkdir(parents=True)
    (src / "Music").mkdir(parents=True)
    (src / "note.txt").write_text("stray file, must be ignored")
    adb = FakePullPush(present=set())
    pushed, failed = gui._push_media(adb, src)
    assert set(pushed) == {"DCIM", "Music"}
    assert failed == []
    assert adb.scanned, "pushed files stay invisible to the Gallery without a rescan"
