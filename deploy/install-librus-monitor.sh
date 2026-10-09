#!/bin/sh
# Root-only owned-path installer. Does not read credentials or restart services.
set -eu
[ "$(id -u)" = 0 ] || { echo 'Run this installer as root.' >&2; exit 1; }
base=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
[ "$#" -eq 1 ] || { echo 'Usage: install-librus-monitor.sh APP_USER' >&2; exit 1; }
app_user="$1"
case "$app_user" in *[!a-zA-Z0-9_-]*|'') echo 'Invalid application user.' >&2; exit 1;; esac
id "$app_user" >/dev/null
getent group netdata >/dev/null
[ -d /usr/libexec/netdata/python.d ] || { echo 'Netdata python.d is missing.' >&2; exit 1; }
for directory in /var/lib /var/lib/librus-monitor /etc/netdata /etc/netdata/python.d /etc/netdata/health.d /usr/libexec /usr/libexec/netdata /usr/libexec/netdata/python.d; do
  if [ -L "$directory" ] || { [ -e "$directory" ] && [ ! -d "$directory" ]; }; then
    echo 'Unsafe Netdata installation directory.' >&2
    exit 1
  fi
done
# Complete the preflight before changing ownership or copying any plugin file.
for item in 'librus.chart.py:/usr/libexec/netdata/python.d' 'librus.conf:/etc/netdata/python.d' 'librus_local-health.conf:/etc/netdata/health.d'; do
  filename=${item%%:*}
  directory=${item#*:}
  target="$directory/$filename"
  [ "$filename" != librus_local-health.conf ] || target="$directory/librus_local.conf"
  if [ -L "$target" ] || { [ -e "$target" ] && [ ! -f "$target" ]; }; then
    echo 'Unsafe Netdata installation target.' >&2
    exit 1
  fi
  if [ -e "$target" ] && ! cmp -s "$base/netdata/$filename" "$target"; then
    echo 'Refusing to overwrite different existing Librus Netdata file.' >&2
    exit 1
  fi
done
install -d -o "$app_user" -g netdata -m 2750 /var/lib/librus-monitor
[ -d /etc/netdata/python.d ] || install -d -o root -g root -m 0755 /etc/netdata/python.d
[ -d /etc/netdata/health.d ] || install -d -o root -g root -m 0755 /etc/netdata/health.d
install -o root -g root -m 0644 "$base/netdata/librus.chart.py" /usr/libexec/netdata/python.d/librus.chart.py
install -o root -g root -m 0644 "$base/netdata/librus.conf" /etc/netdata/python.d/librus.conf
install -o root -g root -m 0644 "$base/netdata/librus_local-health.conf" /etc/netdata/health.d/librus_local.conf
# Python modules enabled by default in the installed plugin are discovered automatically.
# Leave python.d.conf, all existing collectors, notification configuration and services intact.
echo 'Librus Netdata files installed; install/enable the user monitor timer, then reload Netdata.'
