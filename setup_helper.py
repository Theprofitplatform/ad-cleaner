"""First-run checks + ADB download helper (BUILD_PLAN 4.1).

The zip download is the ONLY network call the app ever makes.
"""

import io
import urllib.request
import zipfile
from pathlib import Path

from adb import data_dir, find_adb

PLATFORM_TOOLS_URL = (
    "https://dl.google.com/android/repository/platform-tools-latest-windows.zip"
)


def adb_available():
    return find_adb() is not None


def download_platform_tools(progress=None, dest=None):
    """Download Google Platform Tools and unzip next to the app.

    `progress(fraction)` is called 0.0..1.0 during download. Returns the path to
    the unzipped adb.exe. Raises on network/zip failure (caller shows a dialog).
    """
    dest = Path(dest) if dest else data_dir()
    buf = io.BytesIO()
    req = urllib.request.Request(PLATFORM_TOOLS_URL, headers={"User-Agent": "AdCleaner"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        total = int(resp.headers.get("Content-Length") or 0)
        read = 0
        while True:
            chunk = resp.read(64 * 1024)
            if not chunk:
                break
            buf.write(chunk)
            read += len(chunk)
            if progress and total:
                progress(min(read / total, 1.0))
    if progress:
        progress(1.0)

    with zipfile.ZipFile(buf) as zf:
        zf.extractall(dest)  # zip contains a top-level platform-tools/ folder

    adb_path = dest / "platform-tools" / "adb.exe"
    if not adb_path.exists():
        raise RuntimeError("Download finished but adb.exe was not found in the zip.")
    return str(adb_path)
