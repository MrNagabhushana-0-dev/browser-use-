"""Tests for the optional Tor transport (browser_use/net/tor.py).

The pure-logic tests (config validation, torrc generation, proxy mapping, and
the fallback classifier) always run. The tests that need a real Tor skip when
neither a tor binary nor a running SOCKS proxy is present, rather than mocking
Tor: per the repo rules, nothing is mocked except the LLM.
"""

from __future__ import annotations

import shutil
import socket

import pytest

from browser_use.net.tor import (
	TorConfig,
	TorTransport,
	TorUnavailableError,
	should_fall_back,
)


def _tor_available() -> bool:
	if shutil.which('tor'):
		return True
	with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
		sock.settimeout(0.3)
		return sock.connect_ex(('127.0.0.1', 9050)) == 0


# --- config ---------------------------------------------------------------


def test_disabled_by_default():
	assert TorConfig().enabled is False


def test_exit_country_is_normalised():
	assert TorConfig(exit_country='DE').exit_country == 'de'
	assert TorConfig(exit_country=' us ').exit_country == 'us'


def test_bad_exit_country_rejected():
	with pytest.raises(Exception):
		TorConfig(exit_country='germany')
	with pytest.raises(Exception):
		TorConfig(exit_country='d1')


def test_unknown_field_forbidden():
	with pytest.raises(Exception):
		TorConfig(enabld=True)  # type: ignore[call-arg]


def test_port_bounds():
	with pytest.raises(Exception):
		TorConfig(socks_port=0)
	with pytest.raises(Exception):
		TorConfig(control_port=70000)


# --- torrc generation -----------------------------------------------------


def test_torrc_has_core_directives(tmp_path):
	t = TorTransport(TorConfig(socks_port=9150, control_port=9151))
	torrc = t._torrc(tmp_path)
	assert 'SocksPort 9150' in torrc
	assert 'ControlPort 9151' in torrc
	assert 'CookieAuthentication 1' in torrc
	assert f'DataDirectory {tmp_path}' in torrc


def test_torrc_exit_country_pins_exit(tmp_path):
	t = TorTransport(TorConfig(exit_country='nl'))
	torrc = t._torrc(tmp_path)
	assert 'ExitNodes {nl}' in torrc
	assert 'StrictNodes 1' in torrc


def test_torrc_bridges_enable_bridge_mode(tmp_path):
	bridge = 'obfs4 1.2.3.4:443 FINGERPRINT cert=abc iat-mode=0'
	t = TorTransport(TorConfig(bridges=[bridge]))
	torrc = t._torrc(tmp_path)
	assert 'UseBridges 1' in torrc
	assert f'Bridge {bridge}' in torrc


def test_torrc_omits_bridge_lines_without_bridges(tmp_path):
	torrc = TorTransport(TorConfig())._torrc(tmp_path)
	assert 'UseBridges' not in torrc
	assert 'Bridge ' not in torrc


# --- proxy wiring ---------------------------------------------------------


def test_proxy_settings_requires_start():
	t = TorTransport(TorConfig(enabled=True))
	with pytest.raises(AssertionError):
		t.proxy_settings()


def test_proxy_settings_maps_to_socks5_after_bootstrap():
	t = TorTransport(TorConfig(enabled=True, socks_port=9250))
	t._bootstrapped = True  # simulate a completed bootstrap for the pure mapping
	assert t.proxy_settings().server == 'socks5://127.0.0.1:9250'


async def test_start_rejects_when_enabled_false():
	t = TorTransport(TorConfig(enabled=False))
	with pytest.raises(AssertionError):
		await t.start()


# --- fallback classifier --------------------------------------------------


@pytest.mark.parametrize(
	'err',
	[
		'net::ERR_CONNECTION_RESET',
		'ERR_CONNECTION_REFUSED',
		'ERR_TIMED_OUT',
		'ERR_NAME_NOT_RESOLVED',
		'ERR_BLOCKED_BY_ADMINISTRATOR',
		'HTTP 451 Unavailable For Legal Reasons',
	],
)
def test_network_errors_trigger_fallback(err):
	assert should_fall_back(err) is True


@pytest.mark.parametrize(
	'wall',
	[
		'Please complete the CAPTCHA to continue',
		'Our systems have detected unusual traffic',
		'Sign in to confirm you are not a bot',
		'Just a moment... (Cloudflare)',
		'https://www.google.com/sorry/index',
		'Attention Required! | Cloudflare',
	],
)
def test_bot_walls_never_trigger_fallback(wall):
	assert should_fall_back(wall) is False


def test_bot_wall_vetoes_even_with_network_marker():
	# A page that both timed out AND shows a captcha must not be retried on Tor.
	assert should_fall_back('ERR_TIMED_OUT then a reCAPTCHA appeared') is False


def test_unrelated_and_empty_text_do_not_fall_back():
	assert should_fall_back('') is False
	assert should_fall_back('HTTP 200 OK, page rendered fine') is False


# --- real Tor (skipped when unavailable) ----------------------------------


@pytest.mark.skipif(not _tor_available(), reason='no tor binary or running SOCKS proxy on 9050')
async def test_start_bootstraps_and_exposes_socks():
	import asyncio

	t = TorTransport(TorConfig(enabled=True, socks_port=9050, bootstrap_timeout_s=120))
	try:
		await asyncio.wait_for(t.start(), timeout=125)
		assert t._bootstrapped is True
		assert t.proxy_settings().server == 'socks5://127.0.0.1:9050'
	finally:
		await t.stop()


@pytest.mark.skipif(_tor_available(), reason='tor is present, so the unavailable path cannot be exercised for real')
async def test_missing_tor_raises_clear_error():
	# With no running SOCKS proxy and no tor binary, the error names the fix.
	# Uses a port that is genuinely closed on this host (no mocking).
	t = TorTransport(TorConfig(enabled=True, socks_port=9050, tor_binary=None))
	with pytest.raises(TorUnavailableError) as excinfo:
		await t.start()
	assert 'Install Tor' in str(excinfo.value)
