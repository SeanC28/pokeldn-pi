# Experimental Raspberry Pi 4 radio alternative

This fork adds a Pi-only experimental transport to Decryptu's pokeldn. It uses
the Pi 4's built-in BCM43455 Wi-Fi, Nexmon firmware, a matching patched brcmfmac
driver, and userspace CCMP/IP/Pia. It does not require an ESP32 or USB Wi-Fi adapter.
The upstream game engine, Pokémon builder, and reference messages do the game work.

## Verified result

On October 21, 2026, a Raspberry Pi 4 Model B / 2 GB completed a Legends Z-A
local trade with a stock Switch 2. The user confirmed Genesect arrived; the Pi
saved the received Litwick. A separate 45-second session and clean departure
also passed. This is one hardware/game combination, not general compatibility.

Tested OS: Raspberry Pi OS Bookworm Lite 64-bit, kernel
`6.12.109+rpt-rpi-v8`; Nexmon BCM43455c0 firmware `7_45_206`; Python 3.13.16,
Trio 0.33.0, python-netlink 0.0.15, pycryptodome 3.23.0, zstandard 0.25.0,
and this checkout's vendored LDN (0.0.17).

## Status and prerequisites

This is an advanced source integration, not an installer or a GUI radio option.
Only joining Legends Z-A was tested. Other games, Pi models, kernels, hosting,
and repeated unattended trades have not been validated. Some received frames were
rejected during testing; the current counter combines parse and integrity failures.

Keep Ethernet connected. Radio setup temporarily unloads the normal Wi-Fi driver
and creates `mon0`; it restores the stock driver on exit. Do not run it through
a Wi-Fi-only SSH connection. The scripts expect NetworkManager, systemd, iw, ip,
rfkill, ethtool, flock, and matching kernel headers/build tools.

Prepare a data directory outside the checkout, referred to as `$POKEPI_ROOT`:

* `nexmon/`: build [seemoo-lab/nexmon](https://github.com/seemoo-lab/nexmon),
  specifically `patches/bcm43455c0/7_45_206/nexmon`, and `utilities/nexutil`.
  The firmware must exist at that patch directory's `brcmfmac43455-sdio.bin`.
* `driver/`: the Kali brcmfmac-nexmon-dkms sources at commit
  `b3b2a8622a361cc8929fa3e153f4a87020361472` (tag `kali/6.12.2`). Apply
  `kernel-compat.patch` with `git apply`, then build against the running kernel:
  `make -C /lib/modules/$(uname -r)/build M="$PWD" -j2`.
  The resulting module must be `driver/brcmfmac.ko`. The patch is specific to the
  tested kernel; do not assume it applies to newer kernels.
* `runtime/`: a Python 3.13 virtual environment with the dependencies above and
  this repository's `vendor/LDN` installed. Run from a full source checkout.
* `private/`: mode 0700, for persistent CCMP counters and received Pokémon files.

Firmware/toolchain preparation still needs a reproducible setup script. The
original successful build needed ARM compiler compatibility libraries. No firmware
binary, compiled module, SDK, or Nintendo key is redistributed in this addition.

Use your own keys file outside the checkout. Never commit it. Set absolute paths
explicitly when running through sudo:

```sh
sudo env POKEPI_ROOT=/path/to/pi-data POKEPI_KEYS=/path/to/private/prod.keys \
  bash examples/raspberry_pi/run_session.sh 45
```

Start Z-A local Link Trade searching before running. Session tests do not trade.
They disconnect at the end, which may end the console's search.

## Trading

Prepare and validate a Z-A Pokémon through the upstream builder. The runner takes
a **354-byte framed Z-A offer**, not a raw `.pa9` file. Upstream
`pokeldn.pokemon.offer_bytes('za', decrypted_party_bytes)` creates this framing.
Validate the record before framing; the runner does not perform a legality check.

```sh
sudo env POKEPI_ROOT=/path/to/pi-data POKEPI_KEYS=/path/to/private/prod.keys \
  bash examples/raspberry_pi/run_trade.sh /path/to/validated.offer.bin
```

The partner appears as PI-TEST. Choose and confirm the exchange on the Switch.
The adapter uses upstream `bin/za_join.py` GameStreams, permits one completed
trade per run, and saves the received record under `$POKEPI_ROOT/private/received`.
The process is bounded to 300 seconds, with a separate 360-second radio rollback.
Completion in the log records the engine sending its final step; verify arrival
on the console. A disconnect during a trade requires checking the console's state
before attempting another exchange.

## Offline check

```sh
POKEPI_ROOT=/path/to/pi-data /path/to/pi-data/runtime/bin/python \
  examples/raspberry_pi/test_raw_pia.py
```

Tests cover encrypted ACK round trips, both reliable stream formats, cumulative
ACK progression, IPv4 checksums, nonce progression, and Z-A join serialization.
They do not replace a live hardware test.

## Credits and license

This is a fork of [Decryptu/pokeldn](https://github.com/Decryptu/pokeldn), retaining
its AGPL-3.0 license and upstream attribution. Pi integration developed with Codex
and tested by SeanC28. Nexmon and the Kali driver remain separate upstream
projects with their own licensing; obtain and build them from their sources.
