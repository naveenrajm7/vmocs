"""Configuration tests for template-driven network policy."""

import pytest

from vmocs.config import Config
from vmocs.error import InvalidConfigError
from vmocs.templates import TemplateConfig


def test_global_network_must_be_a_mapping(tmp_path):
    config = tmp_path / 'vmocs.yaml'
    config.write_text('network: user\n')

    with pytest.raises(InvalidConfigError, match="'network' must be a mapping"):
        Config(str(config))


def test_template_network_must_be_a_mapping(tmp_path):
    templates = tmp_path / 'templates.yaml'
    templates.write_text('agent:\n  network: restricted\n')

    with pytest.raises(InvalidConfigError, match="'network' must be a mapping"):
        TemplateConfig().load(str(templates))


def test_template_inherits_network_policy(tmp_path):
    templates = tmp_path / 'templates.yaml'
    templates.write_text(
        'secure:\n'
        '  network:\n'
        '    restrict: true\n'
        '    ipv6: false\n'
        'agent:\n'
        '  inherits: secure\n')
    loaded = TemplateConfig()
    loaded.load(str(templates))

    assert loaded['agent'].network == {'restrict': True, 'ipv6': False}
