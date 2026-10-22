"""Bounded Z-A session handshake; deliberately contains no trade commands.

Uses the protocol builders and title-specific join conventions from pokeldn's
bin/za_join.py. RawStation owns the encrypted wireless transport.
"""
from pi_paths import DATA_ROOT
import secrets
import os
import trio
from types import SimpleNamespace
from pokeldn.ldn import crypto, host_pia, pia_connect, userspace_ip, reliable
from pokeldn.za import session as za
from pokeldn.za import streams


class Joiner(pia_connect.ConnectionManager):
    def _join(self):
        return pia_connect.build_session_join(
            pia_connect.ldn_constant_id(self.our_mac)[:6],
            self.our_var.to_bytes(2, 'big'), self.our_ip, self.host_mac,
            (self.host_var or 0).to_bytes(2, 'big'), ' ', self.random4,
            player_id=self.player_id, app_ver=b'\x00\x06', token=b'\x06',
            protocols=[(1,0),(3,5),(5,1),(6,0),(9,1),(10,3),(11,4),(12,4),(13,7),(15,0)])


class SessionProbe:
    def __init__(self, interface, local_ip, host_ip):
        self.interface = interface
        self.local_ip, self.host_ip = str(local_ip), str(host_ip)
        self.network = SimpleNamespace(our_ip=self.local_ip)
        self.crypto = crypto.PiaCrypto(interface.target.ssid, za.GAME_KEY)
        self.nonces = host_pia.PiaNonceSequence(native=True)
        self.conn = Joiner(bytes(interface.source), bytes(interface.target.address),
            self.local_ip, self.host_ip, our_var=secrets.randbelow(0xffed) + 2,
            random4=secrets.token_bytes(4), log=print)
        self.sent = self.received = 0
        self.links = {p: reliable.ReliableLink(start=streams.IDLE_NEXT) for p in (10, 11)}
        self.acks = 0
        self.left = False
        self.tx_lock = trio.Lock()
        self.game = None
        if offer := os.environ.get('POKEPI_TRADE_OFFER'):
            from raw_game import GameBridge
            self.game = GameBridge(self, offer)

    async def send(self, proto, payload, *, dst, src, establishing=False,
                   footer=None, compress=False, pktid=None, msgflags=None):
        await self.send_items([(proto, payload, msgflags)], dst=dst, src=src,
            establishing=establishing, footer=footer, compress=compress, pktid=pktid)

    async def send_items(self, items, **kwargs):
        async with self.tx_lock:
            await self._send_items(items, **kwargs)

    async def _send_items(self, items, *, dst, src, establishing=False,
                          footer=None, compress=False, pktid=None):
        if self.sent >= (15000 if self.game else 300):
            raise RuntimeError('Session diagnostic transmission limit reached')
        data = host_pia.build_messages(self.network, self.crypto, items,
            dst_var=dst, src_var=src, establishing=establishing,
            footer_var=footer, compress=compress,
            nonce_source=self.nonces,
            pktid=self.sent + 1 if pktid is None else pktid)
        for packet in userspace_ip.build_udp(self.local_ip, self.host_ip, 12345, 12345, data):
            await self.interface.send_payload(self.interface.target.address, 0x0800, packet)
        self.sent += 1

    async def receive(self, data, source_ip):
        if source_ip != self.host_ip:
            return
        decoded, why = host_pia.decode_datagram(data, source_ip, self.crypto)
        if decoded is None:
            print(f'Pia decode: {why}', flush=True)
            return
        self.received += 1
        header, messages = decoded
        print(f'Pia verified: messages={[(m.proto, len(m.payload)) for m in messages]}', flush=True)
        if header.footer == 2 and header.dst not in (0, pia_connect.SESSION_VAR, 0xffff):
            self.conn.learn_ids(header.dst, header.src)
        for message in messages:
            if (message.proto == pia_connect.PROTO_SESSION and
                    message.payload[:1] == bytes([za.SESSION_LEAVE_RESPONSE]) and
                    message.payload[5:13] == pia_connect.ldn_constant_id(self.interface.source)):
                self.left = True
                print('Switch confirmed Pia session departure', flush=True)
            if message.proto == pia_connect.PROTO_SESSION:
                print(f'Pia Session control prefix: {message.payload[:8].hex()}', flush=True)
            if message.proto == pia_connect.PROTO_SESSION and message.payload[:1] == b'\x05':
                sequence = za.session_update_sequence(message.payload)
                await self.send(pia_connect.PROTO_SESSION,
                    za.build_session_update_ack(pia_connect.ldn_constant_id(self.interface.source), sequence),
                    dst=self.conn.host_var or 0, src=self.conn.our_var,
                    footer=self.conn.host_var or 0)
            self.conn.on_message(message.proto, message.payload)
            if self.game and message.proto in self.links:
                self.game.receive(message.proto, message.payload)
            elif message.proto in self.links and self.conn.host_var is not None:
                frame = reliable.parse_reliable(message.payload)
                if frame is not None and frame.flagsA & 1:
                    link = self.links[message.proto]
                    link.note_received(frame.seq)
                    if message.proto == 11:
                        ack = streams.build_broadcast_ack(link.recv_next)
                        body = streams.frame(streams.IDLE_NEXT, link.send_low(), ack,
                                             reliable.FLAGSA_CTRL, 3)
                    else:
                        body = reliable.build_reliable(streams.IDLE_NEXT, link.send_low(),
                            link.ack_payload(), flagsA=reliable.FLAGSA_CTRL)
                    await self.send(message.proto, body,
                        dst=1 if message.proto == 11 else self.conn.host_var,
                        src=self.conn.our_var, footer=self.conn.host_var,
                        compress=True, msgflags=0x40)
                    self.acks += 1
        for out in self.conn.drain():
            if out['proto'] == pia_connect.PROTO_SESSION and out['payload'][:1] == b'\x06':
                continue
            footer = out['footer_var']
            if footer is None and out['footer']:
                footer = self.conn.host_var
            await self.send(out['proto'], out['payload'], dst=out['dst'], src=out['src'],
                establishing=out['establishing'], footer=footer,
                compress=out['compress'], pktid=out['pktid'])

    async def leave(self):
        if self.conn.host_var is not None:
            await self.send(pia_connect.PROTO_SESSION,
                za.build_leave_request(pia_connect.ldn_constant_id(self.interface.source),
                    self.conn.our_var, self.local_ip, secrets.token_bytes(4)),
                dst=self.conn.host_var, src=self.conn.our_var, footer=self.conn.host_var)
        print(f'Pia session result: state={self.conn.state}, verified={self.received}, sent={self.sent}, reliable_acks={self.acks}', flush=True)
