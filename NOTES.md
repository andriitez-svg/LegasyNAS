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
  shell renaming — worth folding into the Phase 2 branding pass.
