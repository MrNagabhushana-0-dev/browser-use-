"""Trusting a TLS-terminating proxy without turning verification off.

Corporate networks and agent sandboxes re-terminate TLS, so Chromium sees a certificate
signed by a CA it has never heard of and refuses every https page. Chromium has no
--cacert; the nearest thing that is not "ignore all certificate errors" is
--ignore-certificate-errors-spki-list, which accepts exactly the public keys you name.
"""

import subprocess
import tempfile
from pathlib import Path

import pytest

from browser_use.browser.profile import MAX_PROXY_CA_CERTS, BrowserProfile, proxy_ca_pins


@pytest.fixture(autouse=True)
def own_config_dir(tmp_path, monkeypatch):
	"""A CA learned in one test (or on this machine) must not leak into another: each test keeps its own."""
	monkeypatch.setenv('BROWSER_USE_CONFIG_DIR', str(tmp_path / 'config'))


@pytest.fixture(scope='module')
def a_certificate(tmp_path_factory) -> Path:
	"""A real self-signed certificate, generated rather than committed."""
	path = tmp_path_factory.mktemp('ca') / 'proxy-ca.crt'
	key = path.with_suffix('.key')
	subprocess.run(
		[
			'openssl',
			'req',
			'-x509',
			'-newkey',
			'rsa:2048',
			'-nodes',
			'-keyout',
			str(key),
			'-out',
			str(path),
			'-days',
			'1',
			'-subj',
			'/CN=Test Proxy CA',
		],
		capture_output=True,
		check=True,
		timeout=60,
	)
	return path


def test_a_certificate_yields_a_stable_pin(a_certificate):
	pins = proxy_ca_pins(a_certificate)
	assert len(pins) == 1
	assert len(pins[0]) == 44 and pins[0].endswith('='), f'not a base64 sha256: {pins[0]!r}'
	assert proxy_ca_pins(a_certificate) == pins, 'the same certificate must pin the same way twice'


def test_the_pin_reaches_the_browser_command_line(a_certificate):
	with tempfile.TemporaryDirectory() as user_data_dir:
		args = BrowserProfile(proxy_ca_cert=str(a_certificate), user_data_dir=user_data_dir).get_args()
		pinned = [a for a in args if a.startswith('--ignore-certificate-errors-spki-list=')]
		assert len(pinned) == 1
		assert proxy_ca_pins(a_certificate)[0] in pinned[0]

		# And nothing weaker: the blanket flag must never appear.
		assert not any(a == '--ignore-certificate-errors' for a in args)


def test_no_certificate_means_no_flag(monkeypatch):
	monkeypatch.delenv('BROWSER_USE_PROXY_CA_CERT', raising=False)  # the test is about none being set
	with tempfile.TemporaryDirectory() as user_data_dir:
		args = BrowserProfile(user_data_dir=user_data_dir).get_args()
		assert not any('spki-list' in a for a in args)


def test_a_full_trust_bundle_is_refused(tmp_path, a_certificate):
	"""Pointing this at a system bundle pins hundreds of public CAs Chromium already has,
	and produces a command line long enough to break the launch."""
	bundle = tmp_path / 'bundle.pem'
	bundle.write_text(a_certificate.read_text() * (MAX_PROXY_CA_CERTS + 2))
	assert proxy_ca_pins(bundle) == []


def test_an_unreadable_certificate_never_stops_the_browser(tmp_path):
	"""A browser that will not launch because a CA file moved is worse than one that
	launches and reports a certificate error you can actually read."""
	assert proxy_ca_pins(tmp_path / 'does-not-exist.crt') == []

	junk = tmp_path / 'junk.crt'
	junk.write_text('this is not a certificate')
	assert proxy_ca_pins(junk) == []

	truncated = tmp_path / 'truncated.crt'
	truncated.write_text('-----BEGIN CERTIFICATE-----\nnot base64 at all\n-----END CERTIFICATE-----')
	assert proxy_ca_pins(truncated) == []


