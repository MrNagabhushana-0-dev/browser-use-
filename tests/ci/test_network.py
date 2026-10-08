"""Network routing: Tor modes, exit-country choice, fallback rules, and the agent-facing tools.

Nothing here mocks Tor or the browser. The control-port parsers are tested on replies written in
the control-spec's own format; the router's state rules run for real; the Retinat tests drive a real
browser against local pages. Tests that need Tor to be *absent* skip when one is present, and the
one real-bootstrap test lives in test_tor.py.
"""

from __future__ import annotations

import asyncio
import shutil
import socket

import mcp.types as types
import pytest
from pytest_httpserver import HTTPServer

from browser_use.net import (
	NetworkMode,
	NetworkPolicyError,
	NetworkRouter,
	classify_navigation,
	tor_chromium_args,
)
from browser_use.net.control import (
	parse_circuit_exit,
	parse_country,
	parse_router_ip,
	parse_stream_circuit,
	parse_version,
	read_reply,
)
from browser_use.retinat import RetinatServer


def _tor_present() -> bool:
	if shutil.which('tor'):
		return True
	with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
		sock.settimeout(0.3)
		return sock.connect_ex(('127.0.0.1', 9050)) == 0


no_tor = pytest.mark.skipif(_tor_present(), reason='a Tor is present, so the no-Tor path cannot be exercised')

# --- control-port replies, in the control-spec's format -----------------------

CIRCUITS = [
	'250+circuit-status=',
	'7 BUILT $AAAA~guard,$BBBB~middle,$CCCC~old PURPOSE=GENERAL TIME_CREATED=2026-09-30T10:00:00.000000',
	'250 BUILT $AAAA~g,$BBBB~m,$DDDD~notours PURPOSE=GENERAL TIME_CREATED=2026-09-30T10:05:00.000000',
	'9 BUILT $AAAA~guard,$BBBB~middle,$EEEE~hsdir PURPOSE=HS_CLIENT_HSDIR TIME_CREATED=2026-09-30T10:09:00.000000',
	'11 EXTENDED $AAAA~guard PURPOSE=GENERAL TIME_CREATED=2026-09-30T10:10:00.000000',
	'.',
	'250 OK',
]


def test_the_exit_is_the_last_hop_of_the_newest_built_general_circuit():
	assert parse_circuit_exit(CIRCUITS) == 'DDDD'


def test_the_circuit_carrying_our_stream_is_preferred():
	streams = ['250-stream-status=', '250-stream-status=12 SUCCEEDED 7 example.org:443', '250 OK']
	assert parse_stream_circuit(streams) == '7'
	assert parse_circuit_exit(CIRCUITS, prefer_circuit='7') == 'CCCC'


def test_no_built_circuit_means_no_exit():
	assert parse_circuit_exit(['250-circuit-status=', '250 OK']) is None
	assert parse_stream_circuit(['250-stream-status=', '250 OK']) is None


def test_router_address_and_country_are_read_from_their_replies():
	ns = ['250+ns/id/DDDD=', 'r nick ZGRk digest 2026-09-30 10:00:00 203.0.113.9 9001 0', 's Exit Fast Running', '.', '250 OK']
	assert parse_router_ip(ns) == '203.0.113.9'
	assert parse_country(['250-ip-to-country/203.0.113.9=de', '250 OK']) == 'de'
	assert parse_country(['250-ip-to-country/203.0.113.9=??', '250 OK']) is None


def test_version_is_parsed_to_a_tuple():
	assert parse_version(['250-version=0.4.8.13 (git-abcdef)', '250 OK']) == ('0.4.8.13', (0, 4, 8, 13))
	assert parse_version(['250 OK']) is None


async def test_a_data_block_line_that_looks_like_a_reply_end_does_not_end_the_reply():
	reader = asyncio.StreamReader()
	reader.feed_data(('\r\n'.join(CIRCUITS) + '\r\n').encode())
	assert await read_reply(reader) == CIRCUITS


