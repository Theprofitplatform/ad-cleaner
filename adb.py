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
                # Android emits UTF-8; decode as such (not the Windows cp1252 locale)
                # and replace stray bytes so a scan never dies on a non-Latin app name.
                capture_output=True, encoding="utf-8", errors="replace",
                timeout=timeout, creationflags=_NO_WINDOW,
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

    def run_bytes(self, args, timeout=DEFAULT_TIMEOUT):
        """Like run() but returns raw stdout bytes (for binary output)."""
        try:
            proc = subprocess.run(self._cmd(args), capture_output=True,
                                  timeout=timeout, creationflags=_NO_WINDOW)
        except subprocess.TimeoutExpired:
            raise AdbError(f"Command timed out after {timeout}s")
        except FileNotFoundError:
            raise AdbError("adb.exe not found")
        if proc.returncode != 0:
            raise AdbError(_friendly((proc.stderr or b"").decode("utf-8", "ignore")))
        return proc.stdout

    def screencap(self, timeout=20):
        """Return a PNG screenshot of the phone screen as bytes.

        `exec-out` (not `shell`) keeps the binary stream free of CRLF mangling.
        """
        return self.run_bytes(["exec-out", "screencap", "-p"], timeout=timeout)

    def pull(self, remote, local, timeout=120):
        return self.run(["pull", remote, local], timeout=timeout)

    def push(self, local, remote, timeout=120):
        return self.run(["push", local, remote], timeout=timeout)

    def reboot(self, timeout=DEFAULT_TIMEOUT):
        return self.run(["reboot"], timeout=timeout)


def wifi_connect(adb, connect_hostport, pair_hostport="", code=""):
    """Pair (first time) then connect to a phone over Wi-Fi — Android 11+
    'Wireless debugging'. Returns (ok, message). `adb connect` exits 0 even
    when it fails, so success is judged from the output text, not the exit
    code. Once connected the phone shows up in `adb devices` with an
    ip:port serial and the normal poll takes over."""
    if bool(pair_hostport) != bool(code):
        return False, ("To pair, fill in both the pairing address and the "
                       "pairing code (or leave both empty if already paired).")
    if pair_hostport and code:
        try:
            out = adb.run(["pair", pair_hostport, code], timeout=30)
        except AdbError as e:
            return False, str(e)
        if "paired" not in out.lower() or "failed" in out.lower():
            return False, out.strip() or "Pairing failed — check the code and address."
    try:
        out = adb.run(["connect", connect_hostport], timeout=30)
    except AdbError as e:
        return False, str(e)
    low = out.lower()
    ok = "connected to" in low and "failed" not in low and "cannot" not in low
    return ok, out.strip()


_MDNS_ADDR_RE = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3}:\d+)\b")


def parse_mdns_services(output):
    """`adb mdns services` output -> {'connect': [ip:port, ...], 'pairing': [...]}.

    Lines look like `adb-R58N…-x  _adb-tls-connect._tcp  192.168.1.9:37099`;
    the service-type spelling drifts across adb versions, so classify on the
    'pairing' substring and keep anything else with an address as a connect
    candidate."""
    found = {"connect": [], "pairing": []}
    for line in (output or "").splitlines():
        if "_adb" not in line:
            continue
        m = _MDNS_ADDR_RE.search(line)
        if not m:
            continue
        kind = "pairing" if "pairing" in line else "connect"
        if m.group(1) not in found[kind]:
            found[kind].append(m.group(1))
    return found


def mdns_discover(adb):
    """Phones advertising Wireless debugging on this network, via adb's own
    mDNS. Best effort: empty lists when mDNS is off or the query fails."""
    try:
        out = adb.run(["mdns", "services"], timeout=10)
    except AdbError:
        out = ""
    return parse_mdns_services(out)


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

    class _F:
        def run(self, args, timeout=10):
            return {"pair": "Successfully paired to h [guid]",
                    "connect": "connected to 1.2.3.4:5555"}[args[0]]
    ok, _ = wifi_connect(_F(), "1.2.3.4:5555", "1.2.3.4:4444", "123456")
    assert ok

    mdns = parse_mdns_services(
        "List of discovered mdns services\n"
        "adb-R58N-x\t_adb-tls-connect._tcp\t192.168.1.9:37099\n"
        "adb-R58N-x\t_adb-tls-pairing._tcp\t192.168.1.9:41234\n")
    assert mdns == {"connect": ["192.168.1.9:37099"],
                    "pairing": ["192.168.1.9:41234"]}, mdns
    print("adb.py demo OK")


if __name__ == "__main__":
    demo()
