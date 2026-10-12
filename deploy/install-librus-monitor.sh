#!/bin/sh
# Root-only owned-path installer. Does not read credentials or restart services.
set -eu
[ "$(id -u)" = 0 ] || { echo 'Run this installer as root.' >&2; exit 1; }
base=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
[ "$#" -ge 1 ] && [ "$#" -le 2 ] || { echo 'Usage: install-librus-monitor.sh APP_USER [--migrate-legacy]' >&2; exit 1; }
app_user="$1"
migrate=false
if [ "$#" -eq 2 ]; then
  [ "$2" = --migrate-legacy ] || { echo "Unknown migration option." >&2; exit 1; }
  migrate=true
fi
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
registry=/etc/netdata/python.d/librus.managed.sha256
if [ -L "$registry" ] || { [ -e "$registry" ] && [ ! -f "$registry" ]; }; then
  echo 'Unsafe Librus Netdata ownership registry.' >&2
  exit 1
fi
if [ -e "$registry" ]; then
  [ "$(stat -c %u "$registry")" = 0 ] || { echo 'Unsafe Librus Netdata registry owner.' >&2; exit 1; }
  [ "$(stat -c %a "$registry")" = 644 ] || { echo 'Unsafe Librus Netdata registry mode.' >&2; exit 1; }
  awk 'NF != 2 || length($1) != 64 || $1 ~ /[^0-9a-f]/ || $2 !~ /^(librus.chart.py|librus.conf|librus_local-health.conf)$/ { bad=1 } END { exit bad || NR != 3 }' "$registry" || { echo 'Invalid Librus Netdata ownership registry.' >&2; exit 1; }
fi
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
    expected=$(awk -v name="$filename" '$2 == name { print $1 }' "$base/netdata/legacy.sha256")
    actual=$(sha256sum "$target" | cut -d ' ' -f 1)
    managed=''
    if [ -e "$registry" ]; then
      managed=$(awk -v name="$filename" '$2 == name { print $1 }' "$registry")
    fi
    if [ "$actual" != "$managed" ] && { [ "$migrate" != true ] || [ -z "$expected" ] || [ "$actual" != "$expected" ]; }; then
      echo 'Refusing to overwrite different existing Librus Netdata file.' >&2
      exit 1
    fi
  fi
done
install -d -o "$app_user" -g netdata -m 2750 /var/lib/librus-monitor
[ -d /etc/netdata/python.d ] || install -d -o root -g root -m 0755 /etc/netdata/python.d
[ -d /etc/netdata/health.d ] || install -d -o root -g root -m 0755 /etc/netdata/health.d
install -o root -g root -m 0644 "$base/netdata/librus.chart.py" /usr/libexec/netdata/python.d/librus.chart.py
install -o root -g root -m 0644 "$base/netdata/librus.conf" /etc/netdata/python.d/librus.conf
install -o root -g root -m 0644 "$base/netdata/librus_local-health.conf" /etc/netdata/health.d/librus_local.conf
record=$(mktemp)
trap 'rm -f "$record"' EXIT HUP INT TERM
for filename in librus.chart.py librus.conf librus_local-health.conf; do
  checksum=$(sha256sum "$base/netdata/$filename" | cut -d ' ' -f 1)
  printf '%s  %s\n' "$checksum" "$filename" >> "$record"
done
install -o root -g root -m 0644 "$record" "$registry"
# Python modules enabled by default in the installed plugin are discovered automatically.
# Leave python.d.conf, all existing collectors, notification configuration and services intact.
echo 'Librus Netdata files installed; install/enable the user monitor timer, then reload Netdata.'
