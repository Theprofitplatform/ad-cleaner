"""Recover messages and contacts from an old phone -- the parts gui.py's file
transfer cannot reach.

gui.py already copies the user's media folders (_pull_media/_push_media). This
module adds the two things that transfer deliberately leaves out, plus the
rescan that makes restored files actually appear in the Gallery.

What is and isn't readable over ADB on an unrooted phone:

  photos/videos  yes -- handled by gui.TRANSFER_FOLDERS
  SIM contacts   yes -- content://icc/adn is world-readable
  phone contacts no -- but they are nearly always synced to the Google account,
                       so they arrive on the new phone at sign-in
  SMS/MMS        no -- the provider requires READ_SMS, which adb shell lacks

SMS is the catch, and the way through is indirect: nearly every old handset
ships a vendor backup app (ZTE Backup, LG Mobile Switch, Alcatel, older Samsung
Kies) that writes vMessage `.vmsg` files to storage. Have the customer run that
app once before Step 1, and this module converts what it wrote into the XML
that SMS Backup & Restore restores on the new phone.

Field-proven ZTE Blade Q Lux (Android 5.0.2) -> Galaxy A11 (Android 11):
4239 messages and 92 MMS attachments recovered from two stale vendor backups.
"""

import base64
import os
import re
from pathlib import Path

from adb import AdbError

# Where vendor backup apps drop things. Searched in order; missing roots are
# skipped, so listing extras costs nothing.
STORAGE_ROOTS = ["/sdcard", "/storage/sdcard1", "/storage/extSdCard", "/storage/emulated/0"]

# Recovered MMS pictures go under DCIM/ so the Gallery indexes them, in their
# own folder so message attachments stay separate from the camera roll.
MMS_FOLDER = "DCIM/OldPhone-MMS"

BOX_TYPE = {
    "INBOX": 1, "SENT": 2, "SENDBOX": 2, "SENTBOX": 2,
    "DRAFT": 3, "OUTBOX": 4, "FAILED": 5, "QUEUED": 6,
}

# Parts with no X-MMS-PART-NAME need an extension or the Android media scanner
# silently ignores the file -- it will sit in DCIM and never reach the Gallery.
MIME_EXT = {
    "image/jpeg": ".jpg", "image/jpg": ".jpg", "image/png": ".png",
    "image/gif": ".gif", "image/bmp": ".bmp", "image/webp": ".webp",
    "video/3gpp": ".3gp", "video/mp4": ".mp4",
    "audio/amr": ".amr", "audio/mpeg": ".mp3",
}

# --------------------------------------------------------------------------
# vMessage (.vmsg) parsing -- the format ZTE/LG/Alcatel backup apps write
# --------------------------------------------------------------------------

def _decode_field(raw, params):
    """Decode one vMessage field value, honouring its ';'-separated parameters."""
    if "QUOTED-PRINTABLE" in params.upper():
        m = re.search(r"CHARSET=([^;]+)", params, re.I)
        charset = m.group(1).strip() if m else "utf-8"
        import quopri
        # A trailing '=' is a QP soft line break, not data.
        return quopri.decodestring(
            raw.replace("=\n", "").encode("latin-1")
        ).decode(charset, "replace")
    return raw


def _parse_message(rec):
    """One BEGIN:VMSG..END:VMSG block -> dict, or None if it carries no body."""
    m = re.search(r"^TEL[^:]*:(.*)$", rec, re.M)
    address = m.group(1).strip() if m else ""
    body, fields = None, {}
    # Re-join soft-wrapped continuation lines before splitting into fields.
    for line in re.split(r"\n(?=[A-Za-z][A-Za-z0-9-]*[;:])", rec):
        head, sep, raw = line.partition(":")
        if not sep:
            continue
        name, _, params = head.partition(";")
        name = name.strip().upper()
        if name == "SUBJECT":
            body = _decode_field(raw.rstrip("\n"), params)
        elif name.startswith("X-") or name == "DATE":
            fields[name] = raw.strip()
    if body is None:
        return None

    date_ms, readable = 0, ""
    if "DATE" in fields:
        from datetime import datetime
        try:
            # Phone-local wall clock; the format records no timezone.
            dt = datetime.strptime(fields["DATE"], "%Y/%m/%d %H:%M:%S")
            date_ms = int(dt.timestamp() * 1000)
            readable = dt.strftime("%d %b %Y %H:%M:%S")
        except ValueError:
            pass
    return {
        "address": address, "body": body, "date": date_ms, "readable": readable,
        "type": BOX_TYPE.get(fields.get("X-BOX", "").upper(), 1),
        "read": 0 if fields.get("X-READ", "").upper() == "UNREAD" else 1,
        "locked": 1 if fields.get("X-LOCKED", "").upper() == "LOCKED" else 0,
    }