# --- classifying what a page did ---------------------------------------------


def test_a_bot_wall_wins_over_everything_and_is_never_a_reason_to_fall_back():
	assert classify_navigation('', title='Just a moment...', text='Checking your browser') == 'walled'
	assert classify_navigation('ERR_CONNECTION_RESET', text="Sign in to confirm you're not a bot") == 'walled'
	assert classify_navigation('', title='429 Too Many Requests') == 'walled'


def test_a_country_block_page_and_http_451_are_geo_blocks():
	assert (
		classify_navigation('', title='Video unavailable', text='This video is not available in your country.') == 'geo_blocked'
	)
	assert classify_navigation('', status=451) == 'geo_blocked'


def test_connection_failures_are_network_errors_and_normal_pages_are_fine():
	assert classify_navigation('net::ERR_CONNECTION_RESET') == 'network_error'
	assert classify_navigation('', title='Hello', text='a perfectly ordinary page') == 'ok'


# --- the router's rules -------------------------------------------------------


def test_the_default_route_is_direct_with_nothing_started():
	router = NetworkRouter()
	assert router.mode is NetworkMode.OFF and router.route == 'direct' and not router.uses_tor


def test_environment_sets_the_mode_and_country(monkeypatch):
	monkeypatch.setenv('BROWSER_USE_NETWORK', 'auto')
	monkeypatch.setenv('BROWSER_USE_EXIT_COUNTRY', 'JP')
	router = NetworkRouter.from_env()
	assert router.mode is NetworkMode.AUTO and router.exit_country == 'jp'
	monkeypatch.setenv('BROWSER_USE_NETWORK', 'sometimes')
	with pytest.raises(NetworkPolicyError):
		NetworkRouter.from_env()


def test_a_bad_country_is_refused_and_any_clears_it():
	with pytest.raises(NetworkPolicyError):
		NetworkRouter(exit_country='germany')
	assert NetworkRouter(exit_country='any').exit_country is None


async def test_a_bad_mode_is_refused_and_nothing_changes():
	router = NetworkRouter(NetworkMode.AUTO, 'de')
	with pytest.raises(NetworkPolicyError):
		await router.set_network('sometimes')
	assert router.mode is NetworkMode.AUTO and router.exit_country == 'de'


async def test_off_and_auto_can_be_set_without_starting_tor():
	router = NetworkRouter()
	summary = await router.set_network('auto', 'de', reason='research')
	assert router.mode is NetworkMode.AUTO and router.exit_country == 'de'
	assert 'auto' in summary and 'DE' in summary and router.route == 'direct'
	assert router.history[-1]['reason'] == 'research'
	assert await router.session_kwargs() == {}


@no_tor
async def test_always_without_tor_fails_up_front_and_keeps_the_old_route():
	router = NetworkRouter(NetworkMode.AUTO, 'de')
	with pytest.raises(NetworkPolicyError, match='Install Tor'):
		await router.set_network('always', 'jp')
	assert router.mode is NetworkMode.AUTO and router.exit_country == 'de' and router.last_error


def test_auto_falls_back_on_network_and_geo_failures_but_never_on_a_wall():
	auto, off = NetworkRouter(NetworkMode.AUTO), NetworkRouter(NetworkMode.OFF)
	assert auto.wants_fallback('network_error') and auto.wants_fallback('geo_blocked')
	assert not auto.wants_fallback('walled') and not auto.wants_fallback('ok')
	assert not off.wants_fallback('network_error')


@no_tor
async def test_engaging_without_tor_says_so_instead_of_pretending():
	router = NetworkRouter(NetworkMode.AUTO, 'de')
	assert await router.engage('geo_blocked at example.org') is False
	assert router.route == 'direct' and router.last_error
	assert 'Tor is not installed' in await router.status() or 'Last Tor problem' in await router.status()


