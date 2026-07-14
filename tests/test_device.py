from pathlib import Path

import device
from device import GB, parse_battery, parse_df, parse_meminfo, read_device_stats

FIXTURES = Path(__file__).parent / "fixtures"


def fx(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_parse_meminfo():
    text = "MemTotal:        3906120 kB\nMemFree: 100 kB\nMemAvailable:    1953060 kB\n"
    total, avail = parse_meminfo(text)
    assert total == 3906120 * 1024
    assert avail == 1953060 * 1024


def test_parse_df():
    text = ("Filesystem     1K-blocks     Used Available Use% Mounted on\n"
            "/dev/block/dm-5 104857600 41943040  62914560  40% /data\n")
    total, used, free = parse_df(text)
    assert total == 104857600 * 1024
    assert used == 41943040 * 1024
    assert free == 62914560 * 1024


def test_parse_df_unparsable_returns_zeros():
    assert parse_df("garbage output") == (0, 0, 0)


def test_parse_battery():
    temp, level = parse_battery("  level: 85\n  temperature: 305\n  health: 2\n")
    assert temp == 30.5
    assert level == 85


def test_parse_battery_missing():
    assert parse_battery("no fields here") == (None, None)


class FakeAdb:
    serial = "T"

    def shell_text(self, args, timeout=10):
        if args == ["cat", "/proc/meminfo"]:
            return "MemTotal: 4194304 kB\nMemAvailable: 1048576 kB\n"
        if args == ["df", "/data"]:
            return ("Filesystem 1K-blocks Used Available Use% Mounted on\n"
                    "/dev/x 104857600 52428800 52428800 50% /data\n")
        if args == ["dumpsys", "battery"]:
            return "  level: 77\n  temperature: 281\n"
        return ""


def test_read_device_stats():
    s = read_device_stats(FakeAdb())
    assert s["ram_total_gb"] == 4.0
    assert s["ram_used_gb"] == 3.0        # 4 GB total - 1 GB available
    assert s["ram_pct"] == 75
    assert s["storage_total_gb"] == round(104857600 * 1024 / GB, 1)
    assert s["storage_pct"] == 50
    assert s["battery_temp_c"] == 28.1
    assert s["battery_level"] == 77


def test_parse_uid_map():
    m = device.parse_uid_map(fx("packages_uids.txt"))
    assert m == {"u0a231": "com.random.freegift", "u0a145": "com.whatsapp"}


def test_parse_power_use_ranks_uids():
    top = device.parse_power_use(fx("batterystats.txt"))
    assert top[0] == ("u0a231", 145.0)
    assert ("u0a145", 40.2) in top
    assert device.parse_power_use("") == []


def test_parse_data_use_sums_buckets_per_uid():
    use = device.parse_data_use(fx("netstats.txt"))
    assert use[10231] == 52428800 + 1048576 + 31457280 + 524288
    assert use[10145] == 2097152 + 1024
    assert device.parse_data_use("") == {}


def test_parse_usage_minutes():
    use = device.parse_usage_minutes(fx("usagestats.txt"))
    assert use["com.whatsapp"] == 62
    assert use["com.random.freegift"] == 4
    assert device.parse_usage_minutes("junk") == {}
