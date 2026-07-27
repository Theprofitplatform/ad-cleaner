"""Pull an app's real icon off the phone, best effort.

Shown next to the app details so the operator sees what the customer sees on
their home screen. There is no adb command for this: we pull the APK and fish
the launcher icon out of the zip by name.

ponytail: name-heuristic zip scan, no manifest/resources.arsc parsing. Apps
whose icon is adaptive-vector-only (no PNG/WEBP fallback) or oddly named yield
None — acceptable; upgrade path is the pyaxmlparser dependency.

Every failure returns None. Extracted icons are cached under
adcleaner_data/icons/ so each package pulls its (possibly huge) APK once.
"""
from __future__ import annotations

import json
import re
import tempfile
import zipfile
from pathlib import Path

try:                     # Pillow decodes webp + resizes; present in the exe
    from PIL import Image
except ImportError:      # stdlib-only fallback: PNG entries shown as-is
    Image = None

ICON_SIZE = 64
_CANDIDATE = re.compile(
    r"(?:^|/)(?:ic_launcher|app_icon|appicon|icon|logo)[^/]*\.(?:png|webp)$")
_LAYER = re.compile(r"_(?:foreground|background|monochrome)")   # adaptive layers


def icons_dir():
    from adb import data_dir
    d = data_dir() / "icons"
    d.mkdir(parents=True, exist_ok=True)
    return d


def pick_icon(names):
    """Zip entry names -> the most launcher-icon-looking one, or None.
    Rank: mipmap beats drawable, exact ic_launcher beats lookalikes, then the
    alphabetically-last density dir (xxxhdpi sorts after hdpi/xhdpi/xxhdpi)."""
    best, best_key = None, None
    for n in names:
        low = n.lower()
        if not low.startswith("res/") or _LAYER.search(low):
            continue
        if not _CANDIDATE.search(low):
            continue
        key = ("mipmap" in low, "/ic_launcher." in low or "/ic_launcher_round." in low, low)
        if best_key is None or key > best_key:
            best, best_key = n, key
    return best


def extract_icon(apk_path):
    """APK on disk -> raw icon bytes + entry name, or (None, None)."""
    try:
        with zipfile.ZipFile(apk_path) as zf:
            name = pick_icon(zf.namelist())
            if not name:
                return None, None
            return zf.read(name), name
    except Exception:
        return None, None


def _save(data, entry_name, out_png):
    """Icon bytes -> normalized PNG on disk. Returns True on success."""
    if Image is not None:
        try:
            import io
            img = Image.open(io.BytesIO(data)).convert("RGBA")
            img.thumbnail((ICON_SIZE, ICON_SIZE))
            img.save(out_png, "PNG")
            return True
        except Exception:
            return False
    if entry_name.lower().endswith(".png"):     # tk reads PNG natively
        try:
            out_png.write_bytes(data)
            return True
        except OSError:
            return False
    return False                                # webp without Pillow


def device_icon(adb, package):
    """Return a cached PNG path for this package's icon, or None.

    Pulls the APK on a cache miss — that can be 100+ MB, which is why callers
    run this in a background thread and only for the selected app.
    """
    out = icons_dir() / f"{package}.png"
    if out.exists():
        return out
    try:
        paths = adb.shell_text(["pm", "path", package])
    except Exception:
        return None
    apk = next((l.strip()[len("package:"):] for l in paths.splitlines()
                if l.strip().startswith("package:")), None)
    if not apk:
        return None
    # ignore_cleanup_errors: closing the app mid-pull leaves adb.exe holding
    # base.apk; without this the shutdown finalizer tracebacks (WinError 32).
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        local = str(Path(tmp) / "base.apk")
        try:
            adb.pull(apk, local)
        except Exception:
            return None
        data, entry = extract_icon(local)
    if data and _save(data, entry, out):
        return out
    return None


def save_play_icon(package, data):
    """Cache an icon downloaded from Play (used when the APK had none)."""
    out = icons_dir() / f"{package}.play.png"
    if out.exists():
        return out
    return out if _save(data, "icon.png", out) else None


# --- fake-app detection: is this icon a copy of a famous one? ---------------
# The other half of impersonation. is_spoof catches a package *name* pretending
# to be a system app; this catches the artwork, which is what the customer
# actually recognises on the home screen.
#
# Reference artwork comes from each brand's Play listing (small, cached, and
# correct for every phone) -- the suspect's icon comes off the phone, because
# what it shows on the home screen is the whole point of the trick.

# Genuine package -> what a customer would call it.
IMPERSONATED = {
    "com.android.chrome": "Chrome",
    "com.android.vending": "the Play Store",
    "com.google.android.gms": "Google Play services",
    "com.android.settings": "Settings",
    "com.google.android.gm": "Gmail",
    "com.google.android.youtube": "YouTube",
    "com.google.android.apps.photos": "Google Photos",
    "com.whatsapp": "WhatsApp",
    "com.facebook.katana": "Facebook",
    "com.facebook.orca": "Messenger",
    "com.instagram.android": "Instagram",
    "com.snapchat.android": "Snapchat",
    "org.telegram.messenger": "Telegram",
    "com.zhiliaoapp.musically": "TikTok",
    "com.netflix.mediaclient": "Netflix",
    "com.spotify.music": "Spotify",
}