async def test_reading_the_status_never_starts_a_tor():
	# `_network_status` is advertised read-only, so asking must not launch or restart anything.
	router = NetworkRouter(NetworkMode.ALWAYS, 'de')  # constructing it starts nothing
	assert router._pool.peek('de') is None
	text = await router.status()
	assert 'Tor is not running yet' in text
	assert router._pool.peek('de') is None and router.last_error is None


def test_plain_http_is_refused_over_tor_but_https_and_loopback_are_not():
	router = NetworkRouter(NetworkMode.ALWAYS)  # constructing it starts nothing
	with pytest.raises(NetworkPolicyError, match='exit relay'):
		router.check_url('http://news.example/')
	router.check_url('https://news.example/')
	router.check_url('http://127.0.0.1:8000/')
	NetworkRouter(NetworkMode.ALWAYS, allow_http=True).check_url('http://news.example/')
	NetworkRouter(NetworkMode.OFF).check_url('http://news.example/')


def test_the_leak_guards_cover_quic_webrtc_and_ipv6():
	args = tor_chromium_args()
	assert '--disable-quic' in args and '--disable-ipv6' in args
	assert '--force-webrtc-ip-handling-policy=disable_non_proxied_udp' in args


# --- the agent-facing tools (real server, real browser) ------------------------

PAGE = '<!doctype html><title>Hello</title><body><h1>Hello from a page</h1></body>'
GEO = '<!doctype html><title>Video unavailable</title><body>This video is not available in your country.</body>'
WALL = '<!doctype html><title>Just a moment...</title><body>Checking your browser before accessing the site.</body>'


@pytest.fixture(scope='module')
def site():
	server = HTTPServer()
	server.start()
	server.expect_request('/').respond_with_data(PAGE, content_type='text/html')
	server.expect_request('/geo').respond_with_data(GEO, content_type='text/html')
	server.expect_request('/wall').respond_with_data(WALL, content_type='text/html')
	yield server
	server.stop()


@pytest.fixture
async def retinat(tmp_path, monkeypatch):
	monkeypatch.setenv('BROWSER_USE_EYES_NOW', str(tmp_path / 'now.json'))
	server = RetinatServer(network=NetworkRouter(NetworkMode.AUTO))
	server.config.setdefault('browser_profile', {}).update({'headless': True, 'user_data_dir': str(tmp_path / 'profile')})
	yield server
	await server._close_all_sessions()


async def _call(server, name: str, arguments: dict):
	handler = server.server.get_request_handler('tools/call')
	assert handler is not None
	result = await handler.handler(None, types.CallToolRequestParams(name=name, arguments=arguments))  # type: ignore[arg-type]
	assert isinstance(result, types.CallToolResult)
	return result


def _text(result) -> str:
	return '\n'.join(b.text for b in result.content if isinstance(b, types.TextContent))


async def test_both_servers_offer_the_route_tools(retinat):
	from browser_use.mcp.server import BrowserUseServer

	for server, prefix in ((retinat, 'retinat'), (BrowserUseServer(), 'browser')):
		handler = server.server.get_request_handler('tools/list')
		assert handler is not None
		listed = await handler.handler(None, types.PaginatedRequestParams())  # type: ignore[arg-type]
		assert isinstance(listed, types.ListToolsResult)
		names = {t.name for t in listed.tools}
		assert {f'{prefix}_network', f'{prefix}_network_status'} <= names


async def test_retinat_defaults_to_auto_and_the_library_to_off(monkeypatch):
	monkeypatch.delenv('BROWSER_USE_NETWORK', raising=False)
	from browser_use.mcp.server import BrowserUseServer

	assert RetinatServer().network.mode is NetworkMode.AUTO
	assert BrowserUseServer().network.mode is NetworkMode.OFF