def parse_vmsg(text):
    """All SMS records in a .vmsg document."""
    out = []
    for rec in re.findall(r"BEGIN:VMSG\n(.*?)\nEND:VMSG", text, re.S):
        msg = _parse_message(rec)
        if msg:
            out.append(msg)
    return out


def write_sms_xml(msgs, path):
    """Write SMS Backup & Restore XML (SyncTech), the format that app restores."""
    from xml.sax.saxutils import quoteattr
    with open(path, "w", encoding="utf-8") as f:
        f.write('<?xml version="1.0" encoding="UTF-8" standalone="yes" ?>\n')
        f.write('<smses count="%d">\n' % len(msgs))
        for m in msgs:
            f.write(
                '  <sms protocol="0" address=%s date="%d" type="%d" subject="null" '
                'body=%s toa="null" sc_toa="null" service_center="null" read="%d" '
                'status="-1" locked="%d" date_sent="0" readable_date=%s '
                'contact_name="(Unknown)" />\n'
                % (quoteattr(m["address"]), m["date"], m["type"], quoteattr(m["body"]),
                   m["read"], m["locked"], quoteattr(m["readable"]))
            )
        f.write("</smses>\n")


def merge_messages(groups):
    """Flatten several message lists, drop duplicates, sort oldest first.

    Vendor apps leave several backups behind and a later one is often *smaller*
    than an earlier one (the customer deleted threads in between), so the newest
    file is not automatically the best -- merge them all.
    """
    seen, merged = set(), []
    for msgs in groups:
        for m in msgs:
            key = (m["address"], m["date"], m["body"], m["type"])
            if key not in seen:
                seen.add(key)
                merged.append(m)
    merged.sort(key=lambda m: m["date"])
    return merged


# --------------------------------------------------------------------------
# MMS attachment extraction
# --------------------------------------------------------------------------

def _safe(s):
    return re.sub(r"[^A-Za-z0-9._-]", "_", s)[:60]


def with_ext(name, ctype):
    """Ensure a filename has an extension the Android media scanner recognises."""
    ext = MIME_EXT.get(ctype.lower().split(";")[0].strip(), "")
    if not name:
        return "attachment" + (ext or ".bin")
    return name if os.path.splitext(name)[1] else name + (ext or ".bin")


def iter_mms_parts(text):
    """Yield (date, content_type, name, base64) per VPART, carrying the
    enclosing VMSG's Date: forward so attachments can be named chronologically."""
    date = ""
    for block in re.split(r"BEGIN:(?=VMSG|VPART)", text):
        m = re.search(r"^Date:(.*)$", block, re.M)
        if m:
            date = m.group(1).strip()
        if not block.startswith("VPART"):
            continue
        ctype = re.search(r"^X-MMS-PART-CONTENT-TYPE:(.*)$", block, re.M)
        name = re.search(r"^X-MMS-PART-NAME:(.*)$", block, re.M)
        data = re.search(
            r"^X-MMS-PART-DATA:(.*?)(?=\nX-MMS-|\nEND:VPART|\Z)", block, re.M | re.S)
        if ctype and data:
            yield (date, ctype.group(1).strip(),
                   name.group(1).strip() if name else "", data.group(1))


def extract_mms_media(text, outdir, start=0):
    """Write image/video/audio attachments from an MMS .vmsg into outdir.

    `start` offsets the sequence number so several source files can share a
    folder without overwriting each other. Returns the number written.
    """
    Path(outdir).mkdir(parents=True, exist_ok=True)
    n = 0
    for i, (date, ctype, name, b64) in enumerate(iter_mms_parts(text), start):
        if not ctype.startswith(("image/", "video/", "audio/")):
            continue
        try:
            blob = base64.b64decode(re.sub(r"\s", "", b64))
        except Exception:
            continue  # a truncated part shouldn't abort the other 90
        stamp = _safe(date.replace("/", "").replace(":", "").replace(" ", "_"))
        fname = _safe(with_ext(_safe(name), ctype))
        Path(outdir, "%s_%03d_%s" % (stamp or "nodate", i, fname)).write_bytes(blob)
        n += 1
    return n


