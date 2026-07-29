# Field-test checklist

Three features are merged but have never run on a live phone. Each one has a
specific thing that unit tests cannot prove, because it depends on what a real
handset prints. This is the shortest path through all three.

Work top to bottom on the next customer phone. Each check says what "pass"
looks like and what to write down if it fails, so a failure is actionable
rather than "it didn't work".

Record results in the session notes or paste them back into Claude.

---

## Before you start

| | |
|---|---|
| **Best phone for this** | An older Android (5–9) with a SIM, some photos, WhatsApp installed, and real ad-adware if you can get one |
| **Time** | ~15 minutes |
| **Risk** | None of these checks change the phone. The Move-tab save only reads. |

```
platform-tools\adb.exe devices -l
```
Expect one line ending `device`. If it says `unauthorized`, unlock the phone and
tap Allow. If nothing lists, reseat the cable — old micro-USB ports are loose.

Write down: **model, Android version, serial.**

---

## 1. Watch mode — v1.9.0

**What's unproven:** `dumpsys window` output format drift between OEMs. The
parser was built to the documented layout; no real handset has been checked.

```
platform-tools\adb.exe shell dumpsys window windows > win.txt
```

Open `win.txt` and confirm all three of these appear:

- [ ] `package=` (or `Window{... u0 <package>/...}`)
- [ ] `ty=` — the window type
- [ ] `mHasSurface=`

**Pass:** all three present, and `Watch for pop-ups` lists windows when run.
**Fail:** note which field is missing or renamed, and paste ~20 lines of
`win.txt` around a real app window. The parser keys on those exact names.

Then, with the tab open, trigger a pop-up (browse a junk site on the phone).

- [ ] An overlay sighting appears within a second or two
- [ ] The app named is the one actually drawing over the screen

> Known ceiling: a sub-second pop-up can slip between polls. A miss on a very
> brief flash is expected, not a bug.

---

## 2. Fake-icon detector — v1.10.0

**What's unproven:** the distance threshold. It was tuned against *synthetic*
rendered icons, not real Play-listing artwork versus a real APK's icon. If it's
mis-tuned this is noise on every scan — this is the most likely of the three
to need a change.

Run a normal scan on a phone with mainstream apps installed.

- [ ] No genuine app is flagged "Pretends to be …" (WhatsApp, Chrome,
      Messenger, Facebook, Instagram)

A single false positive here matters more than a missed detection: a tech who
sees the warning on a real WhatsApp learns to ignore it.

- [ ] `adcleaner_data/icon_spoof.json` gets written, with negative verdicts too

If you have a phone with an actual impersonator:

- [ ] It's caught, and the reason names the brand it's copying

**Fail either way:** note the app, the brand, and the distance recorded in
`icon_spoof.json`. `LOOKALIKE_MAX_DISTANCE` (currently 10) is the knob.
Real-artwork distances are the number we've never had.

---

## 3. Move to new phone — v1.11.0

**What's unproven:** `harvest_extras` has never run end-to-end. Every command in
it was run by hand during the ZTE job, but not through the app.

### 3a. On the OLD phone

If it has a vendor backup app (ZTE Backup, LG Mobile Switch, Alcatel Backup),
**run it first and back up messages to storage.** Without that there is no SMS
to recover and this part of the test is skipped — note that if so.

Press **⬇ Save photos & files to this PC**.

- [ ] Finishes without a `⚠ Couldn't finish` warning
- [ ] The status line reports contacts — and SMS, if a vendor backup existed
- [ ] `adcleaner_data/transfers/<phone>_<date>/` contains `Download/contacts.vcf`

Open `contacts.vcf` in Notepad:

- [ ] It holds the owner's real contacts, not just carrier entries like
      "Message Bank" or "Vodafone Care"

> This is the check that matters most. Phone-memory contacts were assumed
> unreadable until this session proved otherwise, so this path is the newest
> and least exercised code in the feature.

If the phone has WhatsApp:

- [ ] An `OtherMedia/` folder appeared, with WhatsApp pictures in it

> Since Android 11 WhatsApp stores pictures outside DCIM/Pictures/Movies, so
> this folder is the only reason they get copied at all.

If SMS was recovered, open `Download/sms-restore.xml` in Notepad and check a
handful of messages:

- [ ] Sent messages show `type="2"`, received show `type="1"`

> Get this backwards and every thread reads inside-out. It's the bug that hit
> the ZTE — it wrote `SENDBOX` where the spec says `SENT`. If a different
> vendor uses yet another spelling, this is where it shows up: **all** messages
> would be `type="1"`.

### 3b. Space check

With a phone fuller than the PC's free space (or temporarily fill the drive):

- [ ] Pressing Save warns first and lets you cancel

### 3c. On the NEW phone

Press **⬆ Copy onto the new phone**.

- [ ] Photos appear in the **Gallery**, not just in Files

> This is the bug fixed this session — pushed files stay invisible until
> MediaStore reindexes. If they're in Files but not the Gallery, the rescan
> didn't fire; grab the exact Android version.

- [ ] `📦 Install a tool` lists APKs from `adcleaner_data/tools/` and installs one

Then finish the migration properly:

- [ ] SMS Backup & Restore restores `sms-restore.xml`, and threads read the
      right way round with correct dates

---

## Reporting back

For anything that failed, the useful details are:

- Phone model + Android version
- The exact command output (`win.txt` extract, the JSON entry, the XML line)
- What you expected versus what happened

Guessing at a fix without the raw output is how the icon threshold ended up
tuned against synthetic data in the first place.
