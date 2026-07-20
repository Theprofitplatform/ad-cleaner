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
    print("appicon demo OK")


if __name__ == "__main__":
    demo()
