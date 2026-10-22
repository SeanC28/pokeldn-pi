#!/bin/bash
set -euo pipefail
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
root="${POKEPI_ROOT:?Set POKEPI_ROOT to the prepared Pi data directory}"
cd "$root"
test -r "${POKEPI_KEYS:?Set POKEPI_KEYS to your private keys file}"
test -x "$root/runtime/bin/python"
mkdir -p "$root/private"
chmod 700 "$root/private"
offer="${1:?Pass a validated offer file}"
test -f "$offer"
if [[ "${POKEPI_GAME:-za}" == za && $(stat -c %s "$offer") != 354 ]]; then
    echo 'Expected a 354-byte framed Z-A offer.' >&2; exit 2
fi
# Runs as root from a checkout the desktop re-syncs as the login user; root-owned
# __pycache__ directories there would block the next sync.
export PYTHONDONTWRITEBYTECODE=1
exec 9>/run/pokepi-session.lock
flock -n 9 || { echo 'Another Pi radio session is running.' >&2; exit 1; }
restore() { [[ ! -f /run/pokepi-radio/restore.sh ]] || bash /run/pokepi-radio/restore.sh; }
trap restore EXIT
trap 'exit 130' INT TERM
POKEPI_ROLLBACK_SECONDS=360 bash "$script_dir/test-radio.sh" >latest-radio-test.log 2>&1
POKEPI_TRADE_OFFER="$offer" timeout --signal=TERM --kill-after=5 300 \
    "$root/runtime/bin/python" "$script_dir/raw_auth_probe.py" --ldn-auth --keys "${POKEPI_KEYS:?Set POKEPI_KEYS to your private keys file}"