# --------------------------------------------------------------------------
# SIM contacts
# --------------------------------------------------------------------------

def parse_sim_rows(text):
    """`content query --uri content://icc/adn` output -> [(name, number)]."""
    out = []
    for line in text.splitlines():
        if not line.startswith("Row:"):
            continue
        name = re.search(r"name=(.*?), number=", line)
        number = re.search(r"number=(.*?), emails=", line)
        if not (name and number):
            continue
        n, num = name.group(1).strip(), number.group(1).strip()
        if num in ("", "NULL"):
            continue
        out.append((n if n not in ("", "NULL") else num, num))
    return out


def to_vcf(pairs):
    lines = []
    for name, number in pairs:
        lines += ["BEGIN:VCARD", "VERSION:3.0", "N:;%s;;;" % name,
                  "FN:%s" % name, "TEL;TYPE=CELL:%s" % number, "END:VCARD"]
    return "\r\n".join(lines) + "\r\n" if lines else ""


# --------------------------------------------------------------------------
# Device-side helpers
# --------------------------------------------------------------------------

def parse_glob_listing(text, suffix):
    """Keep real paths from an `ls -d <globs>` run.

    sh leaves a non-matching glob as the literal pattern, so any line still
    containing '*' matched nothing. Errors for missing roots go to stderr and
    never reach here.
    """
    out = []
    for line in text.splitlines():
        line = line.strip()
        if line and "*" not in line and line.lower().endswith(suffix) and line not in out:
            out.append(line)
    return out


def find_vendor_backups(adb, suffix=".vmsg", max_depth=5):
    """Locate vendor backup files on internal or removable storage.

    Uses shell globbing rather than `find`. Android 5's toolbox ships no `find`
    (nor `wc`/`head`, and its `ls -R` is not recursive) -- and Android 5 is
    precisely the vintage of phone this module exists for. Glob expansion is
    done by sh itself, so it works on every Android from 4 to 15.
    """
    found = []
    for root in STORAGE_ROOTS:
        globs = ["%s%s*%s" % (root, "/*" * d, suffix) for d in range(max_depth + 1)]
        # `ls` exits non-zero as soon as one glob matches nothing, and run()
        # throws away stdout on a non-zero exit -- so force a zero exit or the
        # paths that *did* match are lost with it.
        cmd = "ls -d %s 2>/dev/null; true" % " ".join(globs)
        try:
            out = adb.shell_text([cmd], timeout=60)
        except AdbError:
            continue
        for path in parse_glob_listing(out, suffix):
            if path not in found:
                found.append(path)
    return found


def scan_media(adb):
    """Make the Gallery notice files written by `adb push`.

    Pushed files are invisible until MediaStore indexes them; on Android 10+
    this is the supported trigger.
    """
    try:
        adb.shell_text(["content", "call", "--uri", "content://media",
                        "--method", "scan_volume", "--arg", "external_primary"],
                       timeout=120)
        return True
    except AdbError:
        return False


# --------------------------------------------------------------------------
# The two halves of the job
# --------------------------------------------------------------------------

