"""Identify a plugged-in phone from Windows USB descriptors — no ADB, no
developer options. A phone in charge-only/MTP mode still announces its
brand, user-visible name, and serial number to Windows the moment the cable
goes in; this reads them so the app can greet the phone before USB debugging
is enabled.

Pure pairing logic (pair_phones) + a thin detect_phones() that shells out to
PowerShell's CIM query. Non-Windows / any error -> [].
"""

import json
import os
import re
import subprocess
import time

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0

# One CIM query: WPD entries carry the phone's friendly name; the sibling
# USB composite device (same VID&PID) carries the real serial number.
_PS_QUERY = (
    "[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
    "Get-CimInstance Win32_PnPEntity | "
    "Where-Object { $_.PNPClass -eq 'WPD' -or $_.PNPDeviceID -like 'USB\\VID_*' } | "
    "Select-Object PNPClass,Name,Manufacturer,PNPDeviceID | ConvertTo-Json -Compress"
)

_VIDPID = re.compile(r"VID_[0-9A-F]{4}&PID_[0-9A-F]{4}", re.IGNORECASE)


def pair_phones(entities):
    """PnP entity dicts -> [{'name', 'brand', 'serial'}], one per WPD device.

    An entity is {'PNPClass', 'Name', 'Manufacturer', 'PNPDeviceID'}. The WPD
    entry gives the name; its serial lives on the composite parent whose
    instance id is USB\\VID_xxxx&PID_xxxx\\<serial> (a Windows-generated id
    contains '&' and is not a real serial).
    """
    phones = []
    for e in entities or []:
        if (e.get("PNPClass") != "WPD"
                or not (e.get("PNPDeviceID") or "").upper().startswith("USB\\")):
            continue
        m = _VIDPID.search(e["PNPDeviceID"])
        serial = brand = ""
        if m:
            prefix = ("USB\\" + m.group(0)).upper()
            for s in entities:
                sid = s.get("PNPDeviceID") or ""
                head, _, tail = sid.rpartition("\\")
                if head.upper() == prefix and tail and "&" not in tail:
                    serial = tail
                    brand = s.get("Manufacturer") or ""
                    break
        phones.append({"name": e.get("Name") or "",
                       "brand": brand or e.get("Manufacturer") or "",
                       "serial": serial})
    return phones


_cache = (0.0, [])


def detect_phones(ttl=10):
    """Phones currently visible on USB (cached for ttl seconds — the CIM
    query costs ~1s and the GUI polls every 2s)."""
    global _cache
    if os.name != "nt":
        return []
    stamp, phones = _cache
    if time.time() - stamp < ttl:
        return phones
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-Command", _PS_QUERY],
            capture_output=True, timeout=15, creationflags=_NO_WINDOW)
        data = json.loads(proc.stdout.decode("utf-8", "replace") or "[]")
        if isinstance(data, dict):    # ConvertTo-Json unwraps single results
            data = [data]
        phones = pair_phones(data)
    except Exception:
        phones = []
    _cache = (time.time(), phones)
    return phones


def demo():
    entities = [
        {"PNPClass": "WPD", "Name": "Abhishek's S26 Ultra", "Manufacturer": "samsung",
         "PNPDeviceID": "USB\\VID_04E8&PID_6860&MS_COMP_MTP&SAMSUNG_ANDROID\\7&3156409A&0&0000"},
        {"PNPClass": "USB", "Name": "SAMSUNG Mobile USB Composite Device",
         "Manufacturer": "SAMSUNG Electronics Co., Ltd.",
         "PNPDeviceID": "USB\\VID_04E8&PID_6860\\R5GL24XWASL"},
        {"PNPClass": "USB", "Name": "USB Composite Device", "Manufacturer": "(Generic)",
         "PNPDeviceID": "USB\\VID_046D&PID_C52B\\6&1CA3507D&0&11"},  # windows-made id, no serial
    ]
    assert pair_phones(entities) == [{
        "name": "Abhishek's S26 Ultra",
        "brand": "SAMSUNG Electronics Co., Ltd.",
        "serial": "R5GL24XWASL"}]
    # WPD device with no matching composite sibling -> falls back to its own strings
    assert pair_phones(entities[:1]) == [{
        "name": "Abhishek's S26 Ultra", "brand": "samsung", "serial": ""}]
    assert pair_phones([]) == [] and pair_phones(None) == []
    print("usbinfo.py demo OK")


if __name__ == "__main__":
    demo()
