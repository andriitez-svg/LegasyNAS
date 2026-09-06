#!/bin/sh
# NAS control-plane installer. Installs Fetcher, the Files app, and the
# Desktop shell as systemd services, plus the USB automount udev rule and
# the power-action sudoers rule. Safe to re-run: every step either leaves
# existing state alone (the config file) or regenerates the same output from
# the same inputs (everything else), so re-running after a config edit or a
# code update reconciles the box instead of duplicating anything.
#
# Non-interactive by default when there's no terminal attached (e.g. piped,
# or run over `ssh host ./install.sh` without -t): every prompt falls back
# to its default silently, so scripted/repeat runs never hang on stdin. Every
# prompted value can also be pre-answered via an environment variable (see
# each prompt below, e.g. NAS_CP_SERVICE_USER) for unattended/scripted runs
# with a real terminal attached.
set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CONF=/etc/nas-control-plane.conf
DEFAULT_CONF="$SCRIPT_DIR/config/nas-control-plane.conf.default"
SUDOERS_FILE=/etc/sudoers.d/nas-control-plane-power

if [ "$(id -u)" -ne 0 ]; then
    echo "Run this as root." >&2
    exit 1
fi

# ---------- scope check: this installer only targets systemd-based Linux ----------
if [ ! -d /run/systemd/system ] || ! command -v systemctl >/dev/null 2>&1; then
    cat >&2 <<'EOF'
This installer only supports systemd-based Linux (no systemd was detected
here). OpenRC, SysVinit, and other init systems aren't handled by this
version - porting the three .service units and the udev RUN mechanism to
your init system is up to you.
EOF
    exit 1
fi

MISSING=""
for cmd in python3 udevadm blkid mountpoint findmnt visudo lsblk; do
    command -v "$cmd" >/dev/null 2>&1 || MISSING="$MISSING $cmd"
done
if [ -n "$MISSING" ]; then
    echo "Missing required commands:$MISSING" >&2
    exit 1
fi

# ---------- prompt helper: falls back to the default with no tty attached ----------
prompt() {
    q="$1"; def="$2"
    if [ -t 0 ]; then
        printf '%s [%s]: ' "$q" "$def" >&2
        read -r ans
        [ -n "$ans" ] && echo "$ans" || echo "$def"
    else
        echo "$def"
    fi
}

# ---------- service account ----------
DEFAULT_USER=debian
if [ -f "$CONF" ]; then
    EXISTING_UID=$(sed -n 's/^OWNER_UID=//p' "$CONF" | head -1)
    [ -n "${EXISTING_UID:-}" ] && DEFAULT_USER=$(getent passwd "$EXISTING_UID" 2>/dev/null | cut -d: -f1 || true)
    [ -n "$DEFAULT_USER" ] || DEFAULT_USER=debian
fi
SERVICE_USER=${NAS_CP_SERVICE_USER:-$(prompt "Run the services as which existing user?" "$DEFAULT_USER")}
if ! id "$SERVICE_USER" >/dev/null 2>&1; then
    echo "User '$SERVICE_USER' doesn't exist - create it first (e.g. adduser $SERVICE_USER), then re-run this." >&2
    exit 1
fi
SERVICE_UID=$(id -u "$SERVICE_USER")
SERVICE_GID=$(id -g "$SERVICE_USER")

# ---------- install location for the app files ----------
INSTALL_DIR=${NAS_CP_INSTALL_DIR:-$(prompt "Install the app files where?" "/opt/nas-control-plane")}
mkdir -p "$INSTALL_DIR"

# ---------- shared config ----------
if [ -f "$CONF" ]; then
    echo "$CONF already exists - leaving its values alone."
