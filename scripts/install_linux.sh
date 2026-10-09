#!/usr/bin/env bash
# Run from deployed ~/librus-calendar. Installs units only, never starts synchronization.
set -euo pipefail
umask 077
app_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
expected_root="$HOME/librus-calendar"
if [[ "$app_root" != "$expected_root" ]]; then
  printf '%s\n' 'Run the installer from ~/librus-calendar on Linux.' >&2
  exit 1
fi
unit_dir="$HOME/.config/systemd/user"
config_dir="$HOME/.config/librus-calendar"
# Inspect every parent before mkdir/chmod/install; dangling links count too.
for directory in "$HOME/.config" "$HOME/.config/systemd" "$unit_dir" "$config_dir" "$app_root/.venv"; do
  if [[ -L "$directory" ]] || { [[ -e "$directory" ]] && [[ ! -d "$directory" ]]; }; then
    printf '%s\n' 'Refusing unsafe installation directory.' >&2
    exit 1
  fi
done
for target in web.env config.json.example monitor.env.example; do
  if [[ -L "$config_dir/$target" ]] || { [[ -e "$config_dir/$target" ]] && [[ ! -f "$config_dir/$target" ]]; }; then
    printf '%s\n' 'Refusing unsafe configuration target.' >&2
    exit 1
  fi
done
for unit in librus-web.service librus-sync.service librus-sync.timer librus-backup.service librus-backup.timer; do
  if [[ -L "$unit_dir/$unit" ]] || { [[ -e "$unit_dir/$unit" ]] && [[ ! -f "$unit_dir/$unit" ]]; }; then
    printf 'Refusing unsafe unit target: %s\n' "$unit" >&2
    exit 1
  fi
  if [[ -e "$unit_dir/$unit" ]] && ! cmp -s "$app_root/deploy/$unit" "$unit_dir/$unit"; then
    printf 'Refusing to overwrite different existing unit: %s\n' "$unit" >&2
    exit 1
  fi
done
python_bin="${LIBRUS_PYTHON:-python3}"
"$python_bin" -c 'import sys; assert sys.version_info >= (3,12), "Python 3.12 or later required"'
if [[ ! -d "$app_root/.venv" ]]; then "$python_bin" -m venv "$app_root/.venv"; fi
"$app_root/.venv/bin/python" -m pip install --disable-pip-version-check -r "$app_root/requirements.txt"
mkdir -p "$unit_dir" "$config_dir"
chmod 700 "$config_dir"
for unit in librus-web.service librus-sync.service librus-sync.timer librus-backup.service librus-backup.timer; do
  install -m 600 "$app_root/deploy/$unit" "$unit_dir/$unit"
done
if [[ ! -e "$config_dir/web.env" ]]; then
  install -m 600 "$app_root/deploy/web.env.example" "$config_dir/web.env"
fi
for example in config.json.example monitor.env.example; do
  if [[ ! -e "$config_dir/$example" ]]; then
    install -m 600 "$app_root/deploy/$example" "$config_dir/$example"
  fi
done
systemctl --user daemon-reload
printf '%s\n' 'Installed. Services and timers have not been enabled.'
printf '%s\n' 'Configure your calendar, credentials and trusted proxy before starting services.'
printf '%s\n' 'After acceptance: systemctl --user enable --now librus-web.service librus-sync.timer librus-backup.timer'
printf '%s\n' 'Tailscale route (requires operator/admin): tailscale serve --bg --https=8445 --yes http://127.0.0.1:8795'
