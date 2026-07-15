"""Device maintenance stats: storage, RAM, battery temperature (via ADB).

Pure parsers (tested with fixtures) + a thin read_device_stats(adb) that runs
the shell commands and returns display-ready numbers.
"""

import json
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


def parse_data_use(text):
    """`dumpsys netstats` bucket lines -> {uid: total rx+tx bytes}. Defensive:
    OEM formats vary; anything that doesn't match the uid/rb/tb shape is skipped."""
    use = {}
    for m in re.finditer(r"uid=(\d+)\b[^\n]*?\brb=(\d+)[^\n]*?\btb=(\d+)",
                         text or ""):
        uid = int(m.group(1))
        use[uid] = use.get(uid, 0) + int(m.group(2)) + int(m.group(3))
    return use


def parse_usage_minutes(text):
    """`dumpsys usagestats` -> {package: foreground minutes}. Format is
    unstable across OEMs; matches both h:mm:ss and mm:ss time strings and
    keeps the largest value seen per package."""
    use = {}
    for m in re.finditer(
            r'package=(\S+)[^\n]*?totalTime(?:Used|Visible)="(?:(\d+):)?(\d+):(\d+)"',
            text or ""):
        pkg = m.group(1)
        h = int(m.group(2) or 0)
        mins = h * 60 + int(m.group(3))
        use[pkg] = max(use.get(pkg, 0), mins)
    return use


def read_battery_report(adb, uid_map=None):
    stats = _safe(adb, ["dumpsys", "batterystats", "--charged"])
    if uid_map is None:
        uid_map = parse_uid_map(_safe(adb, ["pm", "list", "packages", "-U", "-3"]))
    top = [(uid_map.get(uid, uid), mah)
           for uid, mah in parse_power_use(stats) if uid in uid_map][:5]
    asoc = re.search(r"mSavedBatteryAsoc:\s*(\d+)", _safe(adb, ["dumpsys", "battery"]))
    return {"top_drainers": top, "health_pct": int(asoc.group(1)) if asoc else None}


def parse_cpu_by_app(text):
    """`dumpsys cpuinfo` -> [(name, cpu_pct)] descending, recent-minutes window.
    Only app-looking processes (name contains a dot); kernel threads and native
    daemons are noise here. ':remote'-style subprocesses merge into their app."""
    use = {}
    for m in re.finditer(r"^\s*([\d.]+)%\s+\d+/([^\s:]+)(?::\S+)?:",
                         text or "", re.MULTILINE):
        name = m.group(2)
        if "." not in name:
            continue
        use[name] = use.get(name, 0.0) + float(m.group(1))
    return sorted(use.items(), key=lambda r: -r[1])


def parse_pss_by_app(text):
    """`dumpsys meminfo` 'Total PSS by process' section -> [(name, bytes)]
    descending, subprocesses merged, dot-less system daemons dropped."""
    rows = {}
    sect = (text or "").split("Total PSS by process", 1)
    if len(sect) < 2:
        return []
    for line in sect[1].splitlines():
        m = re.match(r"\s+([\d,]+)K:\s+(\S+?)(?::\S+)?\s+\(", line)
        if m:
            name = m.group(2)
            if "." in name:
                rows[name] = rows.get(name, 0) + int(m.group(1).replace(",", "")) * 1024
        elif line.strip().startswith("Total PSS by"):
            break
    return sorted(rows.items(), key=lambda r: -r[1])


def parse_diskstats(text):
    """`dumpsys diskstats` -> ([(package, app_bytes, data_bytes, cache_bytes)],
    free_bytes, total_bytes). Some OEMs omit the per-package arrays -> ([], f, t)."""
    def arr(label):
        m = re.search(rf"^{label}: (\[.*?\])\s*$", text or "", re.MULTILINE)
        try:
            return json.loads(m.group(1)) if m else []
        except ValueError:
            return []
    names = arr("Package Names")
    app, data, cache = arr("App Sizes"), arr("App Data Sizes"), arr("Cache Sizes")
    rows = []
    if names and len(names) == len(app) == len(data) == len(cache):
        rows = [(n, int(a), int(d), int(c))
                for n, a, d, c in zip(names, app, data, cache)]
    free = total = 0
    m = re.search(r"Data-Free:\s*(\d+)K\s*/\s*(\d+)K", text or "")
    if m:
        free, total = int(m.group(1)) * 1024, int(m.group(2)) * 1024
    return rows, free, total


def read_resource_report(adb, top=10):
    """The three hog lists for the 'What's using this phone?' window."""
    cpu = parse_cpu_by_app(_safe(adb, ["dumpsys", "cpuinfo"]))[:top]
    ram = parse_pss_by_app(_safe(adb, ["dumpsys", "meminfo"]))[:top]
    rows, free, total = parse_diskstats(_safe(adb, ["dumpsys", "diskstats"]))
    agg = {}
    for n, a, d, c in rows:      # multi-user phones repeat packages: sum them
        pa, pd, pc = agg.get(n, (0, 0, 0))
        agg[n] = (pa + a, pd + d, pc + c)
    storage = sorted(((n, a + d + c, d, c) for n, (a, d, c) in agg.items()),
                     key=lambda r: -r[1])[:top]
    return {"cpu": cpu, "ram": ram, "storage": storage,
            "disk_free": free, "disk_total": total}


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

    use = parse_data_use("uid=10231 set=DEFAULT rb=100 tb=50\n")
    assert use == {10231: 150} and parse_data_use("") == {}

    cpu = parse_cpu_by_app(
        "Load: 1.0 / 1.0 / 1.0\n"
        "  8.1% 2975/system_server: 5.4% user + 2.7% kernel\n"
        "  0.7% 12350/com.foo.bar: 0.5% user + 0.2% kernel\n"
        "  0.3% 11670/com.foo.bar:remote: 0.1% user + 0.1% kernel\n"
        "  0.5% 22744/kworker/0:4-pm: 0% user + 0.5% kernel\n")
    assert cpu == [("com.foo.bar", 1.0)]        # merged, daemons dropped

    pss = parse_pss_by_app(
        "header\nTotal PSS by process:\n"
        "    100,000K: com.a.big (pid 1 / activities)\n"
        "    2,000K: com.a.big:push (pid 2)\n"
        "    50,000K: surfaceflinger (pid 3)\n"
        "\nTotal PSS by category:\n    999,999K: junk (pid 9)\n")
    assert pss == [("com.a.big", 102_000 * 1024)]
    assert parse_pss_by_app("no such section") == []

    rows, free, total = parse_diskstats(
        'Data-Free: 100K / 200K total = 50% free\n'
        'Package Names: ["com.a", "com.b"]\n'
        'App Sizes: [10, 20]\nApp Data Sizes: [1, 2]\nCache Sizes: [5, 6]\n')
    assert rows == [("com.a", 10, 1, 5), ("com.b", 20, 2, 6)]
    assert (free, total) == (100 * 1024, 200 * 1024)
    assert parse_diskstats("Package Names: [\"com.a\"]\nApp Sizes: [1, 2]\n") == ([], 0, 0)
    print("device.py demo OK")


if __name__ == "__main__":
    demo()
