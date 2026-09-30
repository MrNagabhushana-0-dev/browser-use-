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


def test_no_certificate_means_no_flag():
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


async def test_an_untrusted_proxy_ca_fails_with_the_fix_in_the_message(https_site, monkeypatch, a_certificate):
	"""curl and Python trust the proxy through SSL_CERT_FILE; Chromium does not, and says only
	ERR_CERT_AUTHORITY_INVALID. The error must name the setting that fixes it."""
	from browser_use.browser import BrowserSession

	monkeypatch.delenv('BROWSER_USE_PROXY_CA_CERT', raising=False)
	monkeypatch.setenv('SSL_CERT_FILE', str(a_certificate))
	session = BrowserSession(browser_profile=BrowserProfile(headless=True, user_data_dir=None, keep_alive=False))
	await session.start()
	try:
		with pytest.raises(Exception) as failure:
			await session.navigate_to(https_site.url_for('/').replace('http://', 'https://'))
		message = str(failure.value)
		assert (
			'ERR_CERT_AUTHORITY_INVALID' in message and 'BROWSER_USE_PROXY_CA_CERT' in message and 'SSL_CERT_FILE' in message
		), message
	finally:
		await session.kill()


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
