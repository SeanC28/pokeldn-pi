"""Experimental, bounded LDN authentication over an associated raw radio."""
from pi_paths import DATA_ROOT
import secrets
import struct
import hashlib
import pathlib
import sqlite3
import socket
import os
import trio
import ldn
import raw_profile
from ldn import wlan

PASSPHRASE = b'BM7cXkadR9ugiXdHiurkiyhrQwcR3rMgCM5BF47dranKXWAGpGEA9z3ncXRnPjCX'

def reserve_packet_numbers(key, source):
    path = DATA_ROOT / 'private/packet-numbers.sqlite'
    identity = hashlib.sha256(key + bytes(source)).hexdigest()
    with sqlite3.connect(path) as db:
        path.chmod(0o600)
        db.execute('CREATE TABLE IF NOT EXISTS counters (identity TEXT PRIMARY KEY, next INTEGER)')
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT next FROM counters WHERE identity=?', (identity,)).fetchone()
        start = row[0] if row else secrets.randbits(40) + 1
        end = start + 4096
        if end >= 1 << 48:
            raise RuntimeError('CCMP packet number exhausted; new session key required')
        db.execute('INSERT OR REPLACE INTO counters VALUES (?,?)', (identity, end))
    return start, end

class RawStation:
    def __init__(self, monitor, target, source, key, radiotap):
        self.monitor, self.target, self.source = monitor, target, source
        self.key, self.radiotap = key, radiotap
        self.pn, self.pn_end = reserve_packet_numbers(key, source)
        self.sequence = 64
        self.replay = {}
        self.on_data = None
        self.counts = dict(received=0, decrypted=0, bad_mic=0, replay=0)
        # The nexmon firmware halts under injection bursts (SV sends ~50 frames in 45 ms),
        # so serialize sends and keep a minimum gap between them.
        self.tx_lock = trio.Lock()
        self.tx_gap = float(os.environ.get('POKEPI_TX_GAP_MS', '5')) / 1000
        self.tx_next = 0.0

    def address(self):
        return self.source

    async def send_custom_frame(self, addr, payload):
        await self.send_payload(addr, 0x88b7, payload)
        print('Encrypted LDN authentication request submitted', flush=True)

    async def send_payload(self, addr, protocol, payload, destination=None):
        if addr != self.target.address:
            raise ValueError('Probe can only transmit to selected host')
        frame = wlan.DataFrame(target=addr, source=self.source,
            bssid=destination or addr, tods=True,
            payload=wlan.SNAPHeader(protocol=protocol, payload=payload).encode())
        async with self.tx_lock:
            await trio.sleep_until(self.tx_next)
            self.pn += 1
            if self.pn >= self.pn_end:
                self.pn, self.pn_end = reserve_packet_numbers(self.key, self.source)
            frame.encrypt(self.key, self.pn, 0)
            packet = bytearray(frame.encode())
            struct.pack_into('<H', packet, 22, (self.sequence & 4095) << 4)
            self.sequence += 1
            await self.monitor._socket.send(self.radiotap + packet)
            self.tx_next = trio.current_time() + self.tx_gap

    async def next_event(self):
        while True:
            raw = (await self.monitor.recv()).data
            if len(raw) < 24 or raw[10:16] != bytes(self.target.address):
                continue
            subtype = raw[0] & 0xfc
            if raw[4:10] == bytes(self.source) and subtype in (0xa0, 0xc0):
                return wlan.DisassociationEvent(self.target.address)
            if (raw[0] & 12) != 8 or raw[4:10] not in (bytes(self.source), b'\xff' * 6):
                continue
            self.counts['received'] += 1
            frame = wlan.DataFrame()
            try:
                frame.decode(raw)
                if not frame.protected or frame.keyid not in (0, 1):
                    continue
                stream = (frame.keyid, frame.tid if frame.qos else 0)
                if frame.nonce <= self.replay.get(stream, -1):
                    self.counts['replay'] += 1
                    continue
                frame.decrypt(self.key)
                self.replay[stream] = frame.nonce
                self.counts['decrypted'] += 1
                snap = wlan.SNAPHeader()
                snap.decode(frame.payload)
            except (ValueError, IndexError):
                self.counts['bad_mic'] += 1
                continue
            if snap.oui == 0 and snap.protocol == 0x88b7:
                return wlan.CustomFrameEvent(self.target.address, snap.payload)
            if snap.oui == 0 and self.on_data is not None:
                await self.on_data(snap.protocol, snap.payload)

