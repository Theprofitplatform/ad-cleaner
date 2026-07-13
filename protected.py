"""Protected-package rules (BUILD_PLAN 4.5).

Safety boundary: the app only ever operates on third-party packages
(`pm list packages -3`). On top of that, some third-party-looking packages are
still protected because they are genuine system/essential apps. The one twist:
a *sideloaded* app whose name copies a protected prefix is malware pretending to
be a system app -- it must NOT be protected.
"""

# App stores we trust. A package installed by one of these is genuine.
KNOWN_STORES = frozenset({
    "com.android.vending",                  # Google Play
    "com.sec.android.app.samsungapps",      # Samsung Galaxy Store
    "com.amazon.venezia",                   # Amazon Appstore
    "com.heytap.market",                    # Oppo/Realme
    "com.xiaomi.mipicks",                   # Xiaomi
})

# Prefixes owned by the OS / OEM. Only honoured for store-installed or genuine
# system packages (see is_protected) -- never for sideloaded impersonators.
PROTECTED_PREFIXES = (
    "com.android.",
    "com.google.android.",
    "com.samsung.",
    "com.sec.",
    "com.qualcomm.",
    "com.osp.",
)

# Essentials protected by exact name (all also covered by prefixes, listed
# explicitly for clarity and so the list is easy to extend).
PROTECTED_EXACT = frozenset({
    "com.android.vending",                        # Play Store
    "com.google.android.gms",                     # Google Play services
    "com.google.android.gsf",                     # Google services framework
    "com.samsung.android.messaging",              # Samsung Messages
    "com.samsung.android.honeyboard",             # Samsung Keyboard
    "com.google.android.inputmethod.latin",       # Gboard
    "com.sec.android.app.launcher",               # Samsung Home
    "com.google.android.dialer",                  # Phone
    "com.samsung.android.dialer",
    "com.android.settings",
    "com.android.systemui",
})


def is_from_known_store(installer):
    """True if the installer package is a trusted app store."""
    return installer in KNOWN_STORES


def looks_like_protected_name(package):
    """True if the package name uses an OS/OEM prefix or is a named essential."""
    return package in PROTECTED_EXACT or package.startswith(PROTECTED_PREFIXES)


def is_spoof(package, installer, is_system=False):
    """A sideloaded app wearing a protected name = malware impersonation."""
    if is_system:
        return False
    return looks_like_protected_name(package) and not is_from_known_store(installer)


def is_protected(package, installer=None, is_system=False):
    """Return True if this package must never be paused/stopped/uninstalled.

    - Genuine system packages: always protected.
    - Named essentials / OS-OEM prefixes: protected ONLY when installed from a
      known store (a sideloaded copy of the name is a spoof -> not protected).
    """
    if is_system:
        return True
    if not looks_like_protected_name(package):
        return False
    return is_from_known_store(installer)


def demo():
    # Genuine Play Store system app -> protected.
    assert is_protected("com.google.android.gms", "com.android.vending")
    # Genuine system package with unknown installer -> still protected.
    assert is_protected("com.samsung.android.honeyboard", None, is_system=True)
    # Sideloaded impersonator of a system name -> NOT protected, IS a spoof.
    assert not is_protected("com.google.android.fakecore", None)
    assert is_spoof("com.google.android.fakecore", None)
    # Ordinary sideloaded adware -> not protected, not a spoof.
    assert not is_protected("com.random.adware", None)
    assert not is_spoof("com.random.adware", None)
    # Normal Play Store app that isn't a protected name -> not protected.
    assert not is_protected("com.spotify.music", "com.android.vending")
    print("protected.py demo OK")


if __name__ == "__main__":
    demo()
