"""Curated preinstalled-junk list (carrier installers, OEM ad services,
Facebook preload stubs). Only EXACT matches here may be disabled -- this list
is the safety authorization, so keep it to packages with well-documented
removals and no OS role. ponytail: seed + user file, same model as the
blocklist; UAD-style tiers can come later if the seed proves too small.
"""
BLOAT_SEED = frozenset({
    # Facebook preload stubs (background downloaders, no UI)
    "com.facebook.appmanager", "com.facebook.services", "com.facebook.system",
    # Carrier app installers / "content delivery" (DT Ignite family)
    "com.dti.att", "com.dti.tmobile", "com.dti.sprint", "com.dti.telstra",
    "com.aura.oobe", "com.aura.oobe.att", "com.aura.oobe.samsung",
    "com.ironsource.appcloud.oobe", "com.ironsource.appcloud.oobe.hutchison",
    # OEM ad/analytics services
    "com.miui.msa.global",            # Xiaomi ad service
    "com.miui.analytics",
    "com.samsung.android.mateagent",  # Samsung promotion agent
    "com.samsung.android.app.omcagent",
    # Lock-screen ads/content
    "com.glance.internet", "us.zoom.videomeetings.preload",
})


def _user_bloat():
    from adb import data_dir  # local import: keep monkeypatched adb.data_dir effective
    path = data_dir() / "bloatware.txt"
    if not path.exists():
        return set()
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeDecodeError):
        return set()
    return {ln.split("#", 1)[0].strip() for ln in lines} - {""}


def find_bloat(adb):
    """Installed system packages that are on the junk list, sorted."""
    out = adb.shell_text(["pm", "list", "packages", "-s"])
    installed = {ln.split(":", 1)[1].strip()
                 for ln in (out or "").splitlines() if ln.startswith("package:")}
    return sorted(installed & (BLOAT_SEED | _user_bloat()))
