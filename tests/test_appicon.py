"""appicon.py: fish the launcher icon out of a pulled APK, cache it, never raise."""
import base64
import shutil
import zipfile

import pytest

import adb
import appicon
from appicon import device_icon, pick_icon

TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M8AAAMBAQDJ/pLvAAAAAElFTkSuQmCC")


class FakeAdb:
    """pm path answers; pull copies a prepared 'APK' into place."""
    def __init__(self, apk_path):
        self.apk_path = apk_path
        self.pulls = 0

    def shell_text(self, args, timeout=10):
        assert args[:2] == ["pm", "path"]
        return "package:/data/app/base.apk\n"

    def pull(self, remote, local, timeout=120):
        self.pulls += 1
        shutil.copy(self.apk_path, local)
        return "pulled"


def make_apk(path, entries):
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return path


def test_pick_icon_prefers_mipmap_and_highest_density():
    names = ["classes.dex", "res/drawable/icon.png",
             "res/mipmap-hdpi/ic_launcher.png",
             "res/mipmap-xxxhdpi/ic_launcher.png",
             "res/mipmap-xxxhdpi/ic_launcher_foreground.png"]
    assert pick_icon(names) == "res/mipmap-xxxhdpi/ic_launcher.png"


def test_pick_icon_none_when_no_candidates():
    assert pick_icon(["classes.dex", "assets/logo.txt", "res/raw/song.mp3"]) is None


def test_device_icon_extracts_and_caches(monkeypatch, tmp_path):
    monkeypatch.setattr(adb, "data_dir", lambda: tmp_path)
    apk = make_apk(tmp_path / "a.apk",
                   {"res/mipmap-xxhdpi/ic_launcher.png": TINY_PNG})
    fake = FakeAdb(apk)
    out = device_icon(fake, "com.x")
    assert out is not None and out.exists() and out.suffix == ".png"
    assert device_icon(fake, "com.x") == out
    assert fake.pulls == 1                      # second call served from cache


def test_device_icon_garbage_apk_returns_none(monkeypatch, tmp_path):
    monkeypatch.setattr(adb, "data_dir", lambda: tmp_path)
    junk = tmp_path / "junk.apk"
    junk.write_text("not a zip")
    assert device_icon(FakeAdb(junk), "com.junk") is None


def test_device_icon_adb_failure_returns_none(monkeypatch, tmp_path):
    monkeypatch.setattr(adb, "data_dir", lambda: tmp_path)

    class DeadAdb:
        def shell_text(self, args, timeout=10):
            raise RuntimeError("device gone")

    assert device_icon(DeadAdb(), "com.x") is None


def test_save_play_icon(monkeypatch, tmp_path):
    monkeypatch.setattr(adb, "data_dir", lambda: tmp_path)
    out = appicon.save_play_icon("com.x", TINY_PNG)
    assert out is not None and out.exists()
    assert appicon.save_play_icon("com.x", b"ignored") == out   # cached


# --- fake-icon (lookalike) detection ---------------------------------------

PIL = pytest.importorskip("PIL")
from PIL import Image, ImageDraw          # noqa: E402  (after the skip guard)


