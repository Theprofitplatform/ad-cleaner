"""Screen mirroring via scrcpy — see and control the phone from the PC.

The repair-bench killer feature: a phone with a smashed digitizer or dead
screen can still be driven with the PC mouse/keyboard (as long as USB
debugging was authorised). scrcpy is a self-contained folder like
platform-tools: find_scrcpy locates it (bundled, next to the app, or
downloaded once into adcleaner_data); download_scrcpy fetches it.
"""

import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

from adb import app_dir, data_dir

# ponytail: pinned release, not "latest" — GitHub's latest-URL redirects are
# per-asset-name and scrcpy renames its zip every version. Bump when needed.
SCRCPY_VERSION = "4.1"     # 3.x servers crash on Android 16
SCRCPY_URL = (f"https://github.com/Genymobile/scrcpy/releases/download/"
              f"v{SCRCPY_VERSION}/scrcpy-win64-v{SCRCPY_VERSION}.zip")

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def find_scrcpy(base=None):
    """Locate scrcpy.exe: bundled-in-exe, ./scrcpy, adcleaner_data/scrcpy."""
    base = base or app_dir()
    candidates = [
        base / "scrcpy" / "scrcpy.exe",
        base / "adcleaner_data" / "scrcpy" / "scrcpy.exe",
    ]
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.insert(0, Path(meipass) / "scrcpy" / "scrcpy.exe")
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return shutil.which("scrcpy")


def download_scrcpy(progress=None, dest=None):
    """One-time scrcpy download into adcleaner_data/scrcpy. Returns the path
    to scrcpy.exe. `progress(fraction)` gets 0.0..1.0. Raises on failure."""
    dest = Path(dest) if dest else data_dir()
    tmp = dest / "scrcpy.zip"
    req = urllib.request.Request(SCRCPY_URL, headers={"User-Agent": "AdCleaner"})
    with urllib.request.urlopen(req, timeout=60) as resp, open(tmp, "wb") as out:
        total = int(resp.headers.get("Content-Length") or 0)
        read = 0
        while True:
            chunk = resp.read(64 * 1024)
            if not chunk:
                break
            out.write(chunk)
            read += len(chunk)
            if progress and total:
                progress(min(read / total, 1.0))
    with zipfile.ZipFile(tmp) as zf:
        zf.extractall(dest)          # contains scrcpy-win64-vX.Y.Z/
    tmp.unlink()
    inner = dest / f"scrcpy-win64-v{SCRCPY_VERSION}"
    target = dest / "scrcpy"
    if target.exists():
        shutil.rmtree(target)
    inner.rename(target)
    exe = target / "scrcpy.exe"
    if not exe.exists():
        raise RuntimeError("Download finished but scrcpy.exe was not in the zip.")
    return str(exe)


def launch(scrcpy_path, adb_path, serial, title="Phone"):
    """Start scrcpy detached and return the Popen. ADB env points scrcpy at
    our adb.exe so two different adb builds don't kill each other's server."""
    return subprocess.Popen(
        [scrcpy_path, "--serial", serial, "--window-title", title,
         "--stay-awake"],
        env={**os.environ, "ADB": adb_path},
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=_NO_WINDOW)
