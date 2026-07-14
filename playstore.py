"""Best-effort Google Play lookups: is this package listed, and what does
Google say it's called?

A fake app copies a real app's name and icon, but it can't fake its listing:
`com.kxqz.updater` is not on the Play Store. "Not listed" is therefore a
genuine fake-app signal — while "listed" gives us the official name to compare
against what the phone shows.

Offline-first: this is the only module in the app that touches the network,
and every failure (no internet, Play changed its HTML, weird status) degrades
to `None` = "unknown", never an error. Results are cached on disk so a shop
scanning many phones only pays the lookup once per package.
"""
from __future__ import annotations

import html
import json
import re
import time
import urllib.error
import urllib.request

PLAY_URL = "https://play.google.com/store/apps/details?id={pkg}&hl=en&gl=us"
CACHE_FILE = "play_cache.json"
CACHE_TTL = 7 * 24 * 3600          # seconds; popular packages barely change
TIMEOUT = 6
NOT_LISTED_REASON = "Not listed on Google Play"
# Play serves urllib's default UA fine, but a browser UA avoids bot heuristics.
_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

_OG = r'<meta\s+property="og:{prop}"\s+content="([^"]*)"'


def parse_details(page):
    """Play details HTML -> (official name or None, icon url or None)."""
    name = icon = None
    m = re.search(_OG.format(prop="title"), page)
    if m:
        name = html.unescape(m.group(1))
        # og:title is "<App name> - Apps on Google Play" (dash varies).
        name = re.split(r"\s+[-–—]\s+Apps on Google Play", name)[0].strip() or None
    m = re.search(_OG.format(prop="image"), page)
    if m:
        icon = html.unescape(m.group(1)) or None
    return name, icon


def _fetch(url, timeout=TIMEOUT):
    """GET url -> (status, body-str). Raises OSError family on network trouble."""
    req = urllib.request.Request(url, headers=_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        return e.code, ""


def check(package, fetch=_fetch):
    """Ask Play about one package.

    Returns {"listed": bool, "name": str|None, "icon": url|None},
    or None when the network (or Play) wouldn't say — treat as unknown.
    """
    try:
        status, page = fetch(PLAY_URL.format(pkg=package))
    except Exception:
        return None
    if status == 404:
        return {"listed": False, "name": None, "icon": None}
    if status != 200:
        return None
    name, icon = parse_details(page)
    return {"listed": True, "name": name, "icon": icon}


def fetch_icon(url, timeout=TIMEOUT):
    """Icon url -> raw image bytes, or None. Play icon CDN honours size params;
    without `image/webp` in Accept it serves PNG/JPEG, which tk can show."""
    req = urllib.request.Request(url, headers=dict(_HEADERS, Accept="image/png,image/*;q=0.8"))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except Exception:
        return None


# --- disk cache --------------------------------------------------------------

def _cache_path():
    from adb import data_dir  # local import: keep the parse core import-light
    return data_dir() / CACHE_FILE

def _load():
    try:
        return json.loads(_cache_path().read_text(encoding="utf-8"))
    except Exception:
        return {}

def lookup(package, fetch=_fetch, now=None):
    """check() with a 7-day disk cache. Unknown (None) is never cached, so a
    dropped connection retries on the next scan."""
    now = now or time.time()
    cache = _load()
    hit = cache.get(package)
    if hit and now - hit.get("ts", 0) < CACHE_TTL:
        return {k: hit.get(k) for k in ("listed", "name", "icon")}
    info = check(package, fetch=fetch)
    if info is None:
        return None
    cache[package] = dict(info, ts=now)
    try:
        _cache_path().write_text(json.dumps(cache, indent=0), encoding="utf-8")
    except OSError:
        pass
    return info


def demo():
    page = ('<html><meta property="og:title" content="Google Chrome - Apps on '
            'Google Play"/><meta property="og:image" content="https://play-lh.'
            'googleusercontent.com/abc=w480"/></html>')
    assert parse_details(page) == ("Google Chrome",
                                   "https://play-lh.googleusercontent.com/abc=w480")
    assert parse_details("<html>nope</html>") == (None, None)
    assert check("x", fetch=lambda u: (404, "")) == {
        "listed": False, "name": None, "icon": None}
    assert check("x", fetch=lambda u: (200, page))["name"] == "Google Chrome"
    assert check("x", fetch=lambda u: (503, "")) is None
    boom = lambda u: (_ for _ in ()).throw(OSError("offline"))
    assert check("x", fetch=boom) is None
    print("playstore demo OK")


if __name__ == "__main__":
    demo()
