# Design — Private DNS ad blocking + Clean receipt

**Date:** 2026-07-13
**Status:** Approved (design), pending implementation plan
**Component:** Ad Cleaner (Windows/Tkinter + ADB Android ad-removal tool)

## Summary

Two new features for Ad Cleaner:

1. **System-wide ad blocking via Private DNS** — set Android's Private DNS to an
   ad-blocking resolver so ads/trackers are blocked in *every* app, including
   ones the user keeps. No root, one reversible settings change.
2. **Clean receipt** — a before/after report of what a clean did (apps
   removed/paused, pop-ups blocked, DNS state, space freed), shown as a dialog
   after CLEAN MY PHONE and exportable as printable HTML. Proof-of-work for
   cleaning family/customer phones.

Standard library only, no new dependencies, everything reversible and logged —
consistent with the existing app's safety-first design.

## Locked decisions

- **DNS provider:** a small picker — AdGuard (`dns.adguard.com`, ads + trackers),
  Cloudflare Family (`family.cloudflare-dns.com`, malware + adult), or a custom
  hostname the user types.
- **DNS placement:** opt-in control on the **Device** tab. CLEAN MY PHONE does
  **not** change DNS. (Explicit, reversible, least surprising.)
- **Receipt:** both — an automatic summary dialog after a clean **and** a manual
  export button on the History tab. Format: printable HTML.

## Approach

- DNS read/write/verify lives in `actions.py` (it already owns the ADB
  verify-after-act pattern and the append-only undo log).
- Report rendering lives in a **new pure `report.py`** (no device I/O →
  unit-testable in isolation).
- Both are wired into the existing Device tab, History tab, and clean flow.

Rejected alternatives: a separate `dns.py` (logic too small to earn a file);
inlining everything in `gui.py` (bloats the already-67 KB GUI and can't be
unit-tested).

---

## Feature 1 — Private DNS ad blocking

### Mechanism

Android exposes Private DNS through `settings global` (writable by `shell`,
which already holds `WRITE_SECURE_SETTINGS` — no root):

- **Enable:** `settings put global private_dns_mode hostname` then
  `settings put global private_dns_specifier <hostname>`
- **Disable:** `settings put global private_dns_mode off`
- **Read:** `settings get global private_dns_mode` and
  `settings get global private_dns_specifier`

`private_dns_mode` values seen in the wild: `off`, `opportunistic` (Automatic),
`hostname` (private provider). We treat anything that isn't `hostname` with our
specifier as "Off" for display purposes.

### `actions.py` additions

```python
DNS_PROVIDERS = {
    "AdGuard — blocks ads + trackers": "dns.adguard.com",
    "Cloudflare Family — malware + adult": "family.cloudflare-dns.com",
}

_HOSTNAME_RE = re.compile(r"^[a-z0-9.-]+$")   # trust-boundary check on custom input

def read_private_dns(adb) -> tuple[str, str]:
    """Return (mode, hostname). hostname is '' when mode != 'hostname'."""

def set_private_dns(adb, hostname, log):
    """Validate hostname, put mode=hostname + specifier, verify by read-back, log.
    Raises ValueError on a malformed custom hostname."""

def clear_private_dns(adb, log):
    """mode -> off, verify, log."""
```

- **Validation:** the custom hostname must match `^[a-z0-9.-]+$` and be
  non-empty. Provider-dropdown values are already known-good. (Subprocess args
  are passed as a list, so there's no shell-injection surface; the check is to
  reject typos before writing a broken setting.)
- **Verify:** after writing, read back and confirm mode/specifier match the
  intent, mirroring how `pause`/`uninstall` verify device state.
- **Undo:** logged to the action log for the record with action names
  `set-dns` / `clear-dns`, but **not** added to `UNDOABLE`. The On/Off buttons
  are the reversal path — a `ponytail:` comment documents this so it doesn't
  read as an oversight.

### GUI (Device tab)

A new "🛡️ Block ads system-wide (Private DNS)" group beneath the maintenance
buttons:

