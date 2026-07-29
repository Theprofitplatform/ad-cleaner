"""Tests for migrate.py — SMS/MMS/SIM recovery from vendor backups.

Pure-function tests plus a tiny fake ADB; no device, no Tk. The fixtures are
trimmed from a real ZTE Blade Q Lux backup, so the quirks they pin (SENDBOX,
unnamed MMS parts, toolbox with no `find`) are the ones seen in the field.
"""

import migrate
import pytest
from adb import AdbError

SMS_VMSG = (
    "BEGIN:VMSG\nVERSION:1.1\nBEGIN:VCARD\nTEL:+61402091631\nEND:VCARD\n"
    "BEGIN:VBODY\nX-BOX:INBOX\nX-READ:READ\nX-LOCKED:UNLOCKED\n"
    "Date:2018/01/29 13:14:17\n"
    "Subject;ENCODING=QUOTED-PRINTABLE;CHARSET=UTF-8:=68=69=20=\n=74=68=65=72=65\n"
    "END:VBODY\nEND:VMSG\n"
    "BEGIN:VMSG\nVERSION:1.1\nBEGIN:VCARD\nTEL:+61400000001\nEND:VCARD\n"
    "BEGIN:VBODY\nX-BOX:SENDBOX\nX-READ:UNREAD\n"
    "Date:2018/01/30 09:00:00\nSubject:plain text\n"
    "END:VBODY\nEND:VMSG\n"
)

# An MMS part with no X-MMS-PART-NAME — the case that silently broke Gallery
# indexing until with_ext() started deriving an extension from the MIME type.
MMS_VMSG = (
    "BEGIN:VMSG\nDate:2018/02/11 17:11:52\nBEGIN:VPART\n"
    "X-MMS-PART-CONTENT-TYPE:image/jpeg\nX-MMS-PART-NAME:IMG_6020.jpg\n"
    "X-MMS-PART-DATA:aGVsbG8=\nEND:VPART\n"
    "BEGIN:VPART\nX-MMS-PART-CONTENT-TYPE:image/gif\n"
    "X-MMS-PART-DATA:d29ybGQ=\nEND:VPART\n"
    "BEGIN:VPART\nX-MMS-PART-CONTENT-TYPE:text/plain\n"
    "X-MMS-PART-DATA:eA==\nEND:VPART\nEND:VMSG\n"
)


class FakeAdb:
    """Minimal ADB: canned shell output plus a pull that writes a local file."""

    def __init__(self, sim_rows="", backups=(), payload="", contact_rows="",
                 media_paths=()):
        self.sim_rows, self.backups, self.payload = sim_rows, backups, payload
        self.contact_rows, self.media_paths = contact_rows, list(media_paths)
        self.shell_calls, self.pulled = [], []

    def shell_text(self, args, timeout=10):
        cmd = " ".join(args)
        self.shell_calls.append(cmd)
        if "icc/adn" in cmd:
            return self.sim_rows
        if "com.android.contacts" in cmd:
            return self.contact_rows
        if "images/media" in cmd:
            return self._rows(r for r in self.media_paths
                              if not r.lower().endswith((".mp4", ".3gp")))
        if "video/media" in cmd:
            return self._rows(r for r in self.media_paths
                              if r.lower().endswith((".mp4", ".3gp")))
        if cmd.startswith("ls -d"):
            # Real sh leaves non-matching globs as literal patterns.
            globs = [g for g in cmd.split() if "*" in g]
            return "\n".join(list(self.backups) + globs)
        if "scan_volume" in cmd:
            return "Result: Bundle[{}]"
        raise AdbError("unexpected: " + cmd)

    @staticmethod
    def _rows(paths):
        return "".join("Row: %d _data=%s\n" % (i, p) for i, p in enumerate(paths))

    def pull(self, remote, local, timeout=120):
        from pathlib import Path
        self.pulled.append(remote)
        p = Path(local)
        if p.is_dir():                      # directory pull, as adb does it
            (p / "pulled.jpg").write_bytes(b"x")
            return "1 file pulled"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(self.payload, encoding="latin-1")
        return "1 file pulled"


def test_sendbox_counts_as_sent():
    # ZTE writes SENDBOX, not SENT. Mapping it wrong files every sent message
    # as received and the customer's threads read backwards.
    msgs = migrate.parse_vmsg(SMS_VMSG)
    assert [m["type"] for m in msgs] == [1, 2]
    assert msgs[0]["body"] == "hi there"      # quoted-printable + soft wrap
    assert msgs[1]["body"] == "plain text"    # unencoded Subject
    assert msgs[1]["read"] == 0


def test_merge_prefers_union_not_newest():
    """A later vendor backup is often smaller than an earlier one, so merging
    must union them rather than trust the newest file."""
    big = migrate.parse_vmsg(SMS_VMSG)
    small = big[:1]
    merged = migrate.merge_messages([small, big])
    assert len(merged) == 2
    assert merged[0]["date"] <= merged[1]["date"]