else
    ROOT_DIR_ANS=${NAS_CP_ROOT_DIR:-$(prompt "Root directory for shared data" "/var/downloads")}
    PORT_FILES_ANS=${NAS_CP_PORT_FILES:-$(prompt "Port for the Files app" "8093")}
    PORT_FETCHER_ANS=${NAS_CP_PORT_FETCHER:-$(prompt "Port for Fetcher" "8092")}
    PORT_DESKTOP_ANS=${NAS_CP_PORT_DESKTOP:-$(prompt "Port for the Desktop shell" "8095")}
    DATA_MOUNT_ANS=${NAS_CP_DATA_MOUNT:-$(prompt "Mountpoint the Desktop shell should report storage for" "/mnt/data")}
    INTERNAL_PREFIX_ANS=${NAS_CP_INTERNAL_DISK_PREFIX:-$(prompt "Device-name prefix of your internal disk (never touched by USB automount)" "sda")}

    sed \
        -e "s#^ROOT_DIR=.*#ROOT_DIR=$ROOT_DIR_ANS#" \
        -e "s/^PORT_FILES=.*/PORT_FILES=$PORT_FILES_ANS/" \
        -e "s/^PORT_FETCHER=.*/PORT_FETCHER=$PORT_FETCHER_ANS/" \
        -e "s/^PORT_DESKTOP=.*/PORT_DESKTOP=$PORT_DESKTOP_ANS/" \
        -e "s/^OWNER_UID=.*/OWNER_UID=$SERVICE_UID/" \
        -e "s/^OWNER_GID=.*/OWNER_GID=$SERVICE_GID/" \
        -e "s#^DATA_MOUNT=.*#DATA_MOUNT=$DATA_MOUNT_ANS#" \
        -e "s/^INTERNAL_DISK_PREFIX=.*/INTERNAL_DISK_PREFIX=$INTERNAL_PREFIX_ANS/" \
        "$DEFAULT_CONF" > "$CONF"
    echo "Wrote $CONF"
fi

# shellcheck source=/dev/null
. "$CONF"
OWNER_UID="${OWNER_UID:-$SERVICE_UID}"
ROOT_DIR="${ROOT_DIR:-/var/downloads}"

mkdir -p "$ROOT_DIR"
chown "$SERVICE_USER" "$ROOT_DIR" 2>/dev/null || true

# ---------- app files + systemd units ----------
install_app_files() {
    for f in filemanager.py fetcher.py serve.py desktop.html; do
        install -m 0644 -o "$SERVICE_USER" -g "$SERVICE_USER" "$SCRIPT_DIR/$f" "$INSTALL_DIR/$f"
    done
    echo "Installed app files to $INSTALL_DIR"
}

install_units() {
    for svc in filemgr fetcher desktop; do
        sed \
            -e "s#@SERVICE_USER@#$SERVICE_USER#g" \
            -e "s#@INSTALL_DIR@#$INSTALL_DIR#g" \
            "$SCRIPT_DIR/templates/$svc.service.tmpl" > "/etc/systemd/system/$svc.service"
    done
    systemctl daemon-reload
    for svc in filemgr fetcher desktop; do
        # enable is idempotent; restart (not "start") so a re-run that
        # updated the app files or the unit actually picks the change up
        # instead of leaving an already-running instance untouched.
        systemctl enable "$svc.service" >/dev/null 2>&1
        systemctl restart "$svc.service"
    done
    echo "Installed and (re)started filemgr, fetcher, and desktop services."
}

install_usb_automount() {
    install -m 0755 "$SCRIPT_DIR/usb-automount" /usr/local/sbin/usb-automount
    install -m 0644 "$SCRIPT_DIR/99-usb-automount.rules" /etc/udev/rules.d/99-usb-automount.rules
    udevadm control --reload-rules
    echo "Installed usb-automount + udev rule, and reloaded udev rules."
}

install_power_sudoers() {
    # Resolve the real binary locations on this box rather than assuming
    # Debian's /usr/sbin/reboot and /usr/sbin/poweroff - some distros keep
    # these elsewhere. Falls back to the Debian path only if command -v
    # can't find one at all, so this never produces an empty rule.
    reboot_bin=$(command -v reboot || echo /usr/sbin/reboot)
    poweroff_bin=$(command -v poweroff || echo /usr/sbin/poweroff)

    tmp=$(mktemp)
    cat > "$tmp" <<EOF
# Lets the desktop shell (running as uid $OWNER_UID) restart or shut down
# this NAS from its System settings. Scoped to exactly these two commands -
# deliberately not a blanket NOPASSWD grant. Generated by install.sh; edit
# $CONF and re-run it instead of hand-editing this file.
#$OWNER_UID ALL=(root) NOPASSWD: $reboot_bin, $poweroff_bin
EOF

    # A malformed sudoers file can break sudo system-wide - never install one
    # that hasn't been validated first.
    if ! visudo -c -f "$tmp" >/dev/null 2>&1; then
        echo "Generated sudoers file failed validation - not installing it." >&2
        visudo -c -f "$tmp" >&2 || true
        rm -f "$tmp"
        exit 1
    fi

    install -m 0440 "$tmp" "$SUDOERS_FILE"
    rm -f "$tmp"
    echo "Installed $SUDOERS_FILE ($reboot_bin, $poweroff_bin)."
}

install_app_files
install_units
install_usb_automount
install_power_sudoers

echo
echo "=== Running smoke test ==="
# This box is slow enough that a just-restarted service isn't always
# listening yet by the time curl gets to it - give it a moment.
sleep 3
"$SCRIPT_DIR/smoke_test.sh" localhost
