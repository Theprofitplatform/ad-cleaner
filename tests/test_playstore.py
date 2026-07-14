"""playstore.py: parse Play HTML, cache lookups, degrade to unknown offline."""
import adb
import playstore
from playstore import check, lookup, parse_details

PAGE = ('<html><meta property="og:title" content="Cool App - Apps on Google Play"/>'
        '<meta property="og:image" content="https://play-lh.example/x=w480"/></html>')


def test_parse_details_extracts_name_and_icon():
    assert parse_details(PAGE) == ("Cool App", "https://play-lh.example/x=w480")


def test_parse_details_survives_garbage():
    assert parse_details("") == (None, None)
    assert parse_details("<html><body>consent wall</body></html>") == (None, None)


def test_parse_details_unescapes_entities():
    page = '<meta property="og:title" content="Tom &amp; Jerry - Apps on Google Play"/>'
    assert parse_details(page)[0] == "Tom & Jerry"


def test_check_404_means_not_listed():
    assert check("x", fetch=lambda u: (404, "")) == {
        "listed": False, "name": None, "icon": None}


def test_check_200_means_listed():
    info = check("x", fetch=lambda u: (200, PAGE))
    assert info["listed"] and info["name"] == "Cool App"


def test_check_odd_status_and_network_error_mean_unknown():
    assert check("x", fetch=lambda u: (503, "")) is None
    def boom(url):
        raise OSError("offline")
    assert check("x", fetch=boom) is None


def test_lookup_caches_to_disk(monkeypatch, tmp_path):
    monkeypatch.setattr(adb, "data_dir", lambda: tmp_path)
    calls = []
    def fetch(url):
        calls.append(url)
        return 200, PAGE
    assert lookup("com.cool", fetch=fetch, now=1000)["name"] == "Cool App"
    assert lookup("com.cool", fetch=fetch, now=2000)["name"] == "Cool App"
    assert len(calls) == 1                       # second hit came from disk
    lookup("com.cool", fetch=fetch, now=1000 + playstore.CACHE_TTL + 1)
    assert len(calls) == 2                       # expired -> refetched


def test_lookup_caches_not_listed_but_never_unknown(monkeypatch, tmp_path):
    monkeypatch.setattr(adb, "data_dir", lambda: tmp_path)
    def boom(url):
        raise OSError("offline")
    assert lookup("com.shady", fetch=boom, now=1) is None
    assert not (tmp_path / playstore.CACHE_FILE).exists()   # unknown not cached
    assert lookup("com.shady", fetch=lambda u: (404, ""), now=1)["listed"] is False
    calls = []
    def fetch(url):
        calls.append(url)
        return 404, ""
    assert lookup("com.shady", fetch=fetch, now=2)["listed"] is False
    assert calls == []                           # negative result served from disk


def test_lookup_survives_corrupt_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(adb, "data_dir", lambda: tmp_path)
    (tmp_path / playstore.CACHE_FILE).write_text("{not json", encoding="utf-8")
    assert lookup("com.cool", fetch=lambda u: (200, PAGE), now=1)["listed"] is True
