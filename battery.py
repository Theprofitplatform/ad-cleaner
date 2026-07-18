"""Per-app battery usage over ADB, in plain English.

Read-only, no root. Two sources, joined on UID:
  * dumpsys batterystats --charged   -- estimated mAh per uid since last full charge
  * cmd package list packages -U     -- uid -> package name mapping

Android has no true rolling "last 24 hours" counter over ADB: batterystats
accumulates since the last full charge (or a manual --reset). We report that
window honestly rather than fake a 24h figure.

Pure parsers (unit-tested against fixtures) plus a thin read_battery_report that
drives an Adb object. All device I/O lives in adb.py.
"""

import re
from dataclasses import dataclass

# A few well-known Android system UIDs -> a name a person recognises. Anything
# else numeric is shown as "System (uid N)"; app UIDs resolve to the package.
SYSTEM_UIDS = {
    0: "Android core",
    1000: "Android system",
    1001: "Phone / cellular",
    1002: "Bluetooth",
    1010: "Wi-Fi",
    1013: "Media server",
    1019: "DRM service",
    1027: "NFC",
    2000: "Developer shell",
}

# u<user>a<appid>  ->  numeric uid (user*100000 + 10000 + appid). e.g. u0a154 -> 10154.
_UID_RE = re.compile(r"^u(\d+)a(\d+)$")
_PKG_RE = re.compile(r"package:(\S+)\s+uid:(\d+)")


@dataclass
class BatteryUse:
    name: str      # package ("com.foo") or friendly label ("Screen", "Android system")
    mah: float     # estimated milliamp-hours since last full charge
    is_app: bool   # True for installed-app UIDs -- the adware candidates


def _uid_to_num(token):
    """'u0a154' -> 10154, '1000' -> 1000, anything else -> None."""
    m = _UID_RE.match(token)
    if m:
        return int(m.group(1)) * 100000 + 10000 + int(m.group(2))
    if token.isdigit():
        return int(token)
    return None


def parse_packages(text):
    """`cmd package list packages -U` -> {uid: package}. Lines look like
    `package:com.android.chrome uid:10154`. A uid can host several packages
    (shared uid); last one wins, which is fine for a display label."""
    out = {}
    for line in (text or "").splitlines():
        m = _PKG_RE.search(line)
        if m:
            out[int(m.group(2))] = m.group(1)
    return out


def parse_power_use(text):
    """`dumpsys batterystats --charged` -> {capacity, computed, entries}.

    entries is [(kind, name, mah)] where kind is 'uid' (name is a uid token like
    'u0a154' or '1000') or 'misc' (name is a label like 'Screen'/'Idle'). We
    read only the indented block under 'Estimated power use (mAh):' and stop when
    indentation returns to the header's level (start of the next section)."""
    header_indent = None
    entries = []
    capacity = computed = None
    for line in (text or "").splitlines():
        if header_indent is None:
            if "Estimated power use (mAh):" in line:
                header_indent = len(line) - len(line.lstrip())
            continue
        if not line.strip():
            continue
        if (len(line) - len(line.lstrip())) <= header_indent:
            break  # dedented -> left the block
        s = line.strip()
        mcap = re.match(r"Capacity:\s*([\d.]+),\s*Computed drain:\s*([\d.]+)", s)
        if mcap:
            capacity, computed = float(mcap.group(1)), float(mcap.group(2))
            continue
        muid = re.match(r"Uid\s+(\S+?):\s*([\d.]+)", s)
        if muid:
            entries.append(("uid", muid.group(1), float(muid.group(2))))
            continue
        mlbl = re.match(r"([A-Za-z][\w /()-]*?):\s*([\d.]+)", s)
        if mlbl:
            entries.append(("misc", mlbl.group(1), float(mlbl.group(2))))
    return {"capacity": capacity, "computed": computed, "entries": entries}


def build_uses(power, pkgmap):
    """Join parsed power entries with the uid->package map -> [BatteryUse],
    biggest drain first."""
    out = []
    for kind, name, mah in power["entries"]:
        if kind == "misc":
            out.append(BatteryUse(name, mah, False))
            continue
        num = _uid_to_num(name)
        pkg = pkgmap.get(num)
        if pkg:
            out.append(BatteryUse(pkg, mah, True))
        elif num in SYSTEM_UIDS:
            out.append(BatteryUse(SYSTEM_UIDS[num], mah, False))
        else:
            out.append(BatteryUse(f"System (uid {name})", mah, False))
    out.sort(key=lambda u: u.mah, reverse=True)
    return out


def summarize(uses, computed):
    """One-line verdict + banner kind ('good'/'warn') for a list of uses."""
    if not uses:
        return ("❓  No battery-usage data yet — unplug the phone and use it a "
                "while, then check again.", "warn")
    total = computed or sum(u.mah for u in uses) or 1.0
    apps = [u for u in uses if u.is_app]
    if not apps:
        return ("✅  No third-party app is using notable battery since the last "
                "full charge.", "good")
    top = apps[0]
    share = 100 * top.mah / total
    if share >= 25:
        return (f"⚠️  {top.name} has used {share:.0f}% of the battery since the last "
                f"full charge — unusually high for one app. Worth a look.", "warn")
    return (f"✅  Battery use looks normal. Heaviest app: {top.name} "
            f"({share:.0f}% since last full charge).", "good")


def read_battery_report(adb):
    """Drive an Adb object; return {capacity, computed, uses}."""
    try:
        raw = adb.shell_text(["dumpsys", "batterystats", "--charged"], timeout=60)
    except Exception:
        raw = ""
    try:
        pkgs = adb.shell_text(["cmd", "package", "list", "packages", "-U"], timeout=30)
    except Exception:
        pkgs = ""
    power = parse_power_use(raw)
    return {"capacity": power["capacity"], "computed": power["computed"],
            "uses": build_uses(power, parse_packages(pkgs))}


def demo():
    stats = (
        "Statistics since last charge:\n"
        "  Estimated power use (mAh):\n"
        "    Capacity: 4000, Computed drain: 500, actual drain: 480-520\n"
        "    Screen: 150 Excluded from smearing\n"
        "    Uid u0a154: 200 ( cpu=180 wifi=20 )\n"
        "    Uid 1000: 40.5 ( cpu=30 sensor=10.5 )\n"
        "    Cell standby: 25 ( radio=25 )\n"
        "    Uid u0a97: 8.6 ( cpu=8.6 )\n"
        "    Idle: 15\n"
        "  Per-app mobile ms per packet:\n"          # next section -> must stop here
        "    Uid u0a999: 9999\n"
    )
    pkgs = ("package:com.android.chrome uid:10154\n"
            "package:com.sneaky.adware uid:10097\n")
    power = parse_power_use(stats)
    assert power["computed"] == 500 and power["capacity"] == 4000, power
    # The next-section line must not leak in.
    assert all(n != "u0a999" for _, n, _ in power["entries"]), power["entries"]
    uses = build_uses(power, parse_packages(pkgs))
    assert uses[0].name == "com.android.chrome" and uses[0].is_app          # biggest first
    assert uses[0].mah == 200
    assert any(u.name == "Android system" and not u.is_app for u in uses)   # uid 1000 labelled
    assert any(u.name == "Screen" and not u.is_app for u in uses)           # misc kept
    text, kind = summarize(uses, power["computed"])
    assert kind == "warn" and "40%" in text, text   # chrome 200/500 = 40%
    assert summarize([], None)[1] == "warn"
    ok, kind2 = summarize([BatteryUse("Screen", 150, False)], 500)
    assert kind2 == "good"
    print("battery.py demo OK")


if __name__ == "__main__":
    demo()
