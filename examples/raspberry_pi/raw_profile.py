"""Title selection and exact link-code matching for the experimental Pi joiner."""
import os


class HandedOff(RuntimeError):
    """The console's member wait ran out and it handed us the host role before the trade.

    It destroys that network and browses again, so the joiner should rescan and rejoin."""


def game():
    value = os.environ.get('POKEPI_GAME', 'za')
    if value not in ('za', 'sv'):
        raise ValueError('POKEPI_GAME must be za or sv')
    return value


def session_code(network):
    """-> the link code a session of the selected game advertises, or None for any other network."""
    if game() == 'sv':
        from pokeldn import sv
        if (network.local_communication_id in (sv.COMM_ID_SCARLET, sv.COMM_ID_VIOLET)
                and network.scene_id == sv.SCENE_ID):
            return sv.link_code(network.application_data)
        return None
    from pokeldn import za
    if network.local_communication_id != 0x0100F43008C44000:
        return None
    try:
        return za.parse_advertise_data(bytes(network.application_data))['code']
    except (ValueError, IndexError, KeyError):
        return None


def wanted_code():
    """-> the link code this run joins; None (Z-A only) joins any."""
    return os.environ.get('POKEPI_CODE', '' if game() == 'sv' else None)


def matches(network):
    code = session_code(network)
    return code is not None and wanted_code() in (None, code)


def credentials():
    if game() == 'sv':
        from pokeldn import sv
        return sv.PASSPHRASE, sv.APP_VERSION
    from pokeldn import za
    return za.PASSPHRASE, 6