def test_merge_is_idempotent():
    msgs = migrate.parse_vmsg(SMS_VMSG)
    assert len(migrate.merge_messages([msgs, msgs, msgs])) == len(msgs)


def test_sms_xml_escapes_and_counts(tmp_path):
    msgs = migrate.parse_vmsg(SMS_VMSG)
    msgs[0]["body"] = 'ampersand & "quote" <tag>'
    out = tmp_path / "sms.xml"
    migrate.write_sms_xml(msgs, out)
    text = out.read_text(encoding="utf-8")
    assert 'count="2"' in text
    assert "&amp;" in text and "&lt;tag&gt;" in text
    # Must stay parseable — SMS Backup & Restore rejects malformed XML outright.
    # stdlib ET is fine here: the input is the file this test just wrote, not
    # untrusted data, so there is no XXE/entity-expansion surface to defend.
    import xml.etree.ElementTree as ET
    assert len(ET.parse(out).getroot().findall("sms")) == 2


def test_mms_extract_skips_text_and_names_unnamed_parts(tmp_path):
    n = migrate.extract_mms_media(MMS_VMSG, tmp_path)
    assert n == 2                       # the text/plain part is not media
    names = sorted(p.name for p in tmp_path.iterdir())
    assert names[0].endswith("IMG_6020.jpg")
    assert names[1].endswith(".gif")    # unnamed part still got an extension
    assert all(p.suffix for p in tmp_path.iterdir())


def test_mms_start_offset_avoids_collisions(tmp_path):
    migrate.extract_mms_media(MMS_VMSG, tmp_path, start=0)
    migrate.extract_mms_media(MMS_VMSG, tmp_path, start=1000)
    assert len(list(tmp_path.iterdir())) == 4


def test_mms_bad_base64_does_not_abort_the_rest(tmp_path):
    broken = MMS_VMSG.replace("aGVsbG8=", "!!!not base64!!!")
    assert migrate.extract_mms_media(broken, tmp_path) == 1


def test_sim_rows_and_vcf():
    rows = migrate.parse_sim_rows(
        "Row: 0 name=Jo Bloggs, number=+61400000000, emails=NULL, anrs=NULL, _id=1\n"
        "Row: 1 name=NULL, number=NULL, emails=NULL, anrs=NULL, _id=2\n"
        "Row: 2 name=NULL, number=1555, emails=NULL, anrs=NULL, _id=3\n")
    assert rows == [("Jo Bloggs", "+61400000000"), ("1555", "1555")]
    vcf = migrate.to_vcf(rows)
    assert vcf.count("BEGIN:VCARD") == 2 and "FN:Jo Bloggs" in vcf
    assert migrate.to_vcf([]) == ""


def test_glob_listing_drops_unmatched_patterns():
    # Android 5's toolbox has no `find`, so backups are located by shell globbing;
    # sh returns a non-matching glob verbatim and those must not become paths.
    got = migrate.parse_glob_listing(
        "/sdcard/*.vmsg\n"
        "/storage/sdcard1/backup/Data/2026/Sms/sms.vmsg\n"
        "/sdcard/notes.txt\n", ".vmsg")
    assert got == ["/storage/sdcard1/backup/Data/2026/Sms/sms.vmsg"]


def test_find_vendor_backups_forces_zero_exit():
    """`ls` exits non-zero when any glob misses and run() discards stdout on a
    non-zero exit — so the command must end with `; true` or real hits are lost."""
    adb = FakeAdb(backups=["/sdcard/backup/sms.vmsg"])
    assert migrate.find_vendor_backups(adb) == ["/sdcard/backup/sms.vmsg"]
    assert all(c.endswith("; true") for c in adb.shell_calls)


def test_harvest_extras_writes_all_three(tmp_path):
    adb = FakeAdb(
        sim_rows="Row: 0 name=Jo, number=+61400000000, emails=NULL, anrs=NULL, _id=1\n",
        backups=["/storage/sdcard1/backup/Sms/sms.vmsg"],
        payload=SMS_VMSG + MMS_VMSG)
    got = migrate.harvest_extras(adb, tmp_path)
    assert got["contacts"] == 1
    assert got["sms"] == 2
    assert got["mms_media"] == 2
    assert (tmp_path / "Download" / "contacts.vcf").exists()
    assert (tmp_path / "Download" / "sms-restore.xml").exists()
    # MMS pictures land under DCIM/ so the new phone's Gallery indexes them.
    assert list((tmp_path / migrate.MMS_FOLDER).iterdir())


