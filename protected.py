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

# Installer-package prefixes owned by an OEM's own system components (e.g. Samsung
# OMC: com.samsung.android.app.omcagent). A first-party-named app delivered by one
# of these is a genuine preload -- unlike the generic "unknown sources" installer
# (com.google.android.packageinstaller) or a browser, which sideload impersonators.
OEM_INSTALLER_PREFIXES = ("com.sec.", "com.samsung.")

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


def _trusted_source(installer):
    """True if this installer delivers GENUINE first-party apps: the device image
    (no installer), a known app store, or an OEM system component (Samsung OMC etc.).
    The generic 'unknown sources' installer and browsers are NOT trusted -- a
    first-party name from them is an impersonator.
    """
    if installer is None or installer in KNOWN_STORES:
        return True
    return installer.startswith(OEM_INSTALLER_PREFIXES)


def is_genuine_system(package, installer=None, is_system=False):
    """A first-party-named package that really is the OEM's app, not an impostor.

    Real preloads (e.g. Samsung's) usually have a NULL or OEM-system installer --
    never a known store -- so the old 'protected only if store-installed' rule
    wrongly flagged every genuine preinstalled app as a spoof on real hardware.
    """
    if is_system:
        return True
    return looks_like_protected_name(package) and _trusted_source(installer)


def is_spoof(package, installer, is_system=False):
    """A first-party NAME delivered by an untrusted (sideload) channel = impostor."""
    if is_system:
        return False
    return looks_like_protected_name(package) and not is_genuine_system(package, installer)


def is_protected(package, installer=None, is_system=False):
    """Return True if this package must never be paused/stopped/uninstalled:
    a genuine system/OEM app. Impersonators are NOT protected (see is_spoof).
    """
    return is_genuine_system(package, installer, is_system)


def demo():
    # Genuine Play Store system app -> protected.
    assert is_protected("com.google.android.gms", "com.android.vending")
    # Genuine system package with unknown installer -> still protected.
    assert is_protected("com.samsung.android.honeyboard", None, is_system=True)
    # Real Samsung preloads (first-party name, NULL / OEM installer) -> protected.
    assert is_protected("com.sec.android.app.kidshome", None)
    assert is_protected("com.sec.android.app.clockpackage", "com.samsung.android.app.omcagent")
    assert not is_spoof("com.sec.android.app.kidshome", None)
    # Impersonator: first-party name from the generic sideload installer -> spoof.
    assert is_spoof("com.google.android.fakecore", "com.google.android.packageinstaller")
    assert not is_protected("com.google.android.fakecore", "com.google.android.packageinstaller")
    # Ordinary sideloaded adware -> not protected, not a spoof.
    assert not is_protected("com.random.adware", None)
    assert not is_spoof("com.random.adware", None)
    # Normal Play Store app that isn't a protected name -> not protected.
    assert not is_protected("com.spotify.music", "com.android.vending")
    print("protected.py demo OK")


if __name__ == "__main__":
    demo()
