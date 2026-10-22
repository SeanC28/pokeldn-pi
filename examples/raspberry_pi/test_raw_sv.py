"""Offline bridge regression: broadcast transmission and incoming UDP, no radio."""
import os
from types import SimpleNamespace
import trio
from ldn import wlan
from pokeldn.ldn import userspace_ip
import raw_profile
import raw_sv


class Radio:
    source = wlan.MACAddress('02:00:00:00:00:02')
    target = SimpleNamespace(address=wlan.MACAddress('02:00:00:00:00:01'), ssid=bytes(16))

    def __init__(self):
        self.sent = trio.Event()

    async def next_event(self):
        await trio.sleep_forever()

    async def send_payload(self, address, protocol, payload, destination=None):
        assert address == self.target.address
        assert bytes(destination) == b'\xff' * 6
        assert protocol == 0x800
        assert payload[20 + 8:] == b'outbound'
        self.sent.set()


async def check():
    try:
        import sv_join as upstream
    except ImportError:
        import sv_join_upstream as upstream
    radio = Radio()

    async def session(args, keys, host_ip, host_mac, local_ip, local_mac, record):
        assert args.session_join and args.net_ack and args.game_channel
        assert args.record_set and args.send_on_open
        with userspace_ip.pia_socket(args.ifname, 12345) as sock:
            sock.sendto(b'outbound', ('169.254.1.255', 12345))
            await radio.sent.wait()
            for packet in userspace_ip.build_udp(host_ip, local_ip, 12345, 12345, b'inbound'):
                await radio.on_data(0x800, packet)
            await trio.lowlevel.wait_readable(sock)
            assert sock.recvfrom(100) == (b'inbound', (host_ip, 12345))
            record(rec='trade_done')

    original = upstream.run_session
    upstream.run_session = session
    os.environ['POKEPI_TRADE_OFFER'] = 'unused'
    os.environ['POKEPI_RECEIVED'] = 'unused'
    try:
        with trio.fail_after(3):
            await raw_sv.trade(radio, '169.254.1.2', '169.254.1.1')
        assert userspace_ip.lookup('pokepi-raw') is None
        assert radio.on_data is None

        async def handed(*args):
            return {'handed': True}
        upstream.run_session = handed
        try:
            with trio.fail_after(3):
                await raw_sv.trade(radio, '169.254.1.2', '169.254.1.1')
        except raw_profile.HandedOff:
            pass
        else:
            raise AssertionError('a host handoff before the trade must raise HandedOff')
        assert userspace_ip.lookup('pokepi-raw') is None
    finally:
        upstream.run_session = original
    print('SV bridge broadcast / receive / cleanup / handoff checks passed')


if __name__ == '__main__':
    trio.run(check)
