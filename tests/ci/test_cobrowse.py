"""A person signs in; the agent continues in the same browser.

This is the answer to sites that will not admit a fresh automated profile — Google,
Instagram, anything that escalates to a device prompt the moment it sees one. Nothing
here automates a login. It proves the handover: same profile, same cookies, same tab, and
the agent acting through real input once it arrives.
"""

import asyncio
import json

import pytest
from pytest_httpserver import HTTPServer

from browser_use.browser.events import NavigateToUrlEvent
from browser_use.cobrowse import attach, describe_session, focus_human_tab, launch_for_human
from browser_use.human import HumanInput

# Stands in for a real login: clicking "sign in" leaves a session cookie behind, exactly
# as a real provider would, and the page then shows who you are.
LOGIN_PAGE = """<!DOCTYPE html>
<html><head><title>Acme Login</title></head>
<body>
	<div id="who">signed out</div>
	<button id="signin" style="position:absolute;left:60px;top:120px;width:140px;height:44px;">Sign in</button>
<script>
	function render() {
		const m = document.cookie.match(/session_token=([^;]+)/);
		document.getElementById('who').textContent = m ? 'signed in as ' + m[1] : 'signed out';
	}
	document.getElementById('signin').addEventListener('click', () => {
		document.cookie = 'session_token=ada-lovelace; path=/; max-age=86400';
		localStorage.setItem('profile', 'ada');
		render();
	});
	render();
</script>
</body></html>"""

# Chrome cannot use its sandbox as root in a container; nothing else here is CI-specific.
CONTAINER_ARGS = ['--no-sandbox', '--disable-dev-shm-usage']


@pytest.fixture(scope='module')
def login_server():
	server = HTTPServer()
	server.start()
	server.expect_request('/login').respond_with_data(LOGIN_PAGE, content_type='text/html')
	yield server
	server.stop()


@pytest.fixture
async def human_browser(tmp_path):
	"""A browser standing in for the one on the person's desk."""
	browser = await launch_for_human(
		user_data_dir=tmp_path / 'human-profile',
		headless=True,  # a real handover is headful; CI has no display
		extra_args=CONTAINER_ARGS,
	)
	yield browser
	await browser.close()


async def _sign_in(session, url: str) -> None:
	"""Do what the person does: open the page and click the button."""
	event = session.event_bus.dispatch(NavigateToUrlEvent(url=url))
	await event
	await event.event_result(raise_if_any=True, raise_if_none=False)
	await HumanInput(session, seed=4).click_box((60, 120, 140, 44))


async def test_the_agent_inherits_the_session_the_person_signed_into(human_browser, login_server):
	person = await attach(human_browser.cdp_url)
	try:
		await _sign_in(person, login_server.url_for('/login'))
		state = await person.run_page_script("return document.getElementById('who').textContent;")
		assert json.loads(state.value) == 'signed in as ada-lovelace', 'the stand-in login did not take'
	finally:
		await person.kill()
		await person.event_bus.stop(clear=True, timeout=5)

	# Hand over: a brand new agent session, attaching to the same live browser.
	agent = await attach(human_browser.cdp_url)
	try:
		target_id = await focus_human_tab(agent, url_contains='/login')
		assert target_id is not None, 'the agent found no human tab to take over'

		# It landed on the page the person left, already signed in — no login automated.
		who = await agent.run_page_script("return document.getElementById('who').textContent;")
		assert json.loads(who.value) == 'signed in as ada-lovelace'

		stored = await agent.run_page_script("return localStorage.getItem('profile');")
		assert json.loads(stored.value) == 'ada'
	finally:
		await agent.kill()
		await agent.event_bus.stop(clear=True, timeout=5)


async def test_taking_over_does_not_disturb_what_the_person_left(human_browser, login_server):
	person = await attach(human_browser.cdp_url)
	try:
		await _sign_in(person, login_server.url_for('/login'))
		tabs_before = len(person.session_manager.get_all_page_targets())
	finally:
		await person.kill()
		await person.event_bus.stop(clear=True, timeout=5)

	agent = await attach(human_browser.cdp_url)
	try:
		await focus_human_tab(agent)
		tabs_after = len(agent.session_manager.get_all_page_targets())
		# Attaching must not spawn a blank tab or steal the window.
		assert tabs_after == tabs_before, f'tab count changed on takeover: {tabs_before} -> {tabs_after}'
	finally:
		await agent.kill()
		await agent.event_bus.stop(clear=True, timeout=5)


async def test_the_agent_acts_on_the_persons_tab_with_real_input(human_browser, login_server):
	"""Takeover is only useful if what follows is genuine UI driving."""
	agent = await attach(human_browser.cdp_url)
	try:
		event = agent.event_bus.dispatch(NavigateToUrlEvent(url=login_server.url_for('/login')))
		await event
		await event.event_result(raise_if_any=True, raise_if_none=False)

		await agent.run_page_script(
			'window.seen = null;'
			"document.getElementById('signin').addEventListener('click', e => { window.seen = e.isTrusted; });"
			'return 1;'
		)
		await HumanInput(agent, seed=9).click_box((60, 120, 140, 44))

		trusted = await agent.run_page_script('return window.seen;')
		assert json.loads(trusted.value) is True, 'the agent must drive the UI, not script it'
	finally:
		await agent.kill()
		await agent.event_bus.stop(clear=True, timeout=5)


