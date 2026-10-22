"""Offline protocol regression checks; no radio or console required."""
from pi_paths import DATA_ROOT
from types import SimpleNamespace
import trio
from ldn import wlan
from pokeldn.ldn import host_pia, pia_connect, reliable, userspace_ip
from pokeldn.za import streams
from raw_pia import SessionProbe


class FakeRadio:
    source = wlan.MACAddress('02:00:00:00:00:02')
    target = SimpleNamespace(address=wlan.MACAddress('02:00:00:00:00:01'),
                             ssid=bytes(range(16)))

    def __init__(self):
        self.sent = []

    async def send_payload(self, address, protocol, payload):
        assert address == self.target.address and protocol == 0x0800
        self.sent.append(payload)


async def check():
    radio = FakeRadio()
    probe = SessionProbe(radio, '169.254.1.2', '169.254.1.1')
    probe.conn.host_var = 0x1234
    probe.conn.state = pia_connect.ST_CONNECTED
    host = SimpleNamespace(our_ip='169.254.1.1')
    for proto in (10, 11):
        inner = b'example'
        if proto == 11:
            wire = streams.frame(0xfff0, 0xfff0, streams.PREFIX_HOST + inner,
                                 reliable.FLAGSA_INIT, 3)
        else:
            wire = reliable.build_reliable(0xfff0, 0xfff0, inner,
                                           flagsA=reliable.FLAGSA_INIT)
        packet = host_pia.build_message(host, probe.crypto, proto, wire,
            dst_var=probe.conn.our_var, src_var=0x1234, footer_var=probe.conn.our_var)
        await probe.receive(packet, host.our_ip)
    assert probe.acks == 2 and len(radio.sent) == 2
    nonces = []
    for proto, ip in zip((10, 11), radio.sent):
        assert userspace_ip.checksum(ip[:20]) == 0
        decoded, why = host_pia.decode_datagram(ip[28:], probe.local_ip, probe.crypto)
        assert decoded is not None, why
        header, messages = decoded
        nonces.append(int.from_bytes(header.nonce8, 'big'))
        message, = messages
        assert message.proto == proto and message.msgflags == 0x40
        ack = reliable.parse_reliable(message.payload)
        assert ack.flagsA == reliable.FLAGSA_CTRL and ack.seq == 0xfff0
        expected = ack.payload[6:8] if proto == 11 else ack.payload[2:4]
        assert int.from_bytes(expected, 'big') == 0xfff1
    assert nonces[1] == nonces[0] + 1
    assert pia_connect.parse_session_join(probe.conn._join()) is not None
    print('PASS: encrypted ACK round trips, both stream formats, cumulative ACKs, IPv4 checksums, nonce sequence, join encoding')


if __name__ == '__main__':
    trio.run(check)