async def probe_data(interface, local_ip, host_ip):
    """Verify encrypted ARP round trips without entering the trade protocol."""
    from raw_pia import SessionProbe
    session = SessionProbe(interface, local_ip, host_ip)
    local, host = socket.inet_aton(str(local_ip)), socket.inet_aton(str(host_ip))
    replies = set()
    protocols = {}

    async def receive(protocol, payload):
        protocols[hex(protocol)] = protocols.get(hex(protocol), 0) + 1
        if protocol == 0x0806 and len(payload) >= 28:
            hardware, arp_protocol, hlen, plen, op, sender, sip, dest, dip = struct.unpack('!HHBBH6s4s6s4s', payload[:28])
            if (hardware, arp_protocol, hlen, plen) != (1, 0x0800, 6, 4):
                return
            if op == 2 and sip == host and dip == local and dest == bytes(interface.source):
                replies.add(sender)
                print('Encrypted ARP response verified from host', flush=True)
            elif op == 1 and dip == local:
                reply = struct.pack('!HHBBH6s4s6s4s', 1, 0x0800, 6, 4, 2,
                    bytes(interface.source), local, sender, sip)
                await interface.send_payload(interface.target.address, 0x0806, reply)
        elif protocol == 0x0800 and len(payload) >= 20:
            ihl = (payload[0] & 15) * 4
            if payload[0] >> 4 == 4 and payload[9] == 17 and len(payload) >= ihl + 8:
                sport, dport, length = struct.unpack_from('!HHH', payload, ihl)
                print(f'Encrypted UDP received: ports={sport}->{dport}, length={length}', flush=True)
                if sport == 12345 and dport == 12345 and 8 <= length <= len(payload) - ihl:
                    await session.receive(payload[ihl + 8:ihl + length],
                        socket.inet_ntoa(payload[12:16]))

    interface.on_data = receive
    request = struct.pack('!HHBBH6s4s6s4s', 1, 0x0800, 6, 4, 1,
        bytes(interface.source), local, bytes(6), host)
    try:
        if session.game:
            async def receive_game():
                while True:
                    event = await interface.next_event()
                    if isinstance(event, wlan.DisassociationEvent):
                        raise RuntimeError('Host disconnected during trade')
            async with trio.open_nursery() as nursery:
                nursery.start_soon(receive_game)
                with trio.fail_after(240):
                    while not session.game.game.trades:
                        await session.game.pump()
                        await trio.sleep(.02)
                    print('Trade engine submitted completion; awaiting final delivery', flush=True)
                    with trio.move_on_after(8):
                        while True:
                            await session.game.pump()
                            await trio.sleep(.02)
                nursery.cancel_scope.cancel()
            return
        for attempt in range(3):
            await interface.send_payload(interface.target.address, 0x0806, request)
            print(f'Encrypted ARP request {attempt + 1} submitted', flush=True)
            with trio.move_on_after(4):
                while True:
                    event = await interface.next_event()
                    if isinstance(event, wlan.DisassociationEvent):
                        raise RuntimeError('Host disconnected during data probe')
        hold = min(60, max(12, float(os.environ.get('POKEPI_HOLD_SECONDS', '12'))))
        with trio.move_on_after(hold - 12):
            while True:
                event = await interface.next_event()
                if isinstance(event, wlan.DisassociationEvent):
                    raise RuntimeError('Host disconnected during extended hold')
        print(f'Data probe: verified_arp_peers={len(replies)}, protocols={protocols}', flush=True)
    finally:
        with trio.move_on_after(3, shield=True):
            await session.leave()
            while not session.left:
                event = await interface.next_event()
                if isinstance(event, wlan.DisassociationEvent):
                    break
        interface.on_data = None

async def authenticate(monitor, target, source, radiotap, keys):
    passphrase, app_version = raw_profile.credentials()
    derivation = ldn.KeyDerivation(keys, target.protocol)
    key = derivation.derive_data_key(target.server_random, passphrase)
    interface = RawStation(monitor, target, source, key, radiotap)
    param = ldn.ConnectNetworkParam(network=target, keys=keys,
        password=passphrase, name=os.environ.get('POKEPI_TRAINER', 'PI-TEST').encode(), app_version=app_version,
        client_random=secrets.token_bytes(16))
    network = ldn.STANetwork(interface, param, derivation)
    try:
        with trio.fail_after(8):
            await network._authenticate()
        print('Nintendo LDN authentication response verified successfully', flush=True)
        scanner = ldn.Scanner({target.protocol: derivation}, monitor)
        with trio.fail_after(8) as seat_wait:
            while True:
                current = await scanner.receive()
                if (current.address != target.address or current.ssid != target.ssid
                        or current.server_random != target.server_random):
                    continue
                for index, participant in enumerate(current.participants):
                    if participant.connected and participant.mac_address == source:
                        seat_wait.deadline = float('inf')
                        print(f'LDN seat confirmed: index={index}, ip={participant.ip_address}, host={current.participants[0].ip_address}', flush=True)
                        if raw_profile.game() == 'sv':
                            from raw_sv import trade
                            await trade(interface, participant.ip_address, current.participants[0].ip_address)
                        else:
                            await probe_data(interface, participant.ip_address, current.participants[0].ip_address)
                        return
    finally:
        print(f'Encrypted receive counters: {interface.counts}', flush=True)