def harvest_extras(adb, dest):
    """Recover SIM contacts, SMS and MMS pictures into an existing save folder.

    Runs alongside gui._pull_media, writing into the same folder so Step 2
    pushes everything back without knowing the difference:

        <dest>/Download/contacts-sim.vcf   import on the new phone
        <dest>/Download/sms-restore.xml    for SMS Backup & Restore
        <dest>/DCIM/OldPhone-MMS/          pictures pulled out of MMS

    Never raises -- a phone with no vendor backup simply yields zeros, and that
    must not fail the media transfer that already succeeded.
    """
    dest = Path(dest)
    found = {"sim_contacts": 0, "sms": 0, "mms_media": 0, "sources": []}

    try:
        rows = parse_sim_rows(adb.shell_text(
            ["content", "query", "--uri", "content://icc/adn",
             "--projection", "name:number"], timeout=30))
    except AdbError:
        rows = []
    if rows:
        d = dest / "Download"
        d.mkdir(parents=True, exist_ok=True)
        (d / "contacts-sim.vcf").write_text(to_vcf(rows), encoding="utf-8", newline="")
        found["sim_contacts"] = len(rows)

    try:
        backups = find_vendor_backups(adb)
    except AdbError:
        backups = []

    groups, seq = [], 0
    for remote in backups:
        local = dest / "vendor-backups" / _safe(remote.strip("/").replace("/", "_"))
        local.parent.mkdir(parents=True, exist_ok=True)
        try:
            adb.pull(remote, str(local), timeout=600)
            text = local.read_text(encoding="latin-1").replace("\r\n", "\n")
        except (AdbError, OSError):
            continue
        msgs = parse_vmsg(text)
        if msgs:
            groups.append(msgs)
            found["sources"].append(remote)
        # `start=seq` keeps attachment names unique across several backup files.
        found["mms_media"] += extract_mms_media(text, dest / MMS_FOLDER, start=seq)
        seq += 1000

    if groups:
        d = dest / "Download"
        d.mkdir(parents=True, exist_ok=True)
        merged = merge_messages(groups)
        write_sms_xml(merged, d / "sms-restore.xml")
        found["sms"] = len(merged)
    return found


def demo():
    sms = ("BEGIN:VMSG\nVERSION:1.1\nBEGIN:VCARD\nTEL:+61400000000\nEND:VCARD\n"
           "BEGIN:VBODY\nX-BOX:SENDBOX\nX-READ:UNREAD\nDate:2018/01/29 13:14:17\n"
           "Subject;ENCODING=QUOTED-PRINTABLE;CHARSET=UTF-8:=68=69=20=\n=74=68=65=72=65\n"
           "END:VBODY\nEND:VMSG\n")
    (m,) = parse_vmsg(sms)
    assert m["body"] == "hi there", m
    assert m["address"] == "+61400000000"
    # ZTE writes SENDBOX, not SENT -- getting this wrong files every sent
    # message as received, and the customer's threads read backwards.
    assert m["type"] == 2 and m["read"] == 0

    dup = merge_messages([[m], [m, dict(m, body="second", date=m["date"] + 1)]])
    assert len(dup) == 2, dup
    assert dup[0]["body"] == "hi there"

    xml_path = Path(os.environ.get("TEMP", ".")) / "_migrate_demo.xml"
    write_sms_xml(dup, xml_path)
    text = xml_path.read_text(encoding="utf-8")
    assert 'count="2"' in text and 'type="2"' in text
    assert 'address="+61400000000"' in text
    xml_path.unlink()

    assert with_ext("", "image/jpeg") == "attachment.jpg"
    assert with_ext("photo", "image/gif") == "photo.gif"
    assert with_ext("a.jpg", "image/jpeg") == "a.jpg"
    assert with_ext("x", "application/weird") == "x.bin"

    rows = parse_sim_rows(
        "Row: 0 name=Jo Bloggs, number=+61400000000, emails=NULL, anrs=NULL, _id=1\n"
        "Row: 1 name=NULL, number=NULL, emails=NULL, anrs=NULL, _id=2\n")
    assert rows == [("Jo Bloggs", "+61400000000")], rows
    assert "FN:Jo Bloggs" in to_vcf(rows)

    # Unmatched globs come back as literal patterns; only real paths survive.
    globbed = parse_glob_listing(
        "/sdcard/*.vmsg\n"
        "/storage/sdcard1/backup/Data/20260709151411/Sms/sms.vmsg\n"
        "/storage/sdcard1/*/*.vmsg\n", ".vmsg")
    assert globbed == [
        "/storage/sdcard1/backup/Data/20260709151411/Sms/sms.vmsg"], globbed

    mms = ("BEGIN:VMSG\nDate:2018/02/11 17:11:52\nBEGIN:VPART\n"
           "X-MMS-PART-CONTENT-TYPE:image/jpeg\nX-MMS-PART-DATA:aGVsbG8=\n"
           "END:VPART\nEND:VMSG\n")
    tmp = Path(os.environ.get("TEMP", ".")) / "_migrate_demo_mms"
    assert extract_mms_media(mms, tmp) == 1
    written = list(tmp.glob("*"))
    assert written[0].read_bytes() == b"hello"
    assert written[0].suffix == ".jpg", written  # unnamed part still got an extension
    written[0].unlink()
    tmp.rmdir()
    print("migrate.py demo OK")


if __name__ == "__main__":
    demo()
