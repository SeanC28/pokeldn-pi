"""Title selection and exact link-code matching for the experimental Pi joiner."""
import os


def game():
    value = os.environ.get('POKEPI_GAME', 'za')
    if value not in ('za', 'sv'):
        raise ValueError('POKEPI_GAME must be za or sv')
    return value


def matches(network):
    if game() == 'sv':
        from pokeldn import sv
        return (network.local_communication_id in (sv.COMM_ID_SCARLET, sv.COMM_ID_VIOLET)
                and network.scene_id == sv.SCENE_ID
                and sv.link_code(network.application_data) == os.environ.get('POKEPI_CODE', ''))
    from pokeldn import za
    if network.local_communication_id != 0x0100F43008C44000:
        return False
    code = os.environ.get('POKEPI_CODE')
    return code is None or za.parse_advertise_data(bytes(network.application_data))['code'] == code


def credentials():
    if game() == 'sv':
        from pokeldn import sv
        return sv.PASSPHRASE, sv.APP_VERSION
    from pokeldn import za
    return za.PASSPHRASE, 6
