"""Tests for dynamic and pre-flighted host port forwards."""

import socket

import pytest

from vmocs.error import HypervisorError
from vmocs.hypervisor import ALLOC_TOKEN, resolve_host_forwards


PORT_RANGE = [61500, 61600]


def _resolve(network=None, extra_hostfwd=(), reserved=()):
    return resolve_host_forwards(
        network, extra_hostfwd, PORT_RANGE, reserved=reserved)


def test_static_forwards_are_passed_through_unchanged():
    network, extra, forwards = _resolve(
        {'hostfwd': ['tcp:127.0.0.1:61501-:3389']})

    assert network['hostfwd'] == ['tcp:127.0.0.1:61501-:3389']
    assert extra == []
    assert forwards == [{
        'protocol': 'tcp',
        'host_address': '127.0.0.1',
        'host_port': 61501,
        'guest_port': 3389,
    }]


def test_alloc_token_picks_a_free_port():
    network, _, forwards = _resolve(
        {'hostfwd': [f'tcp:127.0.0.1:{ALLOC_TOKEN}-:3389']})

    port = forwards[0]['host_port']
    assert PORT_RANGE[0] <= port < PORT_RANGE[1]
    assert network['hostfwd'] == [f'tcp:127.0.0.1:{port}-:3389']


def test_every_allocated_forward_gets_a_distinct_port():
    """The probe socket closes before use, so each port must be remembered."""
    _, extra, forwards = _resolve(
        {'hostfwd': [f'tcp:127.0.0.1:{ALLOC_TOKEN}-:3389',
                     f'tcp:127.0.0.1:{ALLOC_TOKEN}-:5985']},
        extra_hostfwd=[f'tcp:127.0.0.1:{ALLOC_TOKEN}-:8080'])

    ports = [forward['host_port'] for forward in forwards]
    assert len(set(ports)) == 3
    assert extra == [f'tcp:127.0.0.1:{ports[2]}-:8080']


def test_allocation_avoids_the_reserved_ssh_port():
    _, _, forwards = _resolve(
        {'hostfwd': [f'tcp:127.0.0.1:{ALLOC_TOKEN}-:3389']},
        reserved={PORT_RANGE[0]})

    assert forwards[0]['host_port'] != PORT_RANGE[0]


def test_static_port_already_bound_is_named_instead_of_timing_out():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as held:
        held.bind(('127.0.0.1', 0))
        held.listen(1)
        port = held.getsockname()[1]

        with pytest.raises(HypervisorError, match=f'needs host port {port}'):
            _resolve({'hostfwd': [f'tcp:127.0.0.1:{port}-:3389']})


def test_static_port_colliding_with_the_ssh_port_is_rejected():
    with pytest.raises(HypervisorError, match='needs host port 61501'):
        _resolve({'hostfwd': ['tcp:127.0.0.1:61501-:3389']},
                 reserved={61501})


def test_conflict_message_points_at_the_alloc_token():
    with pytest.raises(HypervisorError, match=r'\{alloc\}'):
        _resolve({'hostfwd': ['tcp:127.0.0.1:61501-:3389']},
                 reserved={61501})


def test_wildcard_bind_is_allowed_but_warned_about(caplog):
    _, _, forwards = _resolve({'hostfwd': ['tcp::61502-:3389']})

    assert forwards[0]['host_address'] == '0.0.0.0'
    assert 'reachable from outside this node' in caplog.text


def test_loopback_bind_is_not_warned_about(caplog):
    _resolve({'hostfwd': ['tcp:127.0.0.1:61502-:3389']})

    assert 'reachable from outside this node' not in caplog.text


@pytest.mark.parametrize('entry', (
    'tcp:127.0.0.1:61501-:3389',
    'udp:127.0.0.1:61501-:3389',
    ':61501-:3389',
    'tcp::61501-:3389',
    'tcp:127.0.0.1:61501-10.0.2.15:3389',
))
def test_accepted_qemu_forward_spellings(entry):
    _, _, forwards = _resolve({'hostfwd': [entry]})

    assert forwards[0]['host_port'] == 61501


def test_protocol_defaults_to_tcp():
    _, _, forwards = _resolve({'hostfwd': [':61501-:3389']})

    assert forwards[0]['protocol'] == 'tcp'


def test_udp_protocol_is_preserved():
    _, _, forwards = _resolve({'hostfwd': ['udp:127.0.0.1:61501-:53']})

    assert forwards[0]['protocol'] == 'udp'


@pytest.mark.parametrize('entry', (
    'tcp:127.0.0.1:61501',
    'nonsense',
    'tcp:127.0.0.1:notaport-:3389',
))
def test_malformed_forwards_are_rejected_with_the_expected_syntax(entry):
    with pytest.raises(HypervisorError, match='not a valid host forward'):
        _resolve({'hostfwd': [entry]})


def test_passt_mode_leaves_forwards_alone():
    network = {'mode': 'passt', 'tcp-ports': ['61501:3389']}

    resolved, extra, forwards = _resolve(network)

    assert resolved == network
    assert extra == []
    assert forwards == []


def test_no_forwards_leaves_the_network_mapping_untouched():
    network = {'restrict': True}

    resolved, extra, forwards = _resolve(network)

    assert resolved == network
    assert extra == []
    assert forwards == []