@pytest.fixture(scope='module')
def https_site(a_certificate):
	"""A local HTTPS page signed by a CA Chromium has never heard of, as behind a TLS proxy."""
	import ssl

	from pytest_httpserver import HTTPServer

	ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
	ctx.load_cert_chain(str(a_certificate), str(a_certificate.with_suffix('.key')))
	server = HTTPServer(ssl_context=ctx)
	server.start()
	server.expect_request('/').respond_with_data('<p>behind the proxy</p>', content_type='text/html')
	yield server
	server.stop()


def test_a_setting_that_points_at_nothing_still_explains_the_error(tmp_path, monkeypatch, a_certificate):
	"""A CA path that is missing (a config shared between machines) or a whole trust bundle trusts nothing. The
	error used to go quiet whenever the setting was present; it must say the file gave no certificate."""
	from browser_use.browser.session import _certificate_hint

	monkeypatch.setenv('SSL_CERT_FILE', str(a_certificate))
	error = 'net::ERR_CERT_AUTHORITY_INVALID'
	missing = tmp_path / 'not-here.crt'
	hint = _certificate_hint(error, BrowserProfile(proxy_ca_cert=str(missing), user_data_dir=None))
	assert str(missing) in hint and 'no readable certificate' in hint, hint
	bundle = tmp_path / 'bundle.crt'
	bundle.write_text(a_certificate.read_text() * (MAX_PROXY_CA_CERTS + 1))
	assert 'no readable certificate' in _certificate_hint(error, BrowserProfile(proxy_ca_cert=str(bundle), user_data_dir=None))
	assert _certificate_hint(error, BrowserProfile(proxy_ca_cert=str(a_certificate), user_data_dir=None)) == '', (
		'a CA that pins is in effect: the hint would mislead'
	)


async def test_the_named_setting_then_opens_the_page(https_site, a_certificate):
	from browser_use.browser import BrowserSession

	session = BrowserSession(
		browser_profile=BrowserProfile(headless=True, user_data_dir=None, keep_alive=False, proxy_ca_cert=str(a_certificate))
	)
	await session.start()
	try:
		await session.navigate_to(https_site.url_for('/').replace('http://', 'https://'))
		cdp = await session.get_or_create_cdp_session(focus=False)
		body = await cdp.cdp_client.send.Runtime.evaluate(
			params={'expression': 'document.body.innerText', 'returnByValue': True}, session_id=cdp.session_id
		)
		assert body['result']['value'] == 'behind the proxy'
	finally:
		await session.kill()


def _other_certificate(folder: Path, subject: str = '/CN=Some Other CA') -> Path:
	path = folder / f'other-{abs(hash(subject))}.crt'
	subprocess.run(
		['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', str(path.with_suffix('.key')), '-out', str(path)]
		+ ['-days', '1', '-subj', subject],
		capture_output=True,
		check=True,
		timeout=60,
	)
	return path


async def _fails_then(url: str) -> str:
	from browser_use.browser import BrowserSession

	session = BrowserSession(browser_profile=BrowserProfile(headless=True, user_data_dir=None, keep_alive=False))
	await session.start()
	try:
		with pytest.raises(Exception) as failure:
			await session.navigate_to(url)
		return str(failure.value)
	finally:
		await session.kill()


