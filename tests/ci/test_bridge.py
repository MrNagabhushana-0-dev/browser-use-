"""The extension bridge: an AI drives a plain, person-launched Chromium through the Retinat bridge extension.

The browser here is started the way a person starts theirs - headful, no --remote-debugging-port, no
--enable-automation - with only the extension added. Everything the AI does goes extension -> relay -> CDP client.
(--load-extension stands in for the person's "Load unpacked"; branded Chrome 137+ ignores the flag, Chromium keeps it.)
"""

import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path

import aiohttp
import httpx
import pytest
from pytest_httpserver import HTTPServer

from browser_use.bridge import EXTENSION_ID, BridgeRelay, bridge_session_kwargs, write_extension
from browser_use.browser import BrowserSession
from browser_use.browser.profile import BrowserProfile
from browser_use.browser.watchdogs.local_browser_watchdog import LocalBrowserWatchdog
from browser_use.human.input import HumanInput
from browser_use.mcp.server import BrowserUseServer, types
from browser_use.retinat.server import RetinatServer

pytestmark = pytest.mark.skipif(not shutil.which('Xvfb'), reason='Xvfb not installed')

SHARED = (
	'<!doctype html><title>Shared page</title><body style="margin:0;height:100vh">'
	'<button id="b" style="position:fixed;left:40px;top:40px;width:220px;height:90px">Press</button>'
	"<script>window.clicks = []; addEventListener('click', e => clicks.push(e.isTrusted))</script></body>"
)

LOGIN = (
	'<!doctype html><title>Sign in</title><body style="margin:0">'
	'<input id="p" type="password" style="position:fixed;left:40px;top:40px;width:240px;height:40px">'
	'<p style="position:fixed;top:120px;left:40px">Sign in to continue</p></body>'
)