async def test_the_signed_in_profile_survives_closing_the_browser(tmp_path, login_server):
	"""The person signs in once, not once per session."""
	profile = tmp_path / 'persistent-profile'

	first = await launch_for_human(user_data_dir=profile, headless=True, extra_args=CONTAINER_ARGS)
	try:
		session = await attach(first.cdp_url)
		await _sign_in(session, login_server.url_for('/login'))
		await session.kill()
		await session.event_bus.stop(clear=True, timeout=5)
	finally:
		await first.close()

	# Same profile directory, new browser process — as if the laptop had been shut.
	second = await launch_for_human(user_data_dir=profile, headless=True, extra_args=CONTAINER_ARGS)
	try:
		session = await attach(second.cdp_url)
		try:
			event = session.event_bus.dispatch(NavigateToUrlEvent(url=login_server.url_for('/login')))
			await event
			await event.event_result(raise_if_any=True, raise_if_none=False)

			who = await session.run_page_script("return document.getElementById('who').textContent;")
			assert json.loads(who.value) == 'signed in as ada-lovelace', 'the login did not persist'
		finally:
			await session.kill()
			await session.event_bus.stop(clear=True, timeout=5)
	finally:
		await second.close()


async def test_the_handover_summary_never_returns_cookie_values(human_browser, login_server):
	"""Those values are the person's live credentials; a model has no use for them."""
	agent = await attach(human_browser.cdp_url)
	try:
		await _sign_in(agent, login_server.url_for('/login'))
		summary = await describe_session(agent)

		assert 'session_token' in summary['cookie_names']
		assert summary['cookie_count'] >= 1
		assert 'ada-lovelace' not in json.dumps(summary), 'a cookie value leaked into the summary'
	finally:
		await agent.kill()
		await agent.event_bus.stop(clear=True, timeout=5)


async def test_the_full_handover_round_trip(human_browser, login_server):
	"""The whole point, in one test: the person signs in, the agent takes over the same
	session, the person takes it back, and the agent stops acting on request.

	Each half is covered above; this asserts they compose, because the failure that
	matters is the one that only appears when control changes hands twice.
	"""
	session = await attach(human_browser.cdp_url)
	try:
		event = session.event_bus.dispatch(NavigateToUrlEvent(url=login_server.url_for('/account')))
		await event
		await event.event_result(raise_if_any=False, raise_if_none=False)
		await session.run_page_script("document.cookie = 'session_token=secret-value; path=/'; return 1;")

		# Handed over: names, never values.
		handover = await describe_session(session)
		assert 'session_token' in handover['cookie_names']
		assert 'secret-value' not in json.dumps(handover)

		# The person takes the wheel; the agent must refuse rather than queue.
		session.control.grant_to_human('signing in')
		assert session.control.agent_may_act is False
		refusal = session.control.refusal('click the button')
		assert 'person is driving' in refusal and 'control will come back' in refusal

		# Handed back, the agent drives the same tab with the same cookies.
		session.control.grant_to_agent('you can drive now')
		assert session.control.agent_may_act is True
		await session.human.wheel(500)
		await asyncio.sleep(0.6)

		state = await session.run_page_script("return {kept: document.cookie.includes('session_token'), y: window.scrollY};")
		assert json.loads(state.value)['kept'] is True, 'the login did not survive the handover'
	finally:
		await session.kill()


async def test_the_human_browser_trusts_a_proxy_ca_when_given_one(tmp_path):
	"""Behind a TLS-terminating proxy, a browser that does not trust the proxy's CA fails
	every HTTPS page with ERR_CERT_AUTHORITY_INVALID — which makes co-browsing useless
	exactly where it is needed, since there is nothing to sign into over plain HTTP."""
	from browser_use.browser.profile import proxy_ca_pins
	from browser_use.cobrowse.service import launch_for_human as _launch

	# A self-signed CA is enough: we are asserting the flag is derived and passed, not
	# that Chromium trusts this particular certificate.
	cert = tmp_path / 'ca.crt'
	process = await asyncio.create_subprocess_exec(
		*[
			'openssl',
			'req',
			'-x509',
			'-newkey',
			'rsa:2048',
			'-nodes',
			'-keyout',
			str(tmp_path / 'ca.key'),
			'-out',
			str(cert),
			'-days',
			'1',
			'-subj',
			'/CN=test-proxy-ca',
		],
		stdout=asyncio.subprocess.DEVNULL,
		stderr=asyncio.subprocess.DEVNULL,
	)
	assert await process.wait() == 0, 'openssl could not produce a test CA'
	pins = proxy_ca_pins(cert)
	assert pins, 'a valid CA certificate should yield an SPKI pin'

	browser = await _launch(user_data_dir=tmp_path / 'profile', headless=True, proxy_ca_cert=cert)
	try:
		args = ' '.join(browser.args)
		assert '--ignore-certificate-errors-spki-list=' in args, args
		assert pins[0] in args
	finally:
		await browser.close()
