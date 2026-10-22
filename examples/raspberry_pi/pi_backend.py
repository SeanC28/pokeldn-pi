"""Experimental join-only Nexmon adapter for pokeldn's vendored LDN library.

Requires a prepared wlan0/mon0 pair; it never installs drivers or deletes either
interface. The raw transport in raw_ldn.py is the tested path; the normal
kernel-managed station helpers below remain experimental and are not used by it.
"""
from pi_paths import DATA_ROOT
import contextlib
import pathlib
import socket
import trio
from ldn import wlan
from netlink import nl80211, route

NEXUTIL = str(DATA_ROOT / 'nexmon/utilities/nexutil/nexutil')


class PiNetlink:
    """brcmfmac delivers control-port traffic on the netdev, not netlink."""
    def __init__(self, net, bssid):
        self.net, self.bssid = net, bssid

    def __getattr__(self, name):
        return getattr(self.net, name)

    async def request(self, command, attrs=None, **kwargs):
        if command == nl80211.NL80211_CMD_CONNECT:
            attrs = dict(attrs)
            attrs.pop(nl80211.NL80211_ATTR_CONTROL_PORT_OVER_NL80211, None)
            # This driver does not advertise configurable control-port protocol.
            # Custom LDN frames are handled by PiStation's packet socket instead.
            attrs.pop(nl80211.NL80211_ATTR_CONTROL_PORT_ETHERTYPE, None)
            if self.bssid is not None:
                attrs[nl80211.NL80211_ATTR_MAC] = bytes(self.bssid)
        return await self.net.request(command, attrs, **kwargs)


class PiStation(wlan.Station):
    @contextlib.asynccontextmanager
    async def connect(self):
        self.control = trio.socket.socket(socket.AF_PACKET, socket.SOCK_RAW,
                                          socket.htons(wlan.ETH_P_OUI))
        try:
            await self.control.bind((self.name(), 0))
            async with super().connect():
                async with trio.open_nursery() as nursery:
                    nursery.start_soon(self.receive_control)
                    try:
                        yield
                    finally:
                        nursery.cancel_scope.cancel()
        finally:
            self.control.close()

    async def send_custom_frame(self, addr, frame):
        await self.control.send(bytes(addr) + bytes(self.address()) +
                                wlan.ETH_P_OUI.to_bytes(2, 'big') + frame)

    async def receive_control(self):
        while True:
            data = await self.control.recv(4096)
            if len(data) >= 14 and data[12:14] == wlan.ETH_P_OUI.to_bytes(2, 'big'):
                if self._host_address is not None and data[6:12] == bytes(self._host_address):
                    await self._events.put(wlan.CustomFrameEvent(
                        wlan.MACAddress(data[6:12]), data[14:]))


class PiMonitor(wlan.Monitor):
    async def set_channel(self, channel):
        if channel not in wlan.Channels:
            raise ValueError(f'Invalid channel: {channel}')
        result = await trio.run_process(
            [NEXUTIL, '-I', 'wlan0', f'-k{channel}'], capture_stdout=True,
            capture_stderr=True)
        # nexutil may exit zero after an ioctl failure; verify readback too.
        result = await trio.run_process(
            [NEXUTIL, '-I', 'wlan0', '-k'], capture_stdout=True,
            capture_stderr=True)
        output = result.stdout.decode().strip()
        if not output.endswith(f', {channel}'):
            raise RuntimeError(f'Channel readback failed: {output}')


def interface_info(name, phyname):
    path = pathlib.Path('/sys/class/net') / name
    if (path / 'phy80211').resolve().name != phyname:
        raise RuntimeError(f'{name} is not on {phyname}')
    return socket.if_nametoindex(name), wlan.MACAddress((path / 'address').read_text().strip())


class PiFactory(wlan.Factory):
    @contextlib.asynccontextmanager
    async def create_monitor(self, phyname, ifname, channel=None):
        index, address = interface_info('mon0', phyname)
        monitor = PiMonitor(self._wlan, self._router, 'mon0', index, address)
        try:
            await monitor.activate()
            if channel is not None:
                await monitor.set_channel(channel)
            yield monitor
        finally:
            monitor._socket.close()

    @contextlib.asynccontextmanager
    async def connect_network(self, phyname, ifname, ssid, channel, key, bssid=None):
        # brcmfmac supports one managed interface: reuse wlan0 rather than
        # asking upstream's factory to create a second station interface.
        await trio.run_process(['/usr/sbin/ip', 'link', 'set', 'mon0', 'down'])
        await trio.run_process([NEXUTIL, '-I', 'wlan0', '-m0'], capture_stdout=True)
        await trio.run_process([NEXUTIL, '-I', 'wlan0', '-c0'], capture_stdout=True)
        await trio.run_process([NEXUTIL, '-I', 'wlan0', f'-k{channel}'], capture_stdout=True)
        index, address = interface_info('wlan0', phyname)
        station = PiStation(PiNetlink(self._wlan, bssid), self._router, 'wlan0', index,
                               address, ssid, channel, key)
        ipv6 = pathlib.Path('/proc/sys/net/ipv6/conf/wlan0/disable_ipv6')
        previous = ipv6.read_text()
        try:
            async with contextlib.AsyncExitStack() as stack:
                with trio.fail_after(20):
                    await stack.enter_async_context(station.connect())
                yield station
        finally:
            ipv6.write_text(previous)

    @contextlib.asynccontextmanager
    async def create_ap(self, *args, **kwargs):
        raise NotImplementedError('Pi backend currently supports joining only')
        yield


@contextlib.asynccontextmanager
async def factory():
    async with nl80211.connect() as net:
        async with route.connect() as router:
            yield PiFactory(net, router)


def use():
    wlan.set_factory(factory)
