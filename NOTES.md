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

## Post-Phase-4: optional login (user-requested, beyond the original 5-phase plan)

- Added a shared login gate across all three services. Session "tokens" are
  self-verifying (expiry + HMAC of that expiry, signed with a secret shared
  by all three via `/etc/nas-control-plane-auth.conf`) rather than stored in
  a session table, because the three Python processes are independent with
  no shared memory - a signed value lets each one verify a cookie set by
  either of the others with no IPC needed. Verified this actually works
  end-to-end: logging in against serve.py alone produced a cookie that
  filemanager.py and fetcher.py - never logged into directly - both accepted.
- Password is never stored - only a PBKDF2-SHA256 hash (200k iterations) +
  random salt, in a file mode 0600 owned by the service account (not even
  world-readable). Login itself is throttled with a 1-second sleep on a
  failed attempt.
- Auth defaults to off and stays off until `install.sh`'s new `install_auth`
  step actually creates the credentials file - confirmed via a real
  before/after deploy: pushed the auth-aware code first with no auth file
  present and re-ran the full smoke test to prove zero behavior change,
  *then* ran `install_auth` to actually turn it on.
- **Real bug found via live testing, not just written and assumed correct**:
  the original "Log out" button tried to clear the session cookie with
  `document.cookie` from page script. That cannot work - `HttpOnly` (set
  deliberately so an XSS bug can't steal the session either) blocks script
  from clearing the cookie, not just reading it. The user tested this in
  their real browser twice: first confirming it was actually broken (not a
  quirk of my own sandboxed test browser, which has separate, unrelated
  limitations with cross-origin iframes noted earlier), then confirming the
  fix - a genuine `/logout` endpoint that clears the cookie via a real
  `Set-Cookie` response header - actually works.
- Also fixed while wiring this up: `serve.py`'s login redirect originally
  sent the browser to `/`, which - unlike `filemanager.py`/`fetcher.py` -
  this service has no handler for, so it fell through to
  `SimpleHTTPRequestHandler`'s raw directory listing instead of the desktop
  shell. Redirects to `/desktop.html` now.
- `smoke_test.sh` gained login-aware checks (`NAS_CP_TEST_USER`/
  `NAS_CP_TEST_PASSWORD`): if set, it logs in first and carries the cookie
  through every other check, and adds two auth-specific assertions
  (unauthenticated request is gated; login on one port authenticates a
  different one). Without those env vars it behaves exactly as before, for
  testing a NAS with login left off.
- Live NAS now has login enabled (credentials chosen by the user, not
  recorded here), set via `install.sh`'s interactive/env-var flow. The
  generated hash/salt/secret were verified against the live
  `/etc/nas-control-plane-auth.conf` (0600, owned by `debian`, no plaintext
  password present).

## Post-Phase-4: change password from the GUI (user-requested follow-up)

- User pointed out there was no way to change the password without SSH
  access and re-running `install.sh`. Added a "Change password" control to
  the Security panel (`POST /change-password` on serve.py, since that's
  where the GUI lives).
- This required a real architectural fix first: all three services were
  caching the auth file's contents once at process startup, so a password
  changed through serve.py wouldn't have been noticed by filemanager.py/
  fetcher.py until they were restarted. Switched all three to read
  `/etc/nas-control-plane-auth.conf` fresh on every auth-related call
  instead - the file is tiny and requests to this box are infrequent, so
  the extra file read per request is negligible, and it closes a whole
  class of "the three services disagree about who's allowed in" bugs, not
  just this one.
- Changing the password rotates `AUTH_SECRET` too, which invalidates every
  outstanding session cookie everywhere (including the one used to submit
  the change) - standard practice after a password change. The client
  redirects to `/logout` right after a successful change to tidy up the
  now-dead cookie and land back on the sign-in page.
- The write itself is a direct in-place rewrite of the existing file, not a
  temp-file-then-atomic-rename - the service account owns the *file*
  (mode 0600) but not the `/etc` *directory* entry, so creating a new
  temp file there to rename into place would fail. A plain rewrite of a
  few dozen bytes was judged an acceptable tradeoff over engineering true
  atomicity for a home-NAS password file; a failure mid-write leaves
  `AUTH_HASH` missing, which fails safe (auth just turns back off) rather
  than locking anyone out or leaving a corrupt-but-accepted state.
- Verified directly against the live NAS (not just locally): logged in,
  changed the password to a temporary value via `curl`, confirmed the old
  password stopped working and the temp one worked, confirmed the auth
  file's ownership/permissions were unchanged after the in-place rewrite,
  then changed it back to the user's original password and reconfirmed
  that works - the live NAS ends this session with the same credentials
  the user originally chose.

## Project renamed to LegasyNAS

User named the project "LegasyNAS" (intended for the GitHub repo) and asked
for the internal naming to match throughout, not just the repo/folder name.
Renamed everywhere the old `nas-control-plane`/`nascp` naming appeared:

- `/etc/nas-control-plane.conf` → `/etc/legasynas.conf`
- `/etc/nas-control-plane-auth.conf` → `/etc/legasynas-auth.conf`
- `/etc/sudoers.d/nas-control-plane-power` → `/etc/sudoers.d/legasynas-power`
- `config/nas-control-plane.conf.default` → `config/legasynas.conf.default`
- Default install dir suggested by `install.sh`: `/opt/nas-control-plane` →
  `/opt/legasynas`
- Session cookie `nascp_session` → `legasynas_session`; CSRF-mitigation
  header `X-NASCP-Confirm` → `X-LegasyNAS-Confirm`
- localStorage keys `nascp.wallpaper`/`.theme`/`.accent`/`.opacity.` →
  `legasynas.*`
- Installer env var prefix `NAS_CP_*` → `LEGASYNAS_*` (also renamed in
  `smoke_test.sh`'s `LEGASYNAS_TEST_USER`/`LEGASYNAS_TEST_PASSWORD`)
- Visible branding: page `<title>`, the shell's `.brand` text, and
  console.error prefixes all changed from "NAS Desktop" to "LegasyNAS";
  `desktop.service`'s systemd Description likewise
- Local project folder and README title/clone URL: `nas-control-plane` →
  `LegasyNAS`

Deliberately left as-is: this file's own historical entries above (accurate
record of what the names were at the time each phase happened, not
retroactively rewritten), and `smb.conf`'s `LS210` NetBIOS branding /
`console.html` (both already flagged in earlier notes as the user's call,
unrelated to this rename).

Live migration was done as a distinct sequence, not a single blind
find-and-replace deploy, specifically because this touches the sudoers file
and the auth file: snapshotted every file about to change; copied
`/etc/nas-control-plane.conf`/`-auth.conf` to their new names (`cp -p`,
preserving ownership/permissions - verified byte-identical content after
copying); deployed the renamed app files + regenerated systemd units (new
`EnvironmentFile=` path) + regenerated udev rule/script, restarted, and
smoke-tested with credentials before touching anything further; copied the
sudoers file to its new name and validated with `visudo -c` *while both old
and new files were still present*, confirmed `sudo -n reboot` still worked,
only then deleted the old sudoers file and re-validated; only after every
new file was proven working did the old `/etc/nas-control-plane*.conf`
files get deleted. Rollback snapshot removed only at the very end, once a
full authenticated smoke test and a real logged-in browser session (showing
"LegasyNAS" as both the tab title and the shell's brand text) both
confirmed the migration.

One expected side effect, not a bug: anyone with an existing browser session
will see the sign-in page again after this deploy (the old `nascp_session`
cookie is simply no longer recognized under its new name) and will have
lost any saved theme/wallpaper/accent choice stored under the old
`nascp.*` localStorage keys, reverting to the current default (dark).

## Big-file transfers: "12 GB copy dies mid-transfer" and "Files UI freezes"

User report: copying a ~12 GB file off the NAS failed partway, whether the
destination was the PC or an external drive on the mini PC; copying through the
NAS's own USB port worked but the Files UI lagged and froze.

**Reproduced first, before changing anything.** The same 13.2 GB file was read
end to end through four independent client paths from the mini PC - `smbclient`
(SMB), the Files app's HTTP `/download` (curl), GVFS `gio copy` to local disk
(the stack Nemo uses), and the kernel CIFS mount the user has at
`/mnt/nas-downloads` (which is mounted `soft`). All four completed with exit 0
at 30-39 MB/s, NAS up throughout, no OOM/I-O/NIC errors (`dmesg`, NIC counters
all zero). So the NAS's serving side does not drop a 12 GB transfer on its own.
The earlier reboot (Sep 18) was a clean shutdown, not a crash.

**Two real causes found:**
1. *The freeze.* `filemanager.py` was a single-threaded `TCPServer`, and
   copy/move/zip ran inside the HTTP request - so one long download or copy held
   the only handler and every other request queued behind it. Measured: while a
   12 GB download ran, the Files page timed out at 15 s (Fetcher and the desktop
   shell, separate processes, answered in <1 s). The old code also wrapped copy
   /move/zip/extract in `except OSError: pass`, so a copy that died partway left
   a truncated file wearing the real name and told nobody.
2. *The mid-transfer failure onto the external drive.* The drive on the mini PC
   (`/dev/sda`, label K) is **FAT32**; `os.pathconf(..., PC_FILESIZEBITS)` reports
   32 bits (4 GiB max) and `truncate -s 5G` there fails with "File too large".
   A 12 GB file cannot be written to it, full stop. (The mini PC's kernel log
   also shows that drive/its USB hub disconnecting repeatedly, which would kill
   any long copy independently.)

**Not fully explained:** the "copy to the PC's internal disk also fails" case.
That disk is ext4 with plenty of room and the read paths all worked, so no
NAS-side cause was found; it may have been the same FAT32 drive, or a client
problem. If it recurs, the exact error text and how far it got is the missing
information.

**Fixes (all in `filemanager.py`):**
- Threaded server (`ThreadingMixIn`, daemon threads, 1 MB stack, TCP keepalive
  so a vanished client can't pin a thread).
- Copy/move/zip/extract run as background jobs with a live progress strip
  (`/jobs`, `/jobs-dismiss`). Copies go through `<name>.part`, fsync'd, then
  renamed - a failed copy never leaves a truncated file, and "done" means the
  bytes are on the device. Free space is checked up front; moves within one
  filesystem stay an instant rename; a cross-filesystem move deletes the
  source only after the copy completes; copying a folder into itself is
  refused. Failures are reported with the reason. Zips store already-
  compressed formats (.gguf, .safetensors, video...) instead of deflating them.
- FAT32 destinations: a file (or a folder containing one) over 4 GiB - 1 is
  refused *before* copying with a plain explanation, instead of dying 4 GB in.
- Downloads/previews: `Range` + `If-Range` + `ETag`/`Last-Modified` (resumable
  downloads, seekable video previews), served with `sendfile`. Measured on the
  NAS: 12 GB download 59 MB/s vs 37 MB/s before. Also fixed: a filename with any
  non-latin-1 character used to raise mid-response and kill the download; now
  sent as RFC 5987 `filename*`.

**Verified on the live NAS** (not just locally): Files page answers in 0.02-0.05
s during a 12 GB download (was: timed out); interrupted a real 650 MB download,
resumed with `curl -C -`, md5 identical to the one the NAS computes; a real copy
job returned in 0.7 s, reported 1%->100%, page answered in 0.25 s mid-copy,
result byte-identical (`cmp`), no `.part` left, mtime preserved; using
temporary ext4 loop "sticks" mounted inside the share: a cross-filesystem move
(copy, verify, then delete source), a too-big copy refused instantly with exact
numbers, and a fitting copy into a nearly-full stick - all correct. All test
artifacts (loop images, mounts, test folders, cookie jar) were removed and
`/var/downloads/USB` checked empty afterwards. `tests/test_filemanager_bigfiles.py`
(38 checks, temp dir only) covers this locally.

Not testable without hardware: the FAT32 guard against a physical stick (its
logic is tested by mocking the mount type; `fs_type_of` was checked against
the real vfat/ext4/cifs mounts on the mini PC). A rollback copy of the previous
`filemanager.py` was left at `/root/filemanager.py.before-bigfile-fix` on the NAS.