# ponytail: a difference hash and a distance knob, not an image classifier.
# Measured on synthetic icons rendered the two ways this code meets them (a
# large Play listing PNG vs a smaller APK mipmap, different transparent
# margins): same artwork scored 5-7 bits apart, different artwork 19-42. 10
# sits in that gap with margin on both sides. Synthetic, though -- real icons
# get the final say, so this is the first knob to move after a field test.
LOOKALIKE_MAX_DISTANCE = 10   # of 64 bits
VERDICTS_FILE = "icon_spoof.json"


def dhash(path):
    """Icon file -> 64-bit difference hash, or None (no Pillow, unreadable).

    Two normalisations earn their keep, both measured: crop to the opaque
    content first, because the same artwork ships with different transparent
    margins and a difference hash is not translation-invariant (it cost 16-17
    bits, enough to bury the signal); then flatten what's left onto white, so
    transparent pixels can't land wherever the encoder happened to leave them.
    """
    if Image is None:
        return None
    try:
        img = Image.open(path).convert("RGBA")
        box = img.getchannel("A").getbbox()
        if box:
            img = img.crop(box)
        img = Image.alpha_composite(Image.new("RGBA", img.size, "white"), img)
        img = img.convert("L").resize((9, 8))
    except Exception:
        return None
    # getdata() is deprecated in Pillow 12 but is all older builds have.
    px = list(getattr(img, "get_flattened_data", img.getdata)())
    bits = 0
    for row in range(8):
        for col in range(8):
            bits = (bits << 1) | int(px[row * 9 + col] > px[row * 9 + col + 1])
    return bits


def hamming(a, b):
    return bin(a ^ b).count("1")


def closest(icon_hash, refs, limit=LOOKALIKE_MAX_DISTANCE):
    """{brand: hash} -> the brand this icon copies, or None."""
    best, dist = None, limit + 1
    for brand, h in refs.items():
        d = hamming(icon_hash, h)
        if d < dist:
            best, dist = brand, d
    return best


def reference_hashes():
    """{brand: icon hash} for the impersonated apps, from their Play listings.

    Cached on disk (icons/<pkg>.play.png), so only the first phone on a given
    PC pays for the downloads. Empty dict when offline or without Pillow --
    the check then simply doesn't run.
    """
    import playstore
    refs = {}
    for pkg, brand in IMPERSONATED.items():
        path = icons_dir() / f"{pkg}.play.png"
        if not path.exists():
            info = playstore.lookup(pkg)
            data = playstore.fetch_icon(info["icon"]) if info and info.get("icon") else None
            path = save_play_icon(pkg, data) if data else None
        h = dhash(path) if path else None
        if h is not None:
            refs[brand] = h
    return refs


def _verdicts_path():
    from adb import data_dir
    return data_dir() / VERDICTS_FILE


def _read_verdicts():
    """{package: brand or ""} -- every package already judged."""
    try:
        data = json.loads(_verdicts_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, str)} if isinstance(
        data, dict) else {}


def known_lookalikes():
    """{package: brand} for the ones that actually matched. Read at scan time."""
    return {k: v for k, v in _read_verdicts().items() if v}


def judged_packages():
    """Every package already looked at, verdict either way."""
    return set(_read_verdicts())


def remember_lookalike(package, brand):
    """Record a verdict, including a negative one ("" = looked, found nothing),
    so the next scan doesn't pull this app's APK again."""
    data = _read_verdicts()
    data[package] = brand or ""
    path = _verdicts_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=1), encoding="utf-8")
    except OSError:
        pass


def check_lookalike(adb, package, refs):
    """Pull this app's icon off the phone and name the brand it copies, or "".

    Returns None when there was nothing to compare (no icon, no references) --
    the caller must not record that as a verdict, or a phone that was merely
    offline would be remembered as clean.
    """
    if not refs or package in IMPERSONATED:
        return None
    path = device_icon(adb, package)
    h = dhash(path) if path else None
    if h is None:
        return None
    return closest(h, refs) or ""


def demo():
    names = ["classes.dex", "res/drawable/icon.png",
             "res/mipmap-hdpi/ic_launcher.png",
             "res/mipmap-xxxhdpi/ic_launcher.png",
             "res/mipmap-xxxhdpi/ic_launcher_foreground.png"]
    assert pick_icon(names) == "res/mipmap-xxxhdpi/ic_launcher.png"
    assert pick_icon(["classes.dex", "assets/logo.txt"]) is None
    assert pick_icon(["res/drawable/appicon.webp"]) == "res/drawable/appicon.webp"
    import io
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("res/mipmap-hdpi/ic_launcher.png", b"fake-png")
    buf.seek(0)
    import tempfile as tf
    with tf.NamedTemporaryFile(suffix=".apk", delete=False) as f:
        f.write(buf.read())
    data, entry = extract_icon(f.name)
    assert data == b"fake-png" and entry.endswith("ic_launcher.png")
    Path(f.name).unlink()
    assert extract_icon("does-not-exist.apk") == (None, None)

    # Lookalike maths: nearest brand wins, but only inside the distance limit.
    assert hamming(0b1011, 0b1001) == 1
    assert closest(0, {"A": 0b1, "B": 1 << 63}, limit=1) == "A"
    assert closest(0, {"A": 0b111}, limit=2) is None
    assert dhash("does-not-exist.png") is None
    print("appicon demo OK")


if __name__ == "__main__":
    demo()
