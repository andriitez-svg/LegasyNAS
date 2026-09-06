# Baseline notes (Phase 0)

Pulled fresh from the live NAS (root@192.168.8.110) on 2026-09-05. See
`NAS_REFERENCE.txt` for the exact OS/kernel this was captured against.

- `filemanager.py`, `desktop.html`, `usb-automount`, `99-usb-automount.rules`
  matched the local scratchpad copies byte-for-byte — no drift.
- `fetcher.py` and `serve.py` did not previously exist locally; pulled fresh.
- `console.html` (432KB, last modified Aug 20 — two weeks before
  `desktop.html`'s last edit) sits in `/home/debian` on the NAS but is not
  referenced by any systemd unit, by `serve.py`, or by any other file here.
  It appears to be a superseded predecessor of `desktop.html` left over from
  before it was renamed. Kept in this baseline commit since it's part of
  what's actually deployed, but it is dead weight — flag to the user before
  deleting it from the live box (not done as part of this baseline).
- All three services (`fetcher`, `filemgr`, `desktop`) run as the `debian`
  user via systemd, `WorkingDirectory=/home/debian`, `ExecStart=/usr/bin/python3
  /home/debian/<script>.py` — these unit files are themselves part of what
  needs generalizing in a later phase (hardcoded `/home/debian` install path).
- `smb.conf`'s `netbios name = LS210` / `server string = LS210 NAS` are
  additional LS210-branded strings not covered by the current plan's desktop
  shell renaming. Still not touched as of Phase 3 - deliberately left alone,
  since renaming a live NAS's NetBIOS identity changes how it appears on the
  network (saved bookmarks, network browsing), which is a user-visible change
  beyond a source refactor. Flag to the user before changing it.

## Phase 3 update

- `ls210-power` (the static sudoers file captured in the Phase 0 baseline) is
  superseded by `install.sh`'s `install_power_sudoers`, which resolves the
  real reboot/poweroff paths via `command -v`, generates
  `/etc/sudoers.d/nas-control-plane-power`, and validates it with
  `visudo -c -f` before ever installing it. The old file has been removed
  from both the live NAS and this repo - `visudo -c` and a live `sudo -n
  reboot --help` were both re-checked after removing it.
- USB automount's `add` path now independently re-checks `ID_BUS=="usb"` and
  a genuinely-removable parent disk (via `udevadm info` + sysfs) before ever
  mounting anything, on top of the udev rule's own matching - verified
  end-to-end with a real USB stick (mounted correctly, writable, picked up
  the right icon in the Files app) and confirmed to correctly refuse both
  the internal disk and a non-USB loopback device.
- The removal path (udev rule's `ENV{ID_BUS}=="usb"`-only remove match, plus
  usb-automount's unchanged-from-Phase-1 cleanup logic) has now also been
  confirmed against a real physical unplug: `journalctl -t usb-automount`
  shows `cleaned up /var/downloads/USB/Z (was /dev/sdb1)` immediately after
  the stick came out, and `/var/downloads/USB/` was back to empty. The full
  add-then-remove lifecycle is verified end-to-end through the real hotplug
  path, not simulated. No open items remain from Phase 3.

## Phase 4 update

- `install.sh` now actually installs the app files and systemd units, not
  just the config/udev/sudoers pieces from Phases 1 and 3. The static
  `filemgr.service`/`fetcher.service`/`desktop.service` files captured in
  the Phase 0 baseline are removed from this repo - `templates/*.tmpl` +
  `install.sh` are now the single source of truth that generates them, the
  same way `ls210-power` was superseded in Phase 3.
- Found and fixed during testing: `install.sh` ran its own `smoke_test.sh`
  immediately after restarting all three services, with no settle delay -
  on this single-core box that's occasionally too fast, and the same
  restart-race that showed up manually during Phase 1 testing. Added a
  3-second sleep first.
- Also fixed: `smoke_test.sh`'s default host was hardcoded to
  `192.168.8.110` - itself a hardcoded-to-this-device assumption, worth
  catching precisely because this project is being generalized. Now
  defaults to `localhost`, matching how `install.sh` invokes it.
- Verified live: ran the full installer twice against the real NAS
  (pinned via `NAS_CP_SERVICE_USER=debian NAS_CP_INSTALL_DIR=/home/debian`
  so it reconciled with the existing install instead of relocating it) -
  first run succeeded end-to-end, second run confirmed idempotency (no
  duplicate units, sudoers entries, or udev rules; checksums of the
  redeployed app files matched every prior phase's recorded values exactly,
  i.e. no drift was introduced).
