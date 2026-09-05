#!/bin/sh
# NAS control-plane installer. Currently a skeleton: it only lays down the
# shared config file. Later phases add udev rule / systemd unit / sudoers
# installation here, one capability at a time.
set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CONF=/etc/nas-control-plane.conf
DEFAULT_CONF="$SCRIPT_DIR/config/nas-control-plane.conf.default"

if [ "$(id -u)" -ne 0 ]; then
    echo "Run this as root (it writes to /etc)." >&2
    exit 1
fi

if [ -f "$CONF" ]; then
    echo "$CONF already exists - leaving it alone."
else
    cp "$DEFAULT_CONF" "$CONF"
    echo "Wrote default config to $CONF"
fi
