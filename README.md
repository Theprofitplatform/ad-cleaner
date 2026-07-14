# Ad Cleaner — remove pop-up ad apps from an Android phone

A simple Windows program that connects to an Android phone with a USB cable and
helps you **close, pause, or remove the downloaded apps that cause pop-up ads**.
Everything is controlled from the computer, so pop-ups on the phone can't get in
your way.

It never touches your photos, messages, or the phone's built-in system apps.

---

## Quick start (the easy way)

1. **Double-click `AdCleaner.exe`.** Nothing to install — everything it needs is
   inside the one file.
   > The first time, Windows may show a blue **"Windows protected your PC"** box.
   > Click **More info → Run anyway**. This appears for any program not made by a
   > big company; the app is safe and does not touch your files.

2. **Connect the phone.** The app shows you a short checklist and **ticks each
   step green by itself** as you plug in and allow the phone. Pick your phone
   brand from the drop-down to see exactly where the settings are.

3. When the top turns **green** with your phone's model, click the big green
   **✨ CLEAN MY PHONE** button.

4. Read the one pop-up, click **Yes**. Done! It closes every downloaded app,
   blocks pop-ups, and pauses the worst offenders. **Nothing is deleted** — you
   can undo anything from the **History** tab.

5. *(Optional)* Look through the list and press **Uninstall** on anything you
   don't want to keep.

That's it. One button does the whole job.

---

## Connecting the phone (one time)

The app walks you through this on screen, but here it is in full:

1. On the phone: **Settings → About phone**.
2. Tap **Build number** seven times, until it says *"You are now a developer."*
   *(On Xiaomi/Redmi it's "MIUI version"; on some phones it's under "Software
   information".)*
3. Go to **Developer options** and turn on **USB debugging**.
4. Plug the phone into the computer with a USB cable.
5. On the phone, tap **Allow** when it asks about USB debugging.
   Tick **"Always allow from this computer."**

## If pop-up ads are covering the screen

Some ad apps throw up pop-ups so fast you can't tap anything. Start the phone in
**Safe Mode** first — Safe Mode stops downloaded apps from running, so nothing
can cover the screen while you work:

- Hold the **Power** button.
- Press and **hold** the on-screen **"Power off"**.
- Tap **Safe Mode** when it appears.

Connect the phone and use **CLEAN MY PHONE** (or **Pause** / **Uninstall** on
individual apps), then restart the phone normally.

---

## What the buttons do

| Button | What happens |
|---|---|
| **✨ CLEAN MY PHONE** | The easy one. Closes all downloaded apps, blocks pop-ups, and pauses the risky ones — in one click. Nothing deleted. |
| **⏹ STOP ALL** | Just closes every downloaded app right now (no pausing). |
| **⏸ Pause** | Freezes one app so it can't run. Fully reversible with **Resume**. |
| **▶ Resume** | Un-freezes a paused app. |
| **🗑 Uninstall** | Removes an app for you. Restore it later from the **History** tab. |
| **🛡️ Block ads (Private DNS)** | On the Device tab. Sends the phone's DNS through an ad-blocking resolver so ads are blocked in **every** app, even ones you keep. Reversible with **Turn off**. |

Anything marked with a 🔒 padlock is a protected system app and can't be changed.

## History / Undo

Every change is recorded on the **History / Undo** tab. Select any entry and
click **Undo** to reverse it (re-enable an app, restore a pop-up permission, or
re-install something you removed).

After a clean, Ad Cleaner saves a **printable receipt** (in `adcleaner_data/reports/`)
listing what it closed, blocked and removed — handy when cleaning someone else's
phone. The **Export report** button saves the full history the same way.

---

## Giving this to family (or cleaning several phones)

Just **send them the single `AdCleaner.exe` file** (email, USB stick, whatever).
They double-click it — no Python, no internet, no setup on the computer. The
same file works for any Android phone: Samsung, Google, Xiaomi, Motorola, etc.

---

## Moving to a new phone

The **Move to new phone** tab copies your **photos, videos, music, Downloads and
documents** from an old Android to a new one, using this PC in between. Nothing
is deleted from either phone.

1. Plug in the **old** phone → **⬇ Save photos & files to this PC**.
2. Unplug it, plug in the **new** phone → **⬆ Copy onto the new phone**.

**Contacts, texts and apps don't travel over the cable** — Android blocks that on
purpose. The easy built-in ways:
- **Contacts & calendar** already ride on your Google account — just sign into
  the same account on the new phone.
- **Apps, texts and the rest** → use the new phone's own **"Copy apps & data"**
  wizard during setup (on Samsung it's **Smart Switch**). It moves everything,
  cable-to-cable, no PC needed. The tab has a link with step-by-step help.

---

## Troubleshooting

- **"Windows protected your PC"** → Click **More info → Run anyway**.
- **"Phone found — tap Allow"** → Look at the phone and tap **Allow**.
- **Nothing is detected** →
  - The cable may be **charge-only** — try a different USB cable.
  - Try another USB port.
  - Make sure **USB debugging** is turned on (steps above).
  - Install a phone driver on this PC: **Samsung USB drivers** for Samsung
    phones, or a **universal ADB driver** for any brand.
- **An app won't uninstall** → It may be a "device administrator". The program
  tries to remove that automatically; if it still won't go, the window shows what
  to do.

---

## For developers

Plain Python 3.11+ / Tkinter, standard library only. ADB is called as a
subprocess — no third-party ADB library. The packaged exe **bundles** Google's
platform-tools so end users need no download.

```
python main.py            # run from source (auto-downloads ADB once if missing)
python -m pytest          # parser / scoring / safety / GUI tests
build.bat                 # download+bundle ADB, produce dist\AdCleaner.exe
```

| File | Purpose |
|---|---|
| `main.py` | Entry point. |
| `gui.py` | Tkinter window, connect wizard, one-click clean, threading, dialogs. |
| `adb.py` | Locate `adb.exe` (incl. bundled), run commands, parse `devices`. |
| `scanner.py` | App inventory + suspicion scoring (tune weights at the top). |
| `actions.py` | Stop-all / clean / pause / resume / uninstall + undo log. |
| `protected.py` | The list of apps that can never be changed. |
| `setup_helper.py` | First-run checks + ADB download (source mode). |

**Intended use:** manages apps on a phone you own and have physically connected
with USB-debugging consent. It has no remote-access, hidden-operation, or
authorization-bypass features and never will.
