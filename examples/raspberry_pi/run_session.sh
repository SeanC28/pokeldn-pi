#!/bin/bash
# Run as root on the Pi. Ethernet must remain connected for remote access.
set -euo pipefail
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
root="${POKEPI_ROOT:?Set POKEPI_ROOT to the prepared Pi data directory}"
cd "$root"
test -r "${POKEPI_KEYS:?Set POKEPI_KEYS to your private keys file}"
test -x "$root/runtime/bin/python"
mkdir -p "$root/private"
chmod 700 "$root/private"
hold="${1:-12}"
case "$hold" in
    ''|*[!0-9]*) echo 'Duration must be an integer from 12 through 60 seconds.' >&2; exit 2 ;;
esac
if (( hold < 12 || hold > 60 )); then
    echo 'Duration must be from 12 through 60 seconds.' >&2
    exit 2
fi
exec 9>/run/pokepi-session.lock
flock -n 9 || { echo 'Another Pi radio session is running.' >&2; exit 1; }
restore() {
    if [[ -f /run/pokepi-radio/restore.sh ]]; then
        bash /run/pokepi-radio/restore.sh
    fi
}
trap restore EXIT
trap 'exit 130' INT TERM
bash "$script_dir/test-radio.sh" >latest-radio-test.log 2>&1
POKEPI_HOLD_SECONDS="$hold" timeout --signal=TERM --kill-after=5 120 \
    "$root/runtime/bin/python" "$script_dir/raw_auth_probe.py" --ldn-auth --keys "${POKEPI_KEYS:?Set POKEPI_KEYS to your private keys file}"
