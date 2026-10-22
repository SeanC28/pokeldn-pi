"""Adapt upstream Z-A GameStreams to the asynchronous Pi radio transport."""
from pi_paths import DATA_ROOT
import time
from collections import deque
from pathlib import Path
import za_join as upstream


class SingleTrade(upstream.GameStreams):
    def _answer_trade(self, inner, elapsed):
        if self.trades or (inner[:2] == b'\x01\x04' and not self.picked):
            return
        super()._answer_trade(inner, elapsed)


class GameBridge:
    def __init__(self, session, offer):
        self.session = session
        self.outbox = deque()
        self.started = time.monotonic()
        args = upstream.build_parser().parse_args([])
        args.game_dir = str(Path(upstream.za.reference.__file__).parent / 'data')
        args.trade_offer = [str(Path(offer).resolve())]
        args.trainer_name = 'PI-TEST'
        args.fresh_pid = False
        args.renew_offer = False
        destination = DATA_ROOT / 'private/received'
        destination.mkdir(mode=0o700, exist_ok=True)
        args.offer_out = str(destination / (time.strftime('%Y%m%d-%H%M%S') + '.pa9'))
        self.game = SingleTrade(args, self.send, self.send_messages, lambda **kw: None)
        missing = set(upstream.za.reference.NAMES) - self.game.ref.keys()
        if missing:
            raise RuntimeError(f'Missing game reference data: {sorted(missing)}')

    def send(self, proto, payload, **kwargs):
        self.send_messages([(proto, payload, kwargs.pop('msgflags', None))], **kwargs)

    def send_messages(self, items, **kwargs):
        self.outbox.append((items, kwargs))

    def receive(self, proto, payload):
        self.game.on_message(proto, payload, time.monotonic() - self.started)

    async def pump(self):
        if not self.session.conn.connected:
            return
        self.game.pump(self.session.conn.host_var, self.session.conn.our_var,
                       time.monotonic() - self.started)
        while self.outbox:
            items, kw = self.outbox.popleft()
            await self.session.send_items(items, dst=kw['dst_var'], src=kw['src_var'],
                establishing=kw.get('establishing', False),
                footer=kw.get('footer_var'), compress=kw.get('compress', False),
                pktid=kw.get('pktid'))
