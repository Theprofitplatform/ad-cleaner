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


# --- Junk / fake-app heuristics ---------------------------------------------
# Consumer nuisanceware categories: cleaners, boosters, optimizers. Substring
# match on the lowercased package/label.
# ponytail: heuristic knob, not a classifier -- a hit means "worth review", not
# "malware". Tune the words; keep them specific enough to avoid legit apps
# (e.g. "speed" is omitted so Ookla Speedtest isn't flagged).
JUNK_WORDS = (
    "clean", "boost", "optimize", "speedup", "junk", "antivirus",
    "flashlight",   # third-party flashlight apps are a classic ad vector; OEM torches are protected
)

# Known-bad package ids (exact match) -> forced HIGH. Seed of notorious
# nuisanceware; extend per-device via adcleaner_data/blocklist.txt (loaded in
# scanner.build_inventory -> extend_blocklist). ponytail: bundled seed + user
# file is the whole "blocklist" feature; no online feed until one is asked for.
_SEED_BLOCKLIST = frozenset({
    "com.cleanmaster.mguard",           # Clean Master
    "com.cleanmaster.security",
    "com.dianxinos.optimizer.duplay",   # DU Speed Booster
    "com.qihoo.security",               # 360 Security
    "com.ijinshan.kbatterydoctor_en",   # Battery Doctor
    # Adware family caught live on a customer phone, 2026-07 (hidden apps +
    # random-name droppers installed together via Play Store):
    "com.maxfree.cjsi",
    "com.zapzip.biger",
    "com.alfacln.jkclnr",
    "com.protect.permission.appmanage.guard",
})
_BLOCKLIST = set(_SEED_BLOCKLIST)


def looks_like_junk(package, label=None):
    """True if the package/label reads as a junk cleaner/booster/optimizer."""
    hay = (package + " " + (label or "")).lower()
    return any(w in hay for w in JUNK_WORDS)


def is_blocked(package):
    """True if the package is on the known-bad blocklist."""
    return package in _BLOCKLIST


def reset_blocklist():
    """Restore the blocklist to the bundled seed. Called before each re-merge of
    the user file so deleting a line from blocklist.txt takes effect on the
    next scan instead of after a process restart."""
    _BLOCKLIST.clear()
    _BLOCKLIST.update(_SEED_BLOCKLIST)


def extend_blocklist(ids):
    """Merge extra package ids into the blocklist. Each item may carry an inline
    or whole-line '#' comment, which is stripped."""
    for raw in ids:
        pkg = raw.split("#", 1)[0].strip()
        if pkg:
            _BLOCKLIST.add(pkg)


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

    A real system-partition app (is_system) is always protected. A third-party
    app that only *looks* system by name (e.g. 'com.sec.reclean') but is a known
    junk id or has a junk/fake name is NOT protected -- otherwise the prefix rule
    would whitelist nuisanceware wearing a system-style name. The junk/blocklist
    override is fenced to the PREFIX rule only: named essentials
    (PROTECTED_EXACT -- Play Store, SystemUI, ...) can never be unprotected by a
    heuristic word hit or a stray blocklist.txt line.
    """
    if is_system:
        return True
    from stalkerware import is_stalkerware
    if package not in PROTECTED_EXACT and (
            is_blocked(package) or looks_like_junk(package)
            or is_stalkerware(package)):
        return False
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
