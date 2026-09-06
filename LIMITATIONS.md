# Known limitations

Running list, appended to as generalization work proceeds. Promoted into the
final README in Phase 5.

- **Login is opt-in and off by default.** `install.sh` can set up a
  username/password for the Files app, Fetcher, and the Desktop shell
  (a shared, cross-port session cookie - log in once via any of the three,
  and the others recognize the same session, since cookies aren't scoped by
  port). Skip that prompt and everything stays exactly as open as before.
  Either way, none of this should ever be exposed to the internet or
  port-forwarded - the login exists to keep other people on the same LAN
  out, not to make internet exposure safe. The password can be changed
  later from the Desktop shell itself (gear icon → Security → Change
  password) without needing SSH access - changing it signs out every other
  active session, everywhere, immediately.
- **The SMB share has no login of its own**, regardless of whether the web
  apps' login is enabled - enabling one does not enable the other.
- **No sleep/suspend mode.** Investigated and found infeasible on the
  original hardware (no wake-on-LAN, broken CPU idle states, drive has no
  APM support) and deliberately not pursued further.
- **NTFS USB sticks are not supported** — the kernel this was built against
  has neither `ntfs3` nor `ntfs-3g`. FAT/exFAT/ext2/3/4 work.
- **systemd only.** The installer targets systemd-based Linux NAS devices.
  OpenRC, SysVinit, and vendor-custom init systems are out of scope for v1;
  the installer detects and refuses on non-systemd systems rather than
  half-installing.
- **Tested on exactly one device** (a Buffalo LinkStation LS210D running
  Debian 12 bookworm, armv7l, single ~800MHz core). Portability to other
  systemd-based NAS hardware is design-review confidence, not empirical
  multi-device testing — there is no second device available to validate
  against.
