"""Device maintenance stats: storage, RAM, battery temperature (via ADB).

Pure parsers (tested with fixtures) + a thin read_device_stats(adb) that runs
the shell commands and returns display-ready numbers.
"""

import re

GB = 1024 ** 3


def parse_meminfo(text):
    """/proc/meminfo -> (total_bytes, available_bytes)."""
    def kb(field):
        m = re.search(rf"^{field}:\s+(\d+)\s*kB", text, re.MULTILINE)
        return int(m.group(1)) * 1024 if m else 0
    return kb("MemTotal"), kb("MemAvailable")


def parse_df(text):
    """`df /data` -> (total_bytes, used_bytes, free_bytes). 0s if unparsable."""
    for line in text.splitlines():
        f = line.split()
        if len(f) >= 6 and f[-1] == "/data" and f[-5].isdigit():
            total, used, avail = (int(f[-5]) * 1024, int(f[-4]) * 1024,
                                  int(f[-3]) * 1024)
            return total, used, avail
    return 0, 0, 0


def parse_battery(text):
    """`dumpsys battery` -> (temp_celsius or None, level_pct or None)."""
    t = re.search(r"temperature:\s*(-?\d+)", text)
    lvl = re.search(r"level:\s*(\d+)", text)
    temp = int(t.group(1)) / 10.0 if t else None
    level = int(lvl.group(1)) if lvl else None
    return temp, level


def _safe(adb, args):
    try:
        return adb.shell_text(args)
    except Exception:
        return ""


def parse_uid_map(text):
    """`pm list packages -U` -> {'u0a231': package}. uid 10231 == u0a231."""
    out = {}
    for m in re.finditer(r"package:(\S+)\s+uid:(\d+)", text or ""):
        uid = int(m.group(2))
        if uid >= 10000:
            out[f"u0a{uid - 10000}"] = m.group(1)
    return out


def parse_power_use(text):
    """'Estimated power use' section of `dumpsys batterystats --charged`
    -> [(uid_str, mAh)] descending. Labels like 'Screen' are dropped."""
    rows = []
    for m in re.finditer(r"^\s+Uid (u0a\d+):\s+([\d.]+)", text or "", re.MULTILINE):
        rows.append((m.group(1), float(m.group(2))))
    return sorted(rows, key=lambda r: -r[1])


def read_battery_report(adb, uid_map=None):
    stats = _safe(adb, ["dumpsys", "batterystats", "--charged"])
    if uid_map is None:
        uid_map = parse_uid_map(_safe(adb, ["pm", "list", "packages", "-U", "-3"]))
    top = [(uid_map.get(uid, uid), mah)
           for uid, mah in parse_power_use(stats) if uid in uid_map][:5]
    asoc = re.search(r"mSavedBatteryAsoc:\s*(\d+)", _safe(adb, ["dumpsys", "battery"]))
    return {"top_drainers": top, "health_pct": int(asoc.group(1)) if asoc else None}


def read_device_stats(adb):
    """Run the maintenance queries and return display-ready values."""
    total_ram, avail_ram = parse_meminfo(_safe(adb, ["cat", "/proc/meminfo"]))
    d_total, d_used, d_free = parse_df(_safe(adb, ["df", "/data"]))
    temp, level = parse_battery(_safe(adb, ["dumpsys", "battery"]))

    def gb(n):
        return round(n / GB, 1)

    return {
        "ram_total_gb": gb(total_ram),
        "ram_used_gb": gb(total_ram - avail_ram) if total_ram else 0,
        "ram_pct": round(100 * (total_ram - avail_ram) / total_ram) if total_ram else 0,
        "storage_total_gb": gb(d_total),
        "storage_used_gb": gb(d_used),
        "storage_free_gb": gb(d_free),
        "storage_pct": round(100 * d_used / d_total) if d_total else 0,
        "battery_temp_c": temp,
        "battery_level": level,
    }


def demo():
    mem = "MemTotal:        3906120 kB\nMemFree: 100 kB\nMemAvailable:    1953060 kB\n"
    total, avail = parse_meminfo(mem)
    assert total == 3906120 * 1024 and avail == 1953060 * 1024

    df = ("Filesystem     1K-blocks     Used Available Use% Mounted on\n"
          "/dev/block/dm-5 104857600 41943040  62914560  40% /data\n")
    t, u, a = parse_df(df)
    assert (t, u, a) == (104857600 * 1024, 41943040 * 1024, 62914560 * 1024)

    temp, level = parse_battery("  level: 85\n  temperature: 305\n  health: 2\n")
    assert temp == 30.5 and level == 85
    print("device.py demo OK")


if __name__ == "__main__":
    demo()