async def test_the_proxy_ca_the_environment_trusts_is_learned_from_what_the_browser_was_shown(
	https_site, a_certificate, tmp_path, monkeypatch
):
	"""Behind a proxy that re-signs TLS, curl and Python trust its CA through SSL_CERT_FILE; Chromium refuses every
	page until someone finds BROWSER_USE_PROXY_CA_CERT. When a page fails on a chain that a CA in that bundle
	verifiably signed, that CA is saved for this network, and the next browser trusts it with no setting."""
	import certifi

	from browser_use.browser import BrowserSession
	from browser_use.browser.profile import PROXY_CA_LEARNED

	monkeypatch.delenv('BROWSER_USE_PROXY_CA_CERT', raising=False)
	bundle = tmp_path / 'bundle.crt'
	bundle.write_text(Path(certifi.where()).read_text() + a_certificate.read_text())  # public roots plus the proxy's
	monkeypatch.setenv('SSL_CERT_FILE', str(bundle))
	url = https_site.url_for('/').replace('http://', 'https://')

	message = await _fails_then(url)
	assert PROXY_CA_LEARNED in message and 'Test Proxy CA' in message, message

	session = BrowserSession(browser_profile=BrowserProfile(headless=True, user_data_dir=None, keep_alive=False))
	assert session.browser_profile.proxy_ca_cert and proxy_ca_pins(session.browser_profile.proxy_ca_cert) == proxy_ca_pins(
		a_certificate
	), 'the learned CA is exactly the one that signed the page'
	await session.start()
	try:
		await session.navigate_to(url)
		cdp = await session.get_or_create_cdp_session(focus=False)
		body = await cdp.cdp_client.send.Runtime.evaluate(
			params={'expression': 'document.body.innerText', 'returnByValue': True}, session_id=cdp.session_id
		)
		assert body['result']['value'] == 'behind the proxy'
	finally:
		await session.kill()


async def test_a_ca_that_did_not_sign_what_the_browser_was_shown_is_never_learned(https_site, tmp_path, monkeypatch):
	"""Only the CA that verifiably signed the refused chain, and only if the environment's own bundle holds it."""
	from browser_use.browser.profile import PROXY_CA_LEARNED, learned_proxy_ca

	monkeypatch.delenv('BROWSER_USE_PROXY_CA_CERT', raising=False)
	monkeypatch.setenv('SSL_CERT_FILE', str(_other_certificate(tmp_path)))
	message = await _fails_then(https_site.url_for('/').replace('http://', 'https://'))
	assert 'ERR_CERT_AUTHORITY_INVALID' in message and PROXY_CA_LEARNED not in message, message
	assert 'BROWSER_USE_PROXY_CA_CERT' in message and 'SSL_CERT_FILE' in message, 'the error still names the setting'
	assert learned_proxy_ca() is None

	# a CA with the very name of the one that signed the page, but another key: the name alone proves nothing
	monkeypatch.setenv('SSL_CERT_FILE', str(_other_certificate(tmp_path, '/CN=Test Proxy CA')))
	message = await _fails_then(https_site.url_for('/').replace('http://', 'https://'))
	assert PROXY_CA_LEARNED not in message and learned_proxy_ca() is None, message


async def test_the_first_open_on_such_a_network_succeeds_through_the_mcp_server(https_site, a_certificate, tmp_path, monkeypatch):
	"""An agent should not have to know any of this: Retinat's first eye_open learns the CA, restarts its browser
	and opens the page in the same call."""
	from browser_use.mcp.server import types
	from browser_use.retinat.server import RetinatServer

	monkeypatch.delenv('BROWSER_USE_PROXY_CA_CERT', raising=False)
	monkeypatch.setenv('BROWSER_USE_EYES_NOW', str(tmp_path / 'now.json'))
	monkeypatch.setenv('SSL_CERT_FILE', str(a_certificate))
	server = RetinatServer()
	server.config.setdefault('browser_profile', {}).update({'headless': True, 'user_data_dir': str(tmp_path / 'profile')})
	handler = server.server.get_request_handler('tools/call')
	assert handler is not None
	try:
		url = https_site.url_for('/').replace('http://', 'https://')
		opened = await handler.handler(None, types.CallToolRequestParams(name='eye_open', arguments={'url': url}))  # type: ignore[arg-type]
		assert isinstance(opened, types.CallToolResult)
		said = ' '.join(b.text for b in opened.content if isinstance(b, types.TextContent))
		assert not opened.is_error and 'proxy CA' in said, said
		assert server.browser_session is not None
		cdp = await server.browser_session.get_or_create_cdp_session(focus=False)
		body = await cdp.cdp_client.send.Runtime.evaluate(
			params={'expression': 'document.body.innerText', 'returnByValue': True}, session_id=cdp.session_id
		)
		assert body['result']['value'] == 'behind the proxy'
	finally:
		await server._close_all_sessions()
