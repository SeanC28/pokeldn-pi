"""Bounded direct Wi-Fi authentication probe against one discovered Z-A host.

Sends at most three open-system authentication requests. With --associate,
also sends up to three association requests. --ldn-auth additionally runs the
bounded encrypted LDN/Pia session probe. No trade commands are sent.
"""
from pi_paths import DATA_ROOT
import argparse
import os
import pathlib
import struct
import secrets
import hashlib
import json
import trio
import ldn
from ldn import wlan
import pi_backend
import raw_profile

def session_summary(target):
    def tag(value):
        return hashlib.sha256(bytes(value)).hexdigest()[:16]
    return dict(host=tag(target.address), session=tag(target.ssid + target.server_random),
                ssid_tag=tag(target.ssid), server_random_tag=tag(target.server_random),
                channel=target.channel, participants=target.num_participants,
                capacity=target.max_participants, accept_policy=target.accept_policy)

async def observe(args):
    pi_backend.use()
    phy = pathlib.Path('/sys/class/net/wlan0/phy80211').resolve().name
    keys = ldn.load_keys(args.keys)
    for sweep in range(3):
        networks = await ldn.scan(keys, phyname=phy, channels=[1,6,11], dwell_time=1, protocols=[1])
        matches = [session_summary(n) for n in networks if raw_profile.matches(n)]
        print('SESSION_AFTER ' + json.dumps(dict(sweep=sweep, networks=matches)), flush=True)

async def main(args):
    try:
        await run(args)
    finally:
        if args.observe_after:
            with trio.move_on_after(15, shield=True):
                await observe(args)

def association_request(target, source, station_rates=False):
    rsn = wlan.RSNElement(group_cipher_suite=wlan.WLAN_CIPHER_SUITE_CCMP,
        pairwise_cipher_suites=[wlan.WLAN_CIPHER_SUITE_CCMP],
        akm_suites=[wlan.WLAN_AKM_SUITE_PSK], capabilities=12)
    return wlan.AssociationRequest(target=target.address, source=source,
        capability_information=0x0431, listen_interval=10,
        elements={0: target.ssid.hex().encode(),
                  1: bytes.fromhex('02040b160c121824' if station_rates else '82848b960c121824'),
                  50: bytes.fromhex('3048606c'), 48: rsn.encode()})

async def associate(monitor, target, source, radiotap, keys=None, station_rates=False):
    request = association_request(target, source, station_rates)
    sequence_base = secrets.randbelow(4090)
    print(f'Association profile: {"station-rates" if station_rates else "baseline"}', flush=True)
    try:
        for attempt in range(3):
            # Bound queue draining so an already-buffered response cannot be
            # mistaken for a reply to this request. This is not proof of ACKs.
            with trio.move_on_after(.025):
                while True:
                    await monitor.recv()
            packet = bytearray(request.encode())
            struct.pack_into('<H', packet, 22, (sequence_base+attempt)<<4)
            await monitor._socket.send(radiotap + packet)
            print(f'Association request {attempt+1} submitted', flush=True)
            with trio.move_on_after(2) as association_wait:
                while True:
                    frame = (await monitor.recv()).data
                    if len(frame)<30 or frame[0]&0xfc != 0x10:
                        continue
                    # Match all three addresses before parsing any optional IEs.
                    if (frame[4:10] != bytes(source) or frame[10:16] != bytes(target.address)
                            or frame[16:22] != bytes(target.address)):
                        continue
                    _, status, aid = struct.unpack_from('<HHH', frame, 24)
                    seq = struct.unpack_from('<H', frame, 22)[0] >> 4
                    retry = bool(frame[1] & 8)
                    print(f'Target association reply: status={status}, aid={aid & 0x3fff}, sequence={seq}, retry={retry}', flush=True)
                    if status != 0:
                        raise RuntimeError(f'Association rejected with status {status}')
                    association_wait.deadline = float('inf')
                    if keys is not None:
                        from raw_ldn import authenticate
                        await authenticate(monitor, target, source, radiotap, keys)
                    return
        raise NoHost('No matching association response')
    finally:
        # Release only our own probe identity at the selected host.
        frame = wlan.DeauthenticationFrame(target=target.address, source=source,
                                           bssid=target.address, reason=3)
        with trio.move_on_after(1, shield=True):
            await monitor._socket.send(radiotap + frame.encode())
        print('Probe identity disconnect submitted; delivery unconfirmed', flush=True)

class NoHost(RuntimeError):
    """No usable matching host right now; the console may still be browsing.

    Also raised when the host stops answering authentication or association: its
    3 to 4 s member wait can end between those frames, taking the network with it."""

