#!/bin/bash
# Prepare and check the Pi data directory for the checkout this script lives in.
#
# Run as the login user (not root) after the desktop has synced the checkout:
#   POKEPI_ROOT=~/pokemon-pi bash ~/pokemon-pi/repo/examples/raspberry_pi/setup-pi.sh
# It creates private/ and, when missing, the Python runtime; it only checks the
# Nexmon firmware, patched driver and keys, which still have to be built or copied
# by hand (README.md). It never touches the radio, so it is safe over Wi-Fi too.
set -euo pipefail
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd -- "$script_dir/../.." && pwd)"
root="${POKEPI_ROOT:-$HOME/pokemon-pi}"
export PATH="$PATH:/usr/sbin:/sbin"
problems=0
ok() { echo "  ok    $*"; }
bad() { echo "  MISSING $*"; problems=$((problems + 1)); }

if [[ $EUID == 0 ]]; then
    echo 'Run setup-pi.sh as your login user, not root.' >&2
    exit 2
fi
echo "checkout: $repo"
echo "data:     $root"
mkdir -p "$root/private/received"
chmod 700 "$root/private"

echo 'system tools'
for tool in nmcli iw ip rfkill ethtool flock systemd-run timeout setsid tar sudo; do
    if command -v "$tool" >/dev/null; then ok "$tool"; else bad "$tool (install it with apt)"; fi
done

echo 'python runtime'
if [[ ! -x "$root/runtime/bin/python" ]]; then
    python="$(command -v python3.13 || true)"
    if [[ -z "$python" ]]; then
        bad 'python3.13 to build runtime/ (install it, e.g. with uv python install 3.13)'
    else
        echo "  creating runtime/ with $python"
        "$python" -m venv "$root/runtime"
        "$root/runtime/bin/python" -m pip install --quiet trio==0.33.0 python-netlink==0.0.15 \
            pycryptodome==3.23.0 zstandard==0.25.0 "$repo/vendor/LDN"
    fi
fi
if [[ -x "$root/runtime/bin/python" ]]; then
    if "$root/runtime/bin/python" -c 'import trio, netlink, Crypto, zstandard, ldn' 2>/dev/null; then
        ok "runtime/ ($("$root/runtime/bin/python" -V))"
    else
        bad "runtime/ imports (trio, netlink, Crypto, zstandard, ldn); delete runtime/ and rerun"
    fi
fi

echo 'radio'
firmware="$root/nexmon/patches/bcm43455c0/7_45_206/nexmon/brcmfmac43455-sdio.bin"
if [[ -f "$firmware" ]]; then ok 'Nexmon firmware'; else bad "Nexmon firmware at $firmware"; fi
if [[ -x "$root/nexmon/utilities/nexutil/nexutil" ]]; then ok nexutil; else bad 'nexmon/utilities/nexutil/nexutil'; fi
if [[ -f "$root/driver/brcmfmac.ko" ]]; then
    built="$(grep -a -o -m1 'vermagic=[^ ]*' "$root/driver/brcmfmac.ko" | head -n1 | cut -d= -f2)"
    if [[ "$built" == "$(uname -r)" ]]; then
        ok "patched driver (kernel $built)"
    else
        bad "patched driver for this kernel: built for '$built', running $(uname -r); rebuild driver/"
    fi
else
    bad 'driver/brcmfmac.ko'
fi
# A new kernel from apt upgrade leaves the patched driver unloadable until it is rebuilt.
kernel_packages='linux-image-rpi-v8 linux-image-rpi-2712 linux-headers-rpi-v8 linux-headers-rpi-2712'
held="$(apt-mark showhold)"
unheld=''
for package in $kernel_packages; do
    if dpkg-query -W "$package" >/dev/null 2>&1 && ! grep -qx "$package" <<<"$held"; then
        unheld+=" $package"
    fi
done
if [[ -z "$unheld" ]]; then
    ok 'kernel packages held'
else
    bad "kernel hold (run: sudo apt-mark hold$unheld)"
fi

echo 'private'
if [[ -r "$root/private/prod.keys" ]]; then ok prod.keys; else bad "$root/private/prod.keys (copy your own)"; fi

echo 'offline checks'
for check in test_raw_pia.py test_raw_sv.py; do
    if [[ -x "$root/runtime/bin/python" ]] && PYTHONDONTWRITEBYTECODE=1 POKEPI_ROOT="$root" \
            "$root/runtime/bin/python" "$script_dir/$check" >/dev/null 2>&1; then
        ok "$check"
    else
        bad "$check passing (run it by hand to see why)"
    fi
done

if (( problems )); then
    echo "$problems problem(s); see examples/raspberry_pi/README.md"
    exit 1
fi
echo 'Pi ready.'