- Provider dropdown (the `DNS_PROVIDERS` labels + a "Custom…" entry).
- A hostname text field, enabled only when "Custom…" is selected.
- **Turn on** / **Turn off** buttons.
- A status line: `On — AdGuard (dns.adguard.com)` or `Off`.
- State is loaded via `read_private_dns` on connect and on Refresh; buttons are
  disabled until a phone is connected (same gating as the other Device-tab
  buttons via `_enable_btn`).
- A one-line muted note: blocks ads in every app; reversible any time; never
  touches photos/messages/accounts.

Runs on the background thread via the existing `_run_bg` / `_post` pattern; a
failure surfaces in the status bar (never crashes), matching current handlers.

---

## Feature 2 — Clean receipt

### Data captured

The clean flow captures storage free space before and after the clean by reading
`df /data` (reusing `device.parse_df`), then assembles a **receipt dict**:

```
{
  "when":            "2026-07-13 14:30:02",
  "model":           "SM G991B",
  "android":         "14",
  "stopped":         12,          # apps force-stopped
  "acted":           4,           # paused or uninstalled
  "removed":         False,       # True = uninstalled, False = paused
  "popups_blocked":  6,
  "packages":        ["com.random.adware", ...],
  "dns":             "On — AdGuard" | "Off",
  "freed_mb":        1180,        # storage_free_after - storage_free_before
}
```

- **`freed_mb`** is shown only when > 0. Pause mode frees no space (the app stays
  installed); the receipt states this plainly instead of inventing a number.
- Counts come from `clean_risky`'s existing return value
  (`{stopped, acted, removed, packages}`); `popups_blocked` is added to that
  return (count of `block-popup` "ok" results in `stop_all`).

### New module `report.py` (pure, no device I/O)

```python
def render_receipt_html(receipt: dict) -> str: ...   # post-clean receipt
def render_history_html(entries: list) -> str: ...   # full action-log export
def _html_page(title: str, body: str) -> str: ...    # shared header/footer/CSS
def demo(): ...                                       # assert HTML contains counts + packages
```

- Self-contained HTML (inline `<style>`), printable via the browser/OS print
  dialog. No external assets.
- `render_history_html` takes `log.recent()` (newest first) and renders a table:
  time, app, action, result.

### GUI wiring

- **After CLEAN MY PHONE** (`_clean_done`): show a summary dialog with the
  headline numbers; a **Save receipt** button opens a file dialog
  (`filedialog.asksaveasfilename`, default `AdCleaner-receipt-<date>.html`) and
  writes `render_receipt_html(receipt)`.
- **History tab:** a **💾 Save report** button → `render_history_html` → file
  dialog → write. Enabled whenever there is at least one log entry.
- `_start_clean` reads storage-free before; `_clean_done` reads it after and
  builds the receipt.

---

## Files & tests

| File | Change |
|---|---|
| `actions.py` | + `DNS_PROVIDERS`, `read/set/clear_private_dns`, hostname validation; extend `demo()` FakeAdb to handle `settings get/put global private_dns_*` and assert read-back + validation rejection |
| `report.py` | **new** — `render_receipt_html`, `render_history_html`, `_html_page`, `demo()` |
| `gui.py` | Device-tab DNS group + handlers; clean-flow storage delta + receipt dialog + save; History export button |
| `device.py` | reuse `parse_df` (no change expected) |

### Test coverage

- `report.demo()` — build a sample receipt with 4 removed + 6 pop-ups + 1180 MB
  freed; assert the HTML contains those counts and a package name; assert pause
  mode omits the freed-space figure.
- `actions.demo()` extension — `set_private_dns` writes and verifies against a
  FakeAdb settings store; `clear_private_dns` returns mode to off; a malformed
  custom hostname raises `ValueError`.
- Existing `pytest` suite continues to pass.

## Non-goals

- No scheduled/automatic DNS enforcement — it's a manual opt-in toggle.
- No per-app DNS or per-app firewalling (out of scope; possible later feature).
- No cloud/online lookups — the app stays fully offline, no new dependencies.
