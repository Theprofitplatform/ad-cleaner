import pytest

from protected import (
    extend_blocklist, is_blocked, is_from_known_store, is_protected, is_spoof,
    looks_like_junk, reset_blocklist,
)


@pytest.fixture(autouse=True)
def _fresh_blocklist():
    """extend_blocklist mutates module state; restore the seed after each test
    so tests can't leak blocklist entries into each other."""
    yield
    reset_blocklist()


def test_genuine_store_system_app_is_protected():
    assert is_protected("com.google.android.gms", "com.android.vending")
    assert is_protected("com.samsung.android.honeyboard", "com.sec.android.app.samsungapps")


def test_genuine_system_package_always_protected():
    assert is_protected("com.samsung.android.honeyboard", None, is_system=True)


def test_sideloaded_spoof_is_not_protected_and_is_spoof():
    # A system-looking name delivered by the generic sideload installer = impostor.
    inst = "com.google.android.packageinstaller"
    assert not is_protected("com.google.android.fakecore", inst)
    assert is_spoof("com.google.android.fakecore", inst)


def test_preinstalled_oem_app_is_protected_not_spoof():
    # Regression (real Samsung device): genuine preloads have a NULL or OEM-system
    # installer, never a store. They must be protected, never flagged as spoofs.
    assert is_protected("com.sec.android.app.kidshome", None)          # baked in
    assert not is_spoof("com.sec.android.app.kidshome", None)
    assert is_protected("com.sec.android.app.clockpackage",
                        "com.samsung.android.app.omcagent")            # Samsung OMC
    assert is_protected("com.samsung.android.heartplugin",
                        "com.sec.android.app.samsungapps")             # Galaxy Store


def test_ordinary_sideloaded_app_not_protected_not_spoof():
    assert not is_protected("com.random.adware", None)
    assert not is_spoof("com.random.adware", None)


def test_normal_store_app_not_protected():
    assert not is_protected("com.spotify.music", "com.android.vending")
    assert not is_spoof("com.spotify.music", "com.android.vending")


def test_known_store_detection():
    assert is_from_known_store("com.android.vending")
    assert is_from_known_store("com.amazon.venezia")
    assert not is_from_known_store("com.sketchy.sideload")
    assert not is_from_known_store(None)


def test_exact_essential_name_from_sideload_installer_is_spoof():
    assert is_protected("com.android.vending", "com.android.vending")
    # Store name delivered by the generic sideload installer -> spoof, not protected.
    assert not is_protected("com.android.vending", "com.google.android.packageinstaller")
    assert is_spoof("com.android.vending", "com.google.android.packageinstaller")


def test_looks_like_junk():
    assert looks_like_junk("com.phone.cleaner.shineapps")
    assert looks_like_junk("smart.cleaner.smart")
    assert looks_like_junk("com.sec.reclean")            # 'clean' substring
    assert looks_like_junk("com.d4rk.cleaner")
    assert not looks_like_junk("com.spotify.music")
    assert not looks_like_junk("com.google.android.apps.photos")
    assert not looks_like_junk("org.zwanoo.android.speedtest")  # 'speed' not a word
    # A legit app that merely contains 'wrapper' must NOT be flagged (bet365's
    # official package is com.bet365Wrapper.Bet365_Application).
    assert not looks_like_junk("com.bet365Wrapper.Bet365_Application")


def test_junk_named_system_lookalike_is_not_protected():
    # 'com.sec.reclean' matches the com.sec. prefix + store installer, which used
    # to whitelist it as a genuine Samsung app. A junk name overrides that.
    assert not is_protected("com.sec.reclean", "com.android.vending")
    # A real Samsung system package (no junk word) stays protected.
    assert is_protected("com.sec.android.app.kidshome", None)


def test_blocklist():
    assert is_blocked("com.cleanmaster.mguard")          # bundled seed
    assert not is_blocked("com.spotify.music")
    extend_blocklist(["com.some.fake.app", "# a comment", "  ",
                      "com.inline.commented   # trailing note"])
    assert is_blocked("com.some.fake.app")
    assert is_blocked("com.inline.commented")  # inline comment stripped
    # A blocklisted id is never protected even with a system-style name.
    extend_blocklist(["com.sec.somejunk"])
    assert not is_protected("com.sec.somejunk", "com.android.vending")


def test_reset_blocklist_drops_user_entries_keeps_seed():
    extend_blocklist(["com.mistake.entry"])
    reset_blocklist()
    assert not is_blocked("com.mistake.entry")
    assert is_blocked("com.cleanmaster.mguard")


def test_system_partition_beats_junk_and_blocklist():
    # Ordering pin: a genuine system-partition app keeps protection even with a
    # junk word in its name or a blocklist entry -- uninstalling it could brick
    # the phone, so is_system must win.
    assert is_protected("com.miui.cleaner", None, is_system=True)
    extend_blocklist(["com.oem.preinstalled.cleaner"])
    assert is_protected("com.oem.preinstalled.cleaner", None, is_system=True)


def test_stalkerware_is_never_protected():
    from stalkerware import is_stalkerware
    assert is_stalkerware("com.thetruthspy")
    assert not is_stalkerware("com.whatsapp")


def test_named_essentials_immune_to_blocklist():
    # A stray blocklist.txt line must never unprotect Play Store / SystemUI:
    # the junk/blocklist override is fenced to the prefix rule only.
    extend_blocklist(["com.android.vending", "com.android.systemui"])
    assert is_protected("com.android.vending", "com.android.vending")
    assert is_protected("com.android.systemui", None)
    # ...but a sideloaded impostor wearing the exact name is still a spoof.
    assert not is_protected("com.android.vending", "com.google.android.packageinstaller")
