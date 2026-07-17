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

## No cable? Connect over Wi-Fi

Some phones' USB ports (or cables) charge fine but can't carry data, so the
phone never shows up. If that happens, use **📶 Connect over Wi-Fi…** in the
connect wizard instead:

1. Make sure the **phone and this PC are on the same Wi-Fi network**.
2. On the phone: **Developer options → Wireless debugging** → turn it on.
3. The wizard **looks for the phone on your network and fills the addresses in
   for you** when it can (press **🔍 Find my phone** to look again). If it
   can't, type them from the phone's Wireless debugging screen.
4. The first time, tap **"Pair device with pairing code"** on the phone and
   enter the 6-digit code into the wizard.
5. Click **Connect**.

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
| **🛠 Restore default apps** | In an app's detail panel when it has hijacked your browser, texts, home screen or dialer. Hands the default back to the stock app. |
| **🔕 Stop its notifications** | In an app's detail panel. Silences an app that floods the phone with notifications. Reversible. |
| **🚫 Stop fake virus pop-ups (Chrome)** | On the Device tab. Silences Chrome's website notifications — the usual source of "your phone has a virus" scare pop-ups. |
| **💤 Disable preinstalled junk** | On the Device tab. Switches off known carrier/maker bloat (never uninstalls, so it can't brick the phone; reversible from History). |
| **📵 Block background data** | In an app's detail panel. Stops an app using mobile data in the background. Reversible. |
| **🛡️ Block ads (Private DNS)** | On the Device tab. Sends the phone's DNS through an ad-blocking resolver so ads are blocked in **every** app, even ones you keep. Reversible with **Turn off**. |
| **🗂 Find big files** | On the Device tab. Lists the biggest files on the phone's shared storage (old videos, downloads) so you can delete the ones you don't need. Deleted files are **copied to this PC first** so you can undo from History; files over 2 GB are too big to copy and are deleted permanently. |
| **🖐 Stop screen control** | In an app's detail panel. Switches off an app's ability to control the screen (its "accessibility" access) — useful for apps that block taps or won't let you uninstall them. Reversible from History. |
| **📲 Smart Switch (transfer data)** | On the Device tab. Opens Samsung's Smart Switch on the phone so you can send apps, texts and more to a new phone, cable-to-cable. |
| **🏪 Shop details…** | On the History tab. Enter your shop's name and contact details once and they'll print at the top of every receipt and condition report. |
| **📶 Connect over Wi-Fi…** | In the connect wizard. For phones whose USB port charges but won't carry data — connects wirelessly instead of over the cable. |

Anything marked with a 🔒 padlock is a protected system app and can't be changed.

The scan also flags **junk cleaner/booster/optimizer apps**, and each app's detail
panel shows extra evidence when the phone reports it: how much **battery** and
**background data** it uses, how long it's been **on screen recently**, and how
many **notifications** it posts. The Device tab shows the phone's **battery health**
(where the phone reports it) and its top battery user; both land on the clean receipt.

### Stalkerware

Ad Cleaner also flags known hidden tracking apps ("stalkerware") that someone
may have secretly installed to monitor the phone's owner. A match is always
marked **HIGH risk** with a caution note asking you to check with the phone's
owner privately before removing it, since doing so can alert whoever installed it.

Stalkerware detection data © Echap (stalkerware-indicators), CC-BY.

## History / Undo

Every change is recorded on the **History / Undo** tab. Select any entry and
click **Undo** to reverse it (re-enable an app, restore a pop-up permission, or
re-install something you removed) — this works even for apps that were
**sideloaded** (installed outside the Play Store), because Ad Cleaner saves a
copy of the app (`adcleaner_data/apk_backups/`) before removing it.

Files deleted with **🗂 Find big files** are copied to this PC
(`adcleaner_data/file_backups/`) before deletion, so those can be undone too —
except files **over 2 GB**, which are too big to copy and are deleted
permanently.

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
- **Phone charges but is never detected** → the cable or the phone's USB port
  may be data-dead — use **📶 Connect over Wi-Fi** instead.
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
