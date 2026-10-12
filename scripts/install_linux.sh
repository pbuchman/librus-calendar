#!/usr/bin/env bash
# Install packaged release units only. Never starts synchronization or makes a backup.
set -euo pipefail
umask 077
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
exec "${LIBRUS_PYTHON:-python3}" "$root/scripts/deploy.py" install-units "$@"
