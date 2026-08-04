"""Age-out tests for retention.py.

The one that matters is stamp_beats_mtime: an old file pulled off a phone today
keeps the phone's original mtime, so trusting mtime alone would delete a
customer's backup the day it was made.
"""
import os
from datetime import datetime, timedelta

from retention import KEEP_DAYS, captured_at, stale

NOW = datetime(2026, 8, 4, 12, 0)


def _touch(path, when):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x", encoding="utf-8")
    ts = when.timestamp()
    os.utime(path, (ts, ts))


def test_stale_splits_on_the_cutoff(tmp_path):
    _touch(tmp_path / "file_backups" / "20260601-120000 old.mp4", NOW)
    _touch(tmp_path / "file_backups" / "20260802-120000 new.mp4", NOW)
    names = [e.name for e in stale(tmp_path, now=NOW)]
    assert names == ["20260601-120000 old.mp4"]


def test_stamp_beats_mtime(tmp_path):
    # Pulled yesterday, but the phone stamped the file in 2019.
    f = tmp_path / "file_backups" / "20260803-090000 holiday.mp4"
    _touch(f, datetime(2019, 4, 2, 8, 0))
    assert captured_at(f).year == 2026
    assert stale(tmp_path, now=NOW) == []


def test_mtime_used_when_name_has_no_stamp(tmp_path):
    # apk_backups dirs are bare serials -- mtime is all there is.
    d = tmp_path / "apk_backups" / "R58W50FEKCW"
    _touch(d / "com.example.apk", NOW - timedelta(days=KEEP_DAYS + 1))
    ts = (NOW - timedelta(days=KEEP_DAYS + 1)).timestamp()
    os.utime(d, (ts, ts))
    assert [e.name for e in stale(tmp_path, now=NOW)] == ["R58W50FEKCW"]


def test_transfers_dir_stamp_is_read(tmp_path):
    d = tmp_path / "transfers" / "Nokia_G50_20260601_135211"
    _touch(d / "DCIM" / "a.jpg", NOW)
    assert [e.name for e in stale(tmp_path, now=NOW)] == ["Nokia_G50_20260601_135211"]


def test_reports_are_never_touched(tmp_path):
    _touch(tmp_path / "reports" / "20260101-090000 job.pdf", NOW)
    assert stale(tmp_path, now=NOW) == []