def logo(size, bg, fg, shape="circle", pad=0, corner=0.0):
    """A stand-in app icon: coloured plate + a shape, with optional transparent
    margin and rounded corners -- the two ways the same artwork differs between
    a Play listing PNG and an APK mipmap."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    inner = size - 2 * pad
    box = (pad, pad, pad + inner, pad + inner)
    if corner:
        d.rounded_rectangle(box, radius=int(inner * corner), fill=bg)
    else:
        d.rectangle(box, fill=bg)
    m, n = pad + inner * 0.25, pad + inner * 0.75
    if shape == "circle":
        d.ellipse((m, m, n, n), fill=fg)
    elif shape == "tri":
        d.polygon([(m, n), (n, n), ((m + n) / 2, m)], fill=fg)
    else:
        d.rectangle((m, m, n, n), fill=fg)
    return img


BLUE, WHITE = (30, 120, 220, 255), (255, 255, 255, 255)


def written(tmp_path, name, img):
    """Save through the same normalisation appicon uses for cached icons."""
    path = tmp_path / name
    out = img.copy()
    out.thumbnail((appicon.ICON_SIZE, appicon.ICON_SIZE))
    out.save(path, "PNG")
    return path


def dist(tmp_path, a, b):
    return appicon.hamming(appicon.dhash(written(tmp_path, "a.png", a)),
                           appicon.dhash(written(tmp_path, "b.png", b)))


def test_same_artwork_rendered_differently_stays_close(tmp_path):
    """Play's 512px rounded plate vs the APK's 192px padded square."""
    play = logo(512, BLUE, WHITE, "circle", pad=0, corner=0.2)
    apk = logo(192, BLUE, WHITE, "circle", pad=20)
    assert dist(tmp_path, play, apk) <= appicon.LOOKALIKE_MAX_DISTANCE


def test_a_transparent_margin_alone_does_not_break_the_match(tmp_path):
    """The regression that made the first cut of this useless: dhash is not
    translation-invariant, so an unaccounted margin cost ~17 bits on its own."""
    assert dist(tmp_path, logo(512, BLUE, WHITE, "circle"),
                logo(512, BLUE, WHITE, "circle", pad=60)) <= appicon.LOOKALIKE_MAX_DISTANCE


@pytest.mark.parametrize("other", [
    logo(192, BLUE, WHITE, "tri", pad=20),                        # same colours
    logo(192, (240, 200, 40, 255), (20, 20, 20, 255), "circle", pad=20),
    logo(192, (10, 10, 10, 255), (200, 40, 40, 255), "square", pad=40),
])
def test_different_artwork_stays_far(tmp_path, other):
    play = logo(512, BLUE, WHITE, "circle", pad=0, corner=0.2)
    assert dist(tmp_path, play, other) > appicon.LOOKALIKE_MAX_DISTANCE


def test_closest_picks_the_nearest_brand_within_the_limit(tmp_path):
    refs = {"Blue": appicon.dhash(written(tmp_path, "r1.png",
                                          logo(512, BLUE, WHITE, "circle", corner=0.2))),
            "Dark": appicon.dhash(written(tmp_path, "r2.png",
                                          logo(512, (10, 10, 10, 255), (200, 40, 40, 255),
                                               "square", pad=40)))}
    fake = appicon.dhash(written(tmp_path, "s.png", logo(192, BLUE, WHITE, "circle", pad=20)))
    assert appicon.closest(fake, refs) == "Blue"


def test_closest_returns_none_when_nothing_is_close_enough(tmp_path):
    refs = {"Blue": appicon.dhash(written(tmp_path, "r.png", logo(512, BLUE, WHITE, "circle")))}
    odd = appicon.dhash(written(tmp_path, "s.png",
                                logo(192, (10, 10, 10, 255), (200, 40, 40, 255), "square", pad=40)))
    assert appicon.closest(odd, refs) is None


def test_dhash_of_an_unreadable_file_is_none(tmp_path):
    bad = tmp_path / "nope.png"
    bad.write_text("not an image")
    assert appicon.dhash(bad) is None
    assert appicon.dhash(tmp_path / "missing.png") is None


# --- verdict store ---------------------------------------------------------

def test_verdicts_remember_both_answers(monkeypatch, tmp_path):
    monkeypatch.setattr(adb, "data_dir", lambda: tmp_path)
    appicon.remember_lookalike("com.fake.wa", "WhatsApp")
    appicon.remember_lookalike("com.plain.app", "")
    # Only a match scores; both count as judged, so neither is pulled again.
    assert appicon.known_lookalikes() == {"com.fake.wa": "WhatsApp"}
    assert appicon.judged_packages() == {"com.fake.wa", "com.plain.app"}


def test_verdicts_survive_a_corrupt_file(monkeypatch, tmp_path):
    monkeypatch.setattr(adb, "data_dir", lambda: tmp_path)
    (tmp_path / appicon.VERDICTS_FILE).write_text("{ not json", encoding="utf-8")
    assert appicon.known_lookalikes() == {}
    appicon.remember_lookalike("com.fake.wa", "WhatsApp")
    assert appicon.known_lookalikes() == {"com.fake.wa": "WhatsApp"}


# --- end to end against a fake phone ---------------------------------------

def apk_with(tmp_path, name, img):
    buf = tmp_path / f"{name}.png"
    written(tmp_path, buf.name, img)
    return make_apk(tmp_path / f"{name}.apk",
                    {"res/mipmap-xxhdpi/ic_launcher.png": buf.read_bytes()})


def test_check_lookalike_names_the_brand_it_copies(monkeypatch, tmp_path):
    monkeypatch.setattr(adb, "data_dir", lambda: tmp_path)
    refs = {"WhatsApp": appicon.dhash(written(tmp_path, "ref.png",
                                              logo(512, BLUE, WHITE, "circle", corner=0.2)))}
    phone = FakeAdb(apk_with(tmp_path, "fake", logo(192, BLUE, WHITE, "circle", pad=20)))
    assert appicon.check_lookalike(phone, "com.free.wa", refs) == "WhatsApp"


def test_check_lookalike_clears_an_unrelated_icon(monkeypatch, tmp_path):
    monkeypatch.setattr(adb, "data_dir", lambda: tmp_path)
    refs = {"WhatsApp": appicon.dhash(written(tmp_path, "ref.png",
                                              logo(512, BLUE, WHITE, "circle", corner=0.2)))}
    phone = FakeAdb(apk_with(tmp_path, "real",
                             logo(192, (10, 10, 10, 255), (200, 40, 40, 255), "square", pad=40)))
    assert appicon.check_lookalike(phone, "com.some.app", refs) == ""


def test_check_lookalike_returns_none_when_it_could_not_look(monkeypatch, tmp_path):
    """None means "unknown", and the caller must not record it as a verdict --
    otherwise an offline PC would remember every app as clean."""
    monkeypatch.setattr(adb, "data_dir", lambda: tmp_path)
    phone = FakeAdb(apk_with(tmp_path, "any", logo(192, BLUE, WHITE, "circle")))
    assert appicon.check_lookalike(phone, "com.x", {}) is None          # no references
    assert appicon.check_lookalike(phone, "com.whatsapp", {"WhatsApp": 1}) is None  # is the brand
    junk = tmp_path / "junk.apk"
    junk.write_text("not a zip")
    assert appicon.check_lookalike(FakeAdb(junk), "com.y", {"WhatsApp": 1}) is None