def test_harvest_extras_merges_phone_and_sim_contacts(tmp_path):
    """Phone-memory contacts are the ones at risk: an owner who never signed
    into Google has them nowhere else. Same number in both = one contact."""
    adb = FakeAdb(
        sim_rows="Row: 0 name=Jo, number=+61 400 000 000, emails=NULL, anrs=NULL, _id=1\n",
        contact_rows=(
            "Row: 0 display_name=Jo, data1=+61400000000, "
            "mimetype=vnd.android.cursor.item/phone_v2\n"
            "Row: 1 display_name=Ann, data1=+61411111111, "
            "mimetype=vnd.android.cursor.item/phone_v2\n"
            "Row: 2 display_name=Ann, data1=ann@example.com, "
            "mimetype=vnd.android.cursor.item/email_v2\n"))
    got = migrate.harvest_extras(adb, tmp_path)
    assert got["contacts"] == 2          # Jo deduped, email row ignored
    vcf = (tmp_path / "Download" / "contacts.vcf").read_text(encoding="utf-8")
    assert "FN:Ann" in vcf and "ann@example.com" not in vcf


def test_harvest_extras_pulls_app_media_the_folder_list_misses(tmp_path):
    """WhatsApp keeps pictures outside DCIM/Pictures/Movies since Android 11."""
    wa = "/storage/emulated/0/Android/media/com.whatsapp/WhatsApp/Media/WhatsApp Images"
    adb = FakeAdb(media_paths=[wa + "/IMG-1.jpg", wa + "/IMG-2.jpg",
                               "/storage/emulated/0/DCIM/Camera/a.jpg"])
    got = migrate.harvest_extras(adb, tmp_path, covered=["/sdcard/DCIM"])
    assert got["other_media"] == 1       # the fake pull writes one file
    # DCIM was already covered by the caller and must not be fetched twice.
    assert [p for p in adb.pulled if "DCIM" in p] == []
    assert any("WhatsApp" in p for p in adb.pulled)


def test_harvest_extras_survives_a_phone_with_nothing(tmp_path):
    """No SIM, no vendor backup — must return zeros, not raise, or it would
    fail a media transfer that already succeeded."""
    class Dead(FakeAdb):
        def shell_text(self, args, timeout=10):
            raise AdbError("No phone detected.")

    got = migrate.harvest_extras(Dead(), tmp_path)
    assert got == {"contacts": 0, "sms": 0, "mms_media": 0, "other_media": 0,
                   "sources": []}
    assert not (tmp_path / "Download").exists()


def test_extra_media_dirs_ranks_and_skips_thumbnails():
    wa = "/storage/emulated/0/Android/media/com.whatsapp/WhatsApp/Media/WhatsApp Images"
    adb = FakeAdb(media_paths=[
        wa + "/a.jpg", wa + "/b.jpg", wa + "/c.jpg",
        "/storage/emulated/0/Pictures/Screenshots/s.png",
        "/storage/emulated/0/DCIM/.thumbnails/t.jpg",     # cache, never wanted
        "/storage/emulated/0/DCIM/Camera/x.jpg",          # covered by caller
    ])
    got = migrate.extra_media_dirs(adb, covered=["/sdcard/DCIM"])
    # Paths come back normalised: /storage/emulated/0 and /sdcard are one place.
    assert got[0] == (wa.replace("/storage/emulated/0", "/sdcard"), 3)
    assert all(".thumbnails" not in d for d, _ in got)
    assert all("/DCIM/" not in d for d, _ in got)         # caller already has it
    assert any("Screenshots" in d for d, _ in got)


@pytest.mark.parametrize("path,want", [
    ("/sdcard/Android/media/com.whatsapp/WhatsApp/Media/WhatsApp Images",
     "WhatsApp_Images"),
    ("/sdcard/Pictures/Screenshots", "Screenshots"),
    # A bare "Media"/"Images" leaf says nothing; keep its parent for context.
    ("/sdcard/Android/media/org.thoughtcrime.securesms/Media", "securesms-Media"),
])
def test_label_for(path, want):
    assert migrate.label_for(path) == want


def test_label_for_breaks_ties():
    taken = set()
    a = migrate.label_for("/sdcard/one/Screenshots", taken); taken.add(a)
    b = migrate.label_for("/sdcard/two/Screenshots", taken)
    assert (a, b) == ("Screenshots", "Screenshots-2")


def test_scan_media_reports_failure_without_raising():
    class Dead(FakeAdb):
        def shell_text(self, args, timeout=10):
            raise AdbError("nope")

    assert migrate.scan_media(FakeAdb()) is True
    assert migrate.scan_media(Dead()) is False


def test_module_demo_passes():
    migrate.demo()


@pytest.mark.parametrize("name,ctype,want", [
    ("", "image/jpeg", "attachment.jpg"),
    ("photo", "image/gif", "photo.gif"),
    ("clip", "video/3gpp", "clip.3gp"),
    ("a.jpg", "image/jpeg", "a.jpg"),
    ("x", "application/weird", "x.bin"),
    ("y", "image/jpeg; charset=utf-8", "y.jpg"),
])
def test_with_ext(name, ctype, want):
    assert migrate.with_ext(name, ctype) == want