@pytest.fixture(scope='module')
def display():
	for n in range(91, 99):
		if not os.path.exists(f'/tmp/.X11-unix/X{n}') and not os.path.exists(f'/tmp/.X{n}-lock'):
			break
	proc = subprocess.Popen(
		['Xvfb', f':{n}', '-screen', '0', '1280x900x24'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
	)
	for _ in range(50):
		if os.path.exists(f'/tmp/.X11-unix/X{n}'):
			break
		subprocess.run(['sleep', '0.1'])
	yield f':{n}'
	proc.terminate()
	proc.wait(timeout=10)


@pytest.fixture(scope='module')
def site():
	server = HTTPServer()
	server.start()
	server.expect_request('/shared').respond_with_data(SHARED, content_type='text/html')
	server.expect_request('/shared/next').respond_with_data('<title>Next page</title>next', content_type='text/html')
	server.expect_request('/shared/login').respond_with_data(LOGIN, content_type='text/html')
	server.expect_request('/private').respond_with_data('<title>Private page</title>mail', content_type='text/html')
	yield server
	server.clear()
	if server.is_running():
		server.stop()


@pytest.fixture(scope='module')
async def bridge(display, site, tmp_path_factory):
	"""A relay plus a person's browser that shares /shared* (by the always-share setting) and not /private."""
	relay = await BridgeRelay(port=0).start()
	ext = write_extension(
		tmp_path_factory.mktemp('ext') / 'retinat-bridge',
		relay=f'ws://127.0.0.1:{relay.port}/extension',
		always_share=[site.url_for('/shared') + '*'],
	)
	chrome = LocalBrowserWatchdog._find_installed_browser_path()
	assert chrome, 'no Chromium found'
	args = [
		chrome,
		f'--user-data-dir={tmp_path_factory.mktemp("profile")}',
		f'--load-extension={ext}',
		f'--disable-extensions-except={ext}',
		'--no-first-run',
		'--no-default-browser-check',
		site.url_for('/private'),
		site.url_for('/shared'),
	]
	if os.geteuid() == 0:
		args.insert(1, '--no-sandbox')  # Chromium refuses to run as root otherwise; a person's browser does not run as root
	proc = _launch(args, display)
	try:
		await relay.wait_for_extension(timeout=30)
		await relay.wait_for_tab(timeout=30)
		yield relay, proc
	finally:
		await relay.stop()
		proc.terminate()
		try:
			proc.wait(timeout=10)
		except subprocess.TimeoutExpired:
			proc.kill()


async def get(relay: BridgeRelay, path: str, **headers: str) -> httpx.Response:
	async with httpx.AsyncClient(trust_env=False) as http:
		return await http.get(relay.cdp_url + path, headers=headers)


def _launch(args: list[str], display: str) -> subprocess.Popen:
	return subprocess.Popen(args, env={**os.environ, 'DISPLAY': display}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


async def until(predicate, timeout: float = 10.0) -> None:
	for _ in range(int(timeout / 0.1)):
		if await predicate():
			return
		await asyncio.sleep(0.1)
	raise TimeoutError(f'still waiting after {timeout}s')


class RawCDP:
	"""Minimal CDP client for poking at the relay directly."""

	def __init__(self, ws: aiohttp.ClientWebSocketResponse):
		self.ws = ws
		self.n = 0
		self.events: list[dict] = []

	async def call(self, method: str, params: dict | None = None, session_id: str | None = None) -> dict:
		self.n += 1
		msg: dict = {'id': self.n, 'method': method, 'params': params or {}}
		if session_id:
			msg['sessionId'] = session_id
		await self.ws.send_str(json.dumps(msg))
		async with asyncio.timeout(20):
			while True:
				reply = json.loads((await self.ws.receive()).data)
				if reply.get('id') == self.n:
					return reply
				self.events.append(reply)


async def raw_cdp(relay: BridgeRelay):
	version = (await get(relay, '/json/version')).json()
	http = aiohttp.ClientSession()
	return http, RawCDP(await http.ws_connect(version['webSocketDebuggerUrl'], max_msg_size=0))


async def test_only_shared_tabs_are_visible(bridge, site):
	relay, _ = bridge
	listed = (await get(relay, '/json/list')).json()
	assert [t['url'] for t in listed] == [site.url_for('/shared')]
	version = (await get(relay, '/json/version')).json()
	assert version['Browser'].startswith('Chrome/') and version['webSocketDebuggerUrl'].startswith('ws://127.0.0.1:')

	http, cdp = await raw_cdp(relay)
	try:
		infos = (await cdp.call('Target.getTargets'))['result']['targetInfos']
		assert [i['title'] for i in infos] == ['Shared page']
		refused = await cdp.call('Target.attachToTarget', {'targetId': 'F' * 32, 'flatten': True})
		assert 'only tabs the person shared' in refused['error']['message']
	finally:
		await http.close()


async def test_web_pages_and_other_extensions_cannot_use_the_relay(bridge):
	relay, _ = bridge
	assert (await get(relay, '/json/version', Origin='https://evil.test')).status_code == 403
	assert (await get(relay, '/json/version', Host='evil.test')).status_code == 403
	async with aiohttp.ClientSession() as http:
		with pytest.raises(aiohttp.WSServerHandshakeError):
			await http.ws_connect(f'ws://127.0.0.1:{relay.port}/extension', origin='chrome-extension://' + 'a' * 32)
		with pytest.raises(aiohttp.WSServerHandshakeError):
			await http.ws_connect(f'ws://127.0.0.1:{relay.port}/cdp/not-the-token')
	assert EXTENSION_ID == 'lcdhfliibkimhbimdfhogcmjedlkoemg'


async def test_browser_session_drives_the_shared_tab_like_a_person(bridge, site):
	relay, proc = bridge
	cmdline = Path(f'/proc/{proc.pid}/cmdline').read_bytes().decode().split('\0')
	assert not any(a.startswith(('--remote-debugging', '--enable-automation', '--headless')) for a in cmdline)

	session = BrowserSession(browser_profile=BrowserProfile(**bridge_session_kwargs(relay.cdp_url)))
	await session.start()
	try:
		assert site.url_for('/shared') in await session.get_current_page_url()
		cdp = await session.get_or_create_cdp_session(focus=True)

		async def js(expr: str):
			r = await cdp.cdp_client.send.Runtime.evaluate(
				params={'expression': expr, 'returnByValue': True}, session_id=cdp.session_id
			)
			return r['result'].get('value')

		assert await js('navigator.webdriver') is False
		await HumanInput(session, seed=1).click(150, 85)
		assert await js('clicks') == [True]  # trusted, exactly like a person's click

		await cdp.cdp_client.send.Page.navigate(params={'url': site.url_for('/shared/next')}, session_id=cdp.session_id)

		async def arrived():
			return await js('document.title') == 'Next page'

		await until(arrived)
	finally:
		await session.stop()
	assert proc.poll() is None, "stopping the AI's session must never close the person's browser"


async def test_relay_refuses_what_a_person_cannot_do(bridge):
	relay, _ = bridge
	http, cdp = await raw_cdp(relay)
	try:
		target = (await cdp.call('Target.getTargets'))['result']['targetInfos'][0]['targetId']
		sid = (await cdp.call('Target.attachToTarget', {'targetId': target, 'flatten': True}))['result']['sessionId']
		for method, params, session in [
			('Network.setUserAgentOverride', {'userAgent': 'x'}, sid),
			('Emulation.setGeolocationOverride', {'latitude': 1, 'longitude': 1, 'accuracy': 1}, sid),
			('Fetch.enable', {}, sid),
			('Network.setCookie', {'name': 'a', 'value': 'b', 'url': 'http://127.0.0.1/'}, sid),
			('Browser.close', {}, None),
			('Storage.clearCookies', {}, None),
		]:
			reply = await cdp.call(method, params, session)
			assert 'refused through the extension bridge' in reply['error']['message'], method
		ok = await cdp.call('Runtime.evaluate', {'expression': '6*7', 'returnByValue': True}, sid)
		assert ok['result']['result']['value'] == 42

		relay.set_holder('human')
		held = await cdp.call('Input.dispatchMouseEvent', {'type': 'mouseMoved', 'x': 5, 'y': 5}, sid)
		assert 'the person is driving' in held['error']['message']
		looked = await cdp.call('Runtime.evaluate', {'expression': 'document.title', 'returnByValue': True}, sid)
		assert 'result' in looked, 'looking stays allowed while the person drives'
	finally:
		relay.set_holder('agent')
		await http.close()


async def test_ai_opens_and_closes_its_own_tab(bridge, site):
	relay, _ = bridge
	http, cdp = await raw_cdp(relay)
	try:
		await cdp.call('Target.setDiscoverTargets', {'discover': True})
		before = len(relay.tabs)
		created = await cdp.call('Target.createTarget', {'url': site.url_for('/shared/next')})
		target_id = created['result']['targetId']
		titles = {i['targetId'] for i in (await cdp.call('Target.getTargets'))['result']['targetInfos']}
		assert target_id in titles and len(relay.tabs) == before + 1
		assert (await cdp.call('Target.closeTarget', {'targetId': target_id}))['result'] == {'success': True}

		async def closed():
			return len(relay.tabs) == before

		await until(closed)
	finally:
		await http.close()


def test_manifest_v2_variant_for_old_chromium(tmp_path):
	out = write_extension(tmp_path / 'mv2', relay='ws://127.0.0.1:9444/extension', manifest_version=2)
	manifest = json.loads((out / 'manifest.json').read_text())
	assert manifest['manifest_version'] == 2 and manifest['background'] == {'scripts': ['worker.js'], 'persistent': True}
	assert 'browser_action' in manifest and 'action' not in manifest
	assert json.loads((out / 'settings.json').read_text())['relay'] == 'ws://127.0.0.1:9444/extension'


async def _call(server: BrowserUseServer, name: str, arguments: dict) -> str:
	handler = server.server.get_request_handler('tools/call')
	assert handler is not None
	result = await handler.handler(None, types.CallToolRequestParams(name=name, arguments=arguments))  # type: ignore[arg-type]
	assert isinstance(result, types.CallToolResult)
	return '\n'.join(b.text for b in result.content if isinstance(b, types.TextContent))


async def test_retinat_mcp_works_in_the_persons_browser_but_leaves_passwords_to_them(bridge, site, tmp_path, monkeypatch):
	relay, proc = bridge
	monkeypatch.setenv('BROWSER_USE_EYES_NOW', str(tmp_path / 'now.json'))
	server = RetinatServer(bridge=relay)
	try:
		assert 'Sign in' in await _call(server, 'retinat_open', {'url': site.url_for('/shared/login')})
		assert 'Sign in to continue' in await _call(server, 'retinat_find', {'text': 'Sign in to continue'})
		await _call(server, 'retinat_click', {'x': 160, 'y': 60})
		refused = await _call(server, 'retinat_type', {'text': 'hunter2'})
		assert 'they enter those themselves' in refused
		assert server.browser_session is not None
		cdp = await server.browser_session.get_or_create_cdp_session(focus=False)
		value = await cdp.cdp_client.send.Runtime.evaluate(
			params={'expression': 'document.getElementById("p").value', 'returnByValue': True}, session_id=cdp.session_id
		)
		assert value['result'].get('value') == ''
	finally:
		await server._close_all_sessions()
	assert proc.poll() is None


async def test_browser_use_mcp_works_in_the_persons_browser_too(bridge, site):
	relay, proc = bridge
	server = BrowserUseServer()
	server.bridge, server.cdp_url = relay, relay.cdp_url
	try:
		await _call(server, 'browser_navigate', {'url': site.url_for('/shared/login')})
		state = json.loads(await _call(server, 'browser_get_state', {}))
		assert state['title'] == 'Sign in'
		field = next(e['index'] for e in state['interactive_elements'] if e['tag'] == 'input')
		assert 'they enter those themselves' in await _call(server, 'browser_type', {'index': field, 'text': 'hunter2'})
	finally:
		await server._close_all_sessions()
	assert proc.poll() is None