async def test_status_and_setting_the_route_need_no_browser(retinat):
	status = await _call(retinat, 'retinat_network_status', {})
	assert 'auto' in _text(status) and 'direct' in _text(status)
	changed = await _call(retinat, 'retinat_network', {'mode': 'off', 'exit_country': 'de', 'reason': 'test'})
	assert not changed.is_error and 'off' in _text(changed) and retinat.network.mode is NetworkMode.OFF
	assert retinat.browser_session is None, 'choosing a route must not launch a browser'


async def test_a_bad_country_or_mode_comes_back_as_an_error_the_agent_can_read(retinat):
	assert _text(await _call(retinat, 'retinat_network', {'mode': 'auto', 'exit_country': 'germany'})).startswith('Error:')
	nonsense = await _call(retinat, 'retinat_network', {'mode': 'sometimes'})
	assert _text(nonsense).startswith('Error:') and nonsense.is_error
	assert (nonsense.structured_content or {}).get('effect_state') == 'none', 'a refused route change changed nothing'
	assert retinat.network.mode is NetworkMode.AUTO


async def test_an_attached_chrome_keeps_its_own_connection(retinat):
	retinat.cdp_url = 'http://127.0.0.1:9222'
	refused = _text(await _call(retinat, 'retinat_network', {'mode': 'always'}))
	assert refused.startswith('Error:') and '--cdp-url' in refused
	assert retinat.network.mode is NetworkMode.AUTO


async def test_a_normal_page_opens_direct_with_no_route_note(retinat, site):
	opened = _text(await _call(retinat, 'retinat_open', {'url': site.url_for('/')}))
	assert opened.startswith('Opened "Hello"') and 'Tor' not in opened
	assert retinat.network.route == 'direct'


@no_tor
async def test_a_geo_block_in_auto_tries_tor_and_says_plainly_that_there_is_none(retinat, site):
	opened = _text(await _call(retinat, 'retinat_open', {'url': site.url_for('/geo')}))
	assert 'Tor fallback unavailable' in opened and 'Install Tor' in opened, opened
	assert retinat.network.last_outcome == 'geo_blocked' and retinat.network.route == 'direct'


async def test_the_same_geo_block_is_left_alone_when_the_route_is_off(retinat, site):
	await _call(retinat, 'retinat_network', {'mode': 'off'})
	opened = _text(await _call(retinat, 'retinat_open', {'url': site.url_for('/geo')}))
	assert 'Tor' not in opened and retinat.network.route == 'direct'


async def test_a_bot_wall_is_reported_and_never_sent_through_tor(retinat, site):
	opened = _text(await _call(retinat, 'retinat_open', {'url': site.url_for('/wall')}))
	assert opened.startswith('BLOCKED:') and 'Tor fallback' not in opened, opened
	assert retinat.network.last_outcome == 'walled' and retinat.network.route == 'direct'


@no_tor
async def test_a_refused_connection_in_auto_reports_the_error_and_the_missing_tor(retinat):
	with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
		sock.bind(('127.0.0.1', 0))
		closed_port = sock.getsockname()[1]
	result = await _call(retinat, 'retinat_open', {'url': f'http://127.0.0.1:{closed_port}/'})
	assert result.is_error
	text = _text(result)
	assert 'ERR_CONNECTION_REFUSED' in text and 'Tor fallback unavailable' in text, text


# --- Chromium against a real SOCKS5 proxy: what the Tor route relies on -----------
# Not a stand-in for Tor, which can't run here. This proves the half that is ours: the flags the
# router produces are accepted, traffic goes through the proxy, the hostname is resolved by the
# proxy (never locally), and a dead proxy means failure rather than a quiet direct connection.


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
	try:
		while data := await reader.read(65536):
			writer.write(data)
			await writer.drain()
	except (ConnectionError, OSError):
		pass
	finally:
		writer.close()


