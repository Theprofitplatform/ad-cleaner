"""ADB wrapper: locate adb.exe, run commands, parse `devices` (BUILD_PLAN 4.1).

Every call has a timeout and never opens a console window (so the packaged
--windowed exe stays silent). Failures raise AdbError; callers surface them in
the status bar rather than crashing.
"""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

DEFAULT_TIMEOUT = 10

# Hide the child console window on Windows (no-op elsewhere).
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


class AdbError(Exception):
    """Any ADB failure, already mapped to a friendly message."""


def app_dir():
    """Folder the app lives in (next to the exe when frozen, else this file)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


def data_dir():
    d = app_dir() / "adcleaner_data"
    d.mkdir(exist_ok=True)
    return d


def find_adb(base=None):
    """Locate adb.exe: bundled-in-exe, ./platform-tools, adcleaner_data, PATH."""
    base = base or app_dir()
    exe = "adb.exe" if os.name == "nt" else "adb"
    candidates = [
        base / "platform-tools" / exe,
        base / "adcleaner_data" / "platform-tools" / exe,
    ]
    meipass = getattr(sys, "_MEIPASS", None)  # PyInstaller onefile bundle dir
    if meipass:
        candidates.insert(0, Path(meipass) / "platform-tools" / exe)
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return shutil.which("adb")


class Adb:
    def __init__(self, adb_path, serial=None):
        self.adb_path = adb_path
        self.serial = serial

    def _cmd(self, args):
        cmd = [self.adb_path]
        if self.serial:
            cmd += ["-s", self.serial]
        return cmd + list(args)

    def run(self, args, timeout=DEFAULT_TIMEOUT):
        """Run `adb [-s serial] <args>`; return stdout. Raise AdbError on failure."""
        try:
            proc = subprocess.run(
                self._cmd(args),
                capture_output=True, text=True, timeout=timeout,
                creationflags=_NO_WINDOW,
            )
        except subprocess.TimeoutExpired:
            raise AdbError(f"Command timed out after {timeout}s")
        except FileNotFoundError:
            raise AdbError("adb.exe not found")
        if proc.returncode != 0:
            raise AdbError(_friendly(proc.stderr or proc.stdout))
        return proc.stdout

    def shell_text(self, args, timeout=DEFAULT_TIMEOUT):
        return self.run(["shell"] + list(args), timeout)

    def start_server(self):
        try:
            self.run(["start-server"], timeout=20)
        except AdbError:
            pass  # devices() will report the real state

    def devices(self):
        """Parse `adb devices -l` -> [{serial, state, model}]."""
        out = self.run(["devices", "-l"], timeout=DEFAULT_TIMEOUT)
        return parse_devices(out)

    def get_prop(self, prop, timeout=DEFAULT_TIMEOUT):
        return self.shell_text(["getprop", prop], timeout).strip()


def parse_devices(output):
    devices = []
    for line in output.splitlines():
        line = line.rstrip()
        if not line or line.startswith("List of devices"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        serial, state = parts[0], parts[1]
        model = ""
        m = re.search(r"\bmodel:(\S+)", line)
        if m:
            model = m.group(1).replace("_", " ")
        devices.append({"serial": serial, "state": state, "model": model})
    return devices


def _friendly(stderr):
    """Map known ADB stderr to plain English (BUILD_PLAN 8)."""
    s = (stderr or "").lower()
    if "unauthorized" in s:
        return "Phone not authorized. Tap 'Allow' on the phone."
    if "offline" in s:
        return "Phone is offline. Unplug and replug the cable."
    if "no devices" in s or "device not found" in s:
        return "No phone detected."
    if "not found" in s or "unknown package" in s or "not installed" in s:
        return "That app is no longer installed."
    if "protected" in s or "permission" in s or "not allowed" in s:
        return "The phone refused that action."
    return (stderr or "ADB command failed").strip().splitlines()[0][:200]


def demo():
    sample = (
        "List of devices attached\n"
        "R58N12ABCDE   device usb:1-3 product:o1sxx model:SM_G991B device:o1s transport_id:1\n"
        "EFGH5678      unauthorized usb:1-4 transport_id:2\n"
    )
    devs = parse_devices(sample)
    assert devs[0] == {"serial": "R58N12ABCDE", "state": "device", "model": "SM G991B"}, devs
    assert devs[1]["state"] == "unauthorized"
    assert "not authorized" in _friendly("error: device unauthorized").lower()
    print("adb.py demo OK")


if __name__ == "__main__":
    demo()
