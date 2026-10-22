"""Run upstream's Scarlet/Violet joiner on the Pi's encrypted Ethernet bridge."""
from pi_paths import DATA_ROOT
import os
import struct
import time
import trio
from ldn import wlan
import raw_profile
from pokeldn import sv
from pokeldn.ldn.userspace_ip import userspace_port
from pokeldn.sv import reference


async def trade(interface, local_ip, host_ip):
    try:
        import sv_join as upstream
    except ImportError:
        import sv_join_upstream as upstream
    args = upstream.build_parser().parse_args([])
    args.ifname = 'pokepi-raw'
    args.trade_offer = [os.environ['POKEPI_TRADE_OFFER']]
    args.offer_out = os.environ['POKEPI_RECEIVED']
    args.trainer_name = os.environ.get('POKEPI_TRAINER', 'PI-TEST')
    args.session_join = args.answer_migration = args.net_ack = args.game_channel = True
    args.ack_flags = 0
    args.open_delay = args.record_delay = args.rtt_delay = .3
    args.announce_timeout = 20
    args.hold = 240
    args.fresh_pid = False  # validated and optionally regenerated on the desktop
    reference.fill_identity(args)
    started = time.monotonic()
    completed = False

    def record(**row):
        nonlocal completed
        if row.get('rec') == 'trade_done':
            completed = True
            args.hold = min(args.hold, time.monotonic() - started + 12)

    async with userspace_port(args.ifname, interface.source) as port:
        local_ip, host_ip = str(local_ip), str(host_ip)
        await port.add_address(local_ip, local_ip.rsplit('.', 1)[0]+'.255')
        await port.add_neighbor(host_ip, interface.target.address)

        async def receive(protocol, payload):
            frame = (bytes(interface.source) + bytes(interface.target.address)
                     + struct.pack('!H', protocol) + payload)
            await port.write(frame)

        async def reader():
            while True:
                event = await interface.next_event()
                if isinstance(event, wlan.DisassociationEvent):
                    raise RuntimeError('Switch disconnected from the Pi')

        async def writer():
            while True:
                frame = await port.read()
                destination = wlan.MACAddress(':'.join(f'{b:02x}' for b in frame[:6]))
                await interface.send_payload(interface.target.address,
                    int.from_bytes(frame[12:14], 'big'), frame[14:], destination)

        interface.on_data = receive
        try:
            async with trio.open_nursery() as nursery:
                nursery.start_soon(reader)
                nursery.start_soon(writer)
                outcome = await upstream.run_session(args, sv.session_keys(interface.target.ssid),
                    host_ip, bytes(interface.target.address), local_ip,
                    bytes(interface.source), record)
                nursery.cancel_scope.cancel()
        finally:
            interface.on_data = None
    if not completed:
        if (outcome or {}).get('handed'):
            raise raw_profile.HandedOff('Scarlet/Violet handed us the host role before the trade')
        raise RuntimeError('Scarlet/Violet session ended without a completed trade')