async def _socks5_to(target_port: int, seen: list[str]) -> asyncio.Server:
	"""A real SOCKS5 server (no auth, like Chromium expects) that sends every request to one local port."""

	async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
		try:
			_, methods = await reader.readexactly(2)
			await reader.readexactly(methods)
			writer.write(b'\x05\x00')
			_, _, _, atyp = await reader.readexactly(4)
			if atyp == 3:
				host = (await reader.readexactly((await reader.readexactly(1))[0])).decode()
			else:
				host = socket.inet_ntoa(await reader.readexactly(4)) if atyp == 1 else '[ipv6]'
				if atyp == 4:
					await reader.readexactly(16)
			await reader.readexactly(2)
			seen.append(host)
			upstream_reader, upstream_writer = await asyncio.open_connection('127.0.0.1', target_port)
			writer.write(b'\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00')
			await writer.drain()
			await asyncio.gather(_pipe(reader, upstream_writer), _pipe(upstream_reader, writer))
		except (asyncio.IncompleteReadError, ConnectionError, OSError):
			writer.close()

	return await asyncio.start_server(handle, '127.0.0.1', 0)


async def _title(session) -> str:
	cdp = await session.get_or_create_cdp_session(focus=False)
	r = await cdp.cdp_client.send.Runtime.evaluate(params={'expression': 'document.title'}, session_id=cdp.session_id)
	return str((r.get('result') or {}).get('value') or '')


async def test_chromium_uses_the_proxy_resolves_names_there_and_fails_closed(site):
	from browser_use.browser import BrowserProfile, BrowserSession
	from browser_use.browser.profile import ProxySettings

	seen: list[str] = []
	proxy = await _socks5_to(site.port, seen)
	proxy_port = proxy.sockets[0].getsockname()[1]
	# `direct.test` resolves locally here, so if the browser ever bypassed the proxy it would succeed.
	args = [*tor_chromium_args(), '--host-resolver-rules=MAP direct.test 127.0.0.1']
	session = BrowserSession(
		browser_profile=BrowserProfile(
			headless=True,
			user_data_dir=None,
			keep_alive=False,
			enable_default_extensions=False,
			proxy=ProxySettings(server=f'socks5://127.0.0.1:{proxy_port}'),
			args=args,
		)
	)
	await session.start()
	try:
		await session.navigate_to(f'http://direct.test:{site.port}/')
		assert await _title(session) == 'Hello', 'the page must load through the proxy'
		assert seen and seen[-1] == 'direct.test', f'the proxy must be handed the hostname, not an address: {seen}'

		proxy.close()
		await proxy.wait_closed()
		before = len(seen)
		with pytest.raises(Exception) as refused:
			await session.navigate_to(f'http://direct.test:{site.port}/')
		assert 'ERR_' in str(refused.value), str(refused.value)
		assert len(seen) == before, 'with the proxy down the browser must fail, not connect another way'
	finally:
		proxy.close()
		await session.kill()


FORM = '<!doctype html><title>Form</title><body><input id="user" type="text"><input id="pw" type="password"></body>'


async def _focus_and_type(server, element_id: str, text: str):
	cdp = await server.browser_session.get_or_create_cdp_session(focus=False)
	await cdp.cdp_client.send.Runtime.evaluate(
		params={'expression': f'document.getElementById("{element_id}").focus()'}, session_id=cdp.session_id
	)
	return await _call(server, 'retinat_type', {'text': text})


async def test_nothing_is_typed_into_a_password_field_while_routed_through_tor(retinat, site):
	site.expect_request('/form').respond_with_data(FORM, content_type='text/html')
	await _call(retinat, 'retinat_network', {'mode': 'off'})
	await _call(retinat, 'retinat_open', {'url': site.url_for('/form')})

	direct = await _focus_and_type(retinat, 'pw', 'hunter2')  # direct: the person's own business
	assert not direct.is_error

	retinat.network.mode = NetworkMode.ALWAYS  # the browser is open; the route is now Tor
	refused = await _focus_and_type(retinat, 'pw', 'hunter2')
	assert refused.is_error and 'password' in _text(refused), _text(refused)

	ordinary = await _focus_and_type(retinat, 'user', 'hello')
	assert not ordinary.is_error, _text(ordinary)
