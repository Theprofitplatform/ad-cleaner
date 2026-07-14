"""Regenerate stalkerware.py from Echap's ioc.yaml (run manually, needs internet).

ioc.yaml has one block per stalkerware family with sibling `packages:`,
`certificates:`, `websites:`, and `c2:` (domains/ips) lists. Only `packages:`
entries are real Android package ids -- the others are C2/marketing domains
that will never match a scanned package, so they're skipped rather than
bloating (and mislabeling) the list.

The starter set curated in stalkerware.py (well-known trade names such as
com.thetruthspy) is unioned in rather than replaced: Echap's own literal
package field for a family doesn't always match the family's public name.
"""
import re
import urllib.request
from pathlib import Path

URL = ("https://raw.githubusercontent.com/AssoEchap/"
       "stalkerware-indicators/master/ioc.yaml")

TARGET = Path(__file__).parent.parent / "stalkerware.py"

raw = urllib.request.urlopen(URL, timeout=30).read().decode("utf-8")

pkgs = set()
in_packages = False
for line in raw.splitlines():
    if re.match(r"^\s*packages:\s*$", line):
        in_packages = True
        continue
    if in_packages:
        m = re.match(r"^\s+- ([\w.]+)\s*$", line)
        if m:
            pkgs.add(m.group(1))
            continue
        in_packages = False
assert len(pkgs) > 100, f"suspiciously few ids parsed: {len(pkgs)}"

# Union with the current starter set so curated ids (e.g. com.thetruthspy)
# survive even when Echap's literal packages: field for that family differs.
current = TARGET.read_text(encoding="utf-8")
starter = set(re.findall(r'"([\w.]+)"', current))
pkgs = sorted(pkgs | starter)
print(f"{len(pkgs)} package ids ({len(starter)} starter + fetched)")

body = "\n".join(
    "    " + ", ".join(f'"{p}"' for p in pkgs[i:i + 4]) + ","
    for i in range(0, len(pkgs), 4)
)
new_set = "STALKERWARE = frozenset({\n" + body + "\n})"
updated = re.sub(r"STALKERWARE = frozenset\(\{.*?\}\)", new_set, current, flags=re.DOTALL)
TARGET.write_text(updated, encoding="utf-8")
print(f"wrote {TARGET}")
