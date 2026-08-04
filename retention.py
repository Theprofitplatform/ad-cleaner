"""Age out customer data under adcleaner_data/.

Backups pulled off a customer's phone are working files, not records -- once the
job is done and paid, holding a stranger's photos and APKs is a liability, not an
asset. This deletes anything older than KEEP_DAYS from the three heavy folders.
reports/ is deliberately excluded: those are the signed job records.

Dry-run by default. Pass --apply to actually delete.

    python retention.py                 # list what would go
    python retention.py --apply         # delete it
    python retention.py --days 60 --apply
"""

import re
import shutil
import sys
from datetime import datetime, timedelta

KEEP_DAYS = 30

# file_backups entries are "YYYYMMDD-HHMMSS <original name>"; transfers dirs are
# "<Device>_YYYYMMDD_HHMMSS". apk_backups dirs are bare serials with no stamp.
_STAMP = re.compile(r"(?:^|_)(\d{8})[-_](\d{6})(?:\s|$)")

# Purged wholesale. reports/, icons/, screenshots/ are small and worth keeping.
PURGE_DIRS = ("file_backups", "apk_backups", "transfers")


def captured_at(entry):
    """When this backup was taken.

    Prefer the timestamp the app stamped into the name: adb pull can carry the
    original file's mtime across, and a 2019 holiday video pulled last Tuesday
    must not read as seven years old and vanish on the next sweep.
    """
    m = _STAMP.search(entry.name)
    if m:
        try:
            return datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
        except ValueError:
            pass
    return datetime.fromtimestamp(entry.stat().st_mtime)


def stale(root, now=None, days=KEEP_DAYS):
    """Top-level entries under root/<PURGE_DIRS> older than the cutoff."""
    now = now or datetime.now()
    cutoff = now - timedelta(days=days)
    out = []
    for name in PURGE_DIRS:
        folder = root / name
        if not folder.is_dir():
            continue
        for entry in sorted(folder.iterdir()):
            if captured_at(entry) < cutoff:
                out.append(entry)
    return out


def _size(entry):
    if entry.is_file():
        return entry.stat().st_size
    return sum(f.stat().st_size for f in entry.rglob("*") if f.is_file())


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    apply = "--apply" in argv
    days = KEEP_DAYS
    if "--days" in argv:
        days = int(argv[argv.index("--days") + 1])

    from adb import data_dir
    victims = stale(data_dir(), days=days)
    if not victims:
        print(f"Nothing older than {days} days. Nothing to do.")
        return 0

    freed = 0
    for entry in victims:
        size = _size(entry)
        freed += size
        print(f"{'DELETE' if apply else '  would'}  {size / 1e9:6.2f} GB  "
              f"{entry.parent.name}/{entry.name}")
        if apply:
            shutil.rmtree(entry) if entry.is_dir() else entry.unlink()

    verb = "Freed" if apply else "Would free"
    print(f"\n{verb} {freed / 1e9:.2f} GB across {len(victims)} entries.")
    if not apply:
        print("Dry run. Re-run with --apply to delete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
