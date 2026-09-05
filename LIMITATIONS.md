# Known limitations

Running list, appended to as generalization work proceeds. Promoted into the
final README in Phase 5.

- **No authentication.** The Files app, Fetcher, Desktop shell, and the SMB
  share are all open on the LAN with no login. This is a deliberate tradeoff
  for a trusted home network, not an oversight — but it means this must never
  be exposed to the internet or port-forwarded. Adding real auth is out of
  scope for this generalization effort.
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
