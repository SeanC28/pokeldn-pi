#!/bin/bash
set -euo pipefail
root="${POKEPI_ROOT:-$HOME/pokemon-pi}"
testdir=/run/pokepi-radio
mkdir -p "$testdir/brcm"
cp "$root/nexmon/patches/bcm43455c0/7_45_206/nexmon/brcmfmac43455-sdio.bin" "$testdir/brcm/"
cp "$testdir/brcm/brcmfmac43455-sdio.bin" "$testdir/brcm/brcmfmac43455-sdio.raspberrypi,4-model-b.bin"
cat > "$testdir/restore.sh" <<'RESTORE'
#!/bin/bash
echo '' > /sys/module/firmware_class/parameters/path
ip link set mon0 down 2>/dev/null || true
iw dev mon0 del 2>/dev/null || true
ip link set wlan0 down 2>/dev/null || true
modprobe -r brcmfmac_wcc 2>/dev/null || true
modprobe -r brcmfmac || true
modprobe brcmfmac
sleep 3
nmcli device set wlan0 managed yes || true
RESTORE
chmod 700 "$testdir/restore.sh"
systemctl stop pokepi-radio-rollback.timer || true
systemd-run --unit=pokepi-radio-rollback --on-active="${POKEPI_ROLLBACK_SECONDS:-180}s" /bin/bash "$testdir/restore.sh"
trap '/bin/bash /run/pokepi-radio/restore.sh' ERR
nmcli device set wlan0 managed no
nmcli radio wifi on
rfkill unblock wifi
ip link set wlan0 down
modprobe -r brcmfmac_wcc
modprobe -r brcmfmac
echo "$testdir" > /sys/module/firmware_class/parameters/path
insmod "$root/driver/brcmfmac.ko" pokepi_bssid_only="${POKEPI_BSSID_ONLY:-0}"
sleep 5
nmcli device set wlan0 managed no
rfkill unblock wifi
ip link set wlan0 up
ethtool -i wlan0
phy=$(basename "$(readlink /sys/class/net/wlan0/phy80211)")
iw phy "$phy" info > "$root/radio-capabilities.txt"
iw phy "$phy" interface add mon0 type monitor
echo 'Monitor interface created'
ip link set mon0 up
echo 'Monitor interface enabled'
"$root/nexmon/utilities/nexutil/nexutil" -I wlan0 -k6
"$root/nexmon/utilities/nexutil/nexutil" -I wlan0 -k
iw dev
dmesg | tail -25