async def run(args):
    pi_backend.use()
    phy = pathlib.Path('/sys/class/net/wlan0/phy80211').resolve().name
    keys = ldn.load_keys(args.keys)
    # A Scarlet/Violet console that hands us the host role destroys its network and hosts
    # again after a few browses, so keep rescanning until this window closes. The trade
    # itself still has to fit inside run_trade.sh's outer timeout.
    retrying = raw_profile.game() == 'sv'
    window_end = trio.current_time() + float(os.environ.get('POKEPI_RETRY_SECONDS', '150'))
    skip = set()
    attempt = 1
    while True:
        scan_until = window_end if attempt > 1 else trio.current_time() + 20
        failure = None
        try:
            await join_once(args, phy, keys, skip, scan_until)
            return
        except* (raw_profile.HandedOff, NoHost) as group:
            failure = group.exceptions[0]
        if not retrying or trio.current_time() >= window_end:
            raise failure
        attempt += 1
        print(f'RETRY {attempt}: {failure}; scanning for the console\'s next session', flush=True)

async def join_once(args, phy, keys, skip, scan_until):
    while True:
        networks = await ldn.scan(keys, phyname=phy, channels=[1,6,11],
                                  dwell_time=1, protocols=[1])
        matches = [n for n in networks if raw_profile.matches(n)
                   and session_summary(n)['session'] not in skip]
        if matches or trio.current_time() >= scan_until:
            break
    if not matches:
        raise NoHost('No matching host found')
    if len(matches) != 1:
        raise RuntimeError(f'Expected one matching host, found {len(matches)}')
    target = matches[0]
    print('SESSION_BEFORE ' + json.dumps(session_summary(target)), flush=True)
    _, source = pi_backend.interface_info('wlan0', phy)
    print(f'Matching game discovered on channel {target.channel}; testing direct authentication', flush=True)
    async with wlan.create_factory() as factory:
        async with factory.create_monitor(phy, 'mon0', target.channel) as monitor:
            # A multi-channel scan can return an advertisement several seconds
            # old. Refresh the selected host on its selected channel immediately
            # before authentication, preserving channel selection from the scan.
            scanner = ldn.Scanner({1: ldn.KeyDerivation(keys, 1)}, monitor)
            previous = session_summary(target)
            with trio.move_on_after(3) as refresh:
                while True:
                    current = await scanner.receive()
                    if (current.address == target.address and
                        current.local_communication_id == target.local_communication_id and
                        current.channel == target.channel):
                        target = current
                        break
            if refresh.cancelled_caught:
                raise NoHost('The selected host stopped advertising')
            print('SESSION_REFRESH ' + json.dumps(dict(previous=previous,
                  current=session_summary(target))), flush=True)
            session = session_summary(target)['session']
            if session in skip:
                raise NoHost('The host still advertises the session it handed over')
            # Never rejoin a session whose host has handed it over.
            skip.add(session)
            # Radiotap: RATE present, 1 Mbps (two 500-kbps units).
            radiotap = struct.pack('<BBHIB', 0, 0, 9, 4, 2)
            request = wlan.AuthenticationFrame(target=target.address, source=source,
                bssid=target.address, algorithm=0, sequence=1, status_code=0)
            sequence_base = secrets.randbelow(4090)
            for attempt in range(3):
                packet = bytearray(request.encode())
                struct.pack_into('<H', packet, 22, (sequence_base+attempt)<<4)
                sent = await monitor._socket.send(radiotap + packet)
                print(f'Request {attempt+1}: socket accepted {sent} bytes (air transmission unconfirmed)', flush=True)
                with trio.move_on_after(2) as authentication_wait:
                    while True:
                        frame = (await monitor.recv()).data
                        if len(frame)<30 or frame[0]&0xfc != 0xb0:
                            continue
                        response = wlan.AuthenticationFrame()
                        try:
                            response.decode(frame)
                        except (ValueError, IndexError):
                            continue
                        if (response.source == target.address and response.target == source
                                and response.bssid == target.address
                                and response.algorithm == 0 and response.sequence == 2):
                            print(f'Target authentication reply: status={response.status_code}', flush=True)
                            if response.status_code != 0:
                                raise RuntimeError(f'Authentication rejected: {response.status_code}')
                            authentication_wait.deadline = float('inf')
                            if args.associate or args.ldn_auth:
                                await associate(monitor, target, source, radiotap,
                                                keys if args.ldn_auth else None, args.station_rates)
                            return
            raise NoHost('No matching authentication response; transmission or response reception remains unverified')

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--keys', required=True)
    parser.add_argument('--associate', action='store_true')
    parser.add_argument('--ldn-auth', action='store_true')
    parser.add_argument('--station-rates', action='store_true',
                        help='Test rates without AP-style basic-rate flags; all other association fields unchanged')
    parser.add_argument('--observe-after', action='store_true')
    trio.run(main, parser.parse_args())
