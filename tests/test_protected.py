from protected import is_from_known_store, is_protected, is_spoof


def test_genuine_store_system_app_is_protected():
    assert is_protected("com.google.android.gms", "com.android.vending")
    assert is_protected("com.samsung.android.honeyboard", "com.sec.android.app.samsungapps")


def test_genuine_system_package_always_protected():
    assert is_protected("com.samsung.android.honeyboard", None, is_system=True)


def test_sideloaded_spoof_is_not_protected_and_is_spoof():
    # Malware wearing a system-looking name must stay actionable.
    assert not is_protected("com.google.android.fakecore", None)
    assert is_spoof("com.google.android.fakecore", None)


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


def test_exact_essential_names_protected_only_from_store():
    assert is_protected("com.android.vending", "com.android.vending")
    # Same name, sideloaded -> a spoof, not protected.
    assert not is_protected("com.android.vending", None)
