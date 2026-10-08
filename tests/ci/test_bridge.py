"""The extension bridge: an AI drives a plain, person-launched Chromium through the Retinat bridge extension.

The browser here is started the way a person starts theirs - headful, no --remote-debugging-port, no
--enable-automation - with only the extension added. Everything the AI does goes extension -> relay -> CDP client.
(--load-extension stands in for the person's "Load unpacked"; branded Chrome 137+ ignores the flag, Chromium keeps it.)
"""

import asyncio
import ctypes
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import aiohttp
import httpx
import pytest
from pytest_httpserver import HTTPServer

from browser_use.bridge import EXTENSION_ID, BridgeError, BridgeRelay, bridge_session_kwargs, write_extension
from browser_use.bridge.relay import EXPECTED_VERSION
from browser_use.browser import BrowserSession
from browser_use.browser.profile import BrowserProfile
from browser_use.browser.watchdogs.local_browser_watchdog import LocalBrowserWatchdog
from browser_use.human.input import HumanInput
from browser_use.mcp.server import BrowserUseServer, types
from browser_use.retinat.server import RetinatServer

pytestmark = pytest.mark.skipif(not shutil.which('Xvfb'), reason='Xvfb not installed')

SHARED = (
	'<!doctype html><title>Shared page</title><body style="margin:0;height:100vh">'
	'<div style="position:fixed;left:0;top:0;width:6px;height:6px;background:#f0f"></div>'  # where the page starts
	'<button id="b" style="position:fixed;left:40px;top:40px;width:220px;height:90px">Press</button>'
	"<script>window.clicks = []; addEventListener('click', e => clicks.push(e.isTrusted))</script></body>"
)

OPENER = (
	'<!doctype html><title>Opener</title><body style="margin:0;height:100vh">'
	'<div style="position:fixed;left:0;top:0;width:6px;height:6px;background:#f0f"></div>'
	'<a id="ai" target="_blank" href="/opened/ai" style="position:fixed;left:40px;top:40px;width:220px;height:60px;'
	'display:block;background:#ddd">The AI opens this</a>'
	'<a id="me" target="_blank" href="/opened/me" style="position:fixed;left:40px;top:160px;width:220px;height:60px;'
	'display:block;background:#ddd">I open this</a></body>'
)
LOGIN = (
	'<!doctype html><title>Sign in</title><body style="margin:0">'
	'<input id="p" type="password" style="position:fixed;left:40px;top:40px;width:240px;height:40px">'
	'<p style="position:fixed;top:120px;left:40px">Sign in to continue</p></body>'
)


def _stop(proc: subprocess.Popen) -> None:
	"""End a browser or X server the test started: politely, then for good (Chromium can take over 10 s to exit)."""
	proc.terminate()
	try:
		proc.wait(timeout=10)
	except subprocess.TimeoutExpired:
		proc.kill()
		proc.wait(timeout=10)


def _xvfb():
	for n in range(91, 120):
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
	_stop(proc)


@pytest.fixture(scope='module')
def display():
	yield from _xvfb()


@pytest.fixture
def own_display():
	"""A screen with nothing else on it, for tests that click the browser's own UI."""
	yield from _xvfb()


@pytest.fixture(scope='module')
def site():
	server = HTTPServer()
	server.start()
	server.expect_request('/shared').respond_with_data(
		SHARED, content_type='text/html', headers={'Set-Cookie': 'shared_pref=1; Path=/'}
	)
	server.expect_request('/shared/next').respond_with_data('<title>Next page</title>next', content_type='text/html')
	server.expect_request('/shared/login').respond_with_data(LOGIN, content_type='text/html')
	server.expect_request('/shared/opener').respond_with_data(OPENER, content_type='text/html')
	for who in ('ai', 'me'):
		server.expect_request(f'/opened/{who}').respond_with_data(
			f'<!doctype html><title>Opened by {who}</title><body style="margin:0;height:100vh;background:#fff">',
			content_type='text/html',
		)
	yield server
	server.clear()
	if server.is_running():
		server.stop()


@pytest.fixture(scope='module')
def mail():
	"""Another site the person is signed in to, never shared with the AI. On another host than `site` (localhost):
	cookies are kept per host, not per port."""
	server = HTTPServer(host='127.0.0.1')
	server.start()
	server.expect_request('/private').respond_with_data(
		'<title>Private page</title>mail<script>'  # what a mail client keeps on the device
		"localStorage.mail_token = 'l0cal-t0k3n';"
		"caches.open('mail').then(c => c.put('/inbox', new Response('cached-inb0x')));"
		"const o = indexedDB.open('mail', 1); o.onupgradeneeded = () => o.result.createObjectStore('msgs');"
		"o.onsuccess = () => o.result.transaction('msgs', 'readwrite').objectStore('msgs').put('idb-m3ssage', 1);"
		'</script>',
		content_type='text/html',
		headers={'Set-Cookie': 'mail_session=s3cr3t; Path=/; HttpOnly'},
	)
	server.expect_request('/pixel').respond_with_data(  # a tracking pixel that also refreshes the session
		b'GIF89a\x01\x00\x01\x00\x00\x00\x00;',
		content_type='image/gif',
		headers={'Set-Cookie': 'mail_refresh=r3fr3sh-t0k3n; Path=/; HttpOnly'},
	)
	yield server
	server.clear()
	if server.is_running():
		server.stop()


@pytest.fixture(scope='module')
async def bridge(display, site, mail, tmp_path_factory):
	"""A relay plus a person's browser that shares /shared* (by the always-share setting) and not the mail tab."""
	relay = await BridgeRelay(port=0).start()
	proc = _person_browser(
		tmp_path_factory.mktemp('person'),
		relay,
		display,
		[mail.url_for('/private'), site.url_for('/shared')],
		always_share=[site.url_for('/shared') + '*'],
		resume_after_ms=1500,
	)
	try:
		await relay.wait_for_extension(timeout=30)
		await relay.wait_for_tab(timeout=30)
		yield relay, proc
	finally:
		await relay.stop()
		_stop(proc)


async def get(relay: BridgeRelay, path: str, **headers: str) -> httpx.Response:
	async with httpx.AsyncClient(trust_env=False) as http:
		return await http.get(relay.cdp_url + path, headers=headers)


def x_click(display: str, x: int, y: int) -> None:
	"""A real pointer click on the X display, as the person's mouse makes it (XTest), not through CDP."""
	xlib, xtst = ctypes.CDLL('libX11.so.6'), ctypes.CDLL('libXtst.so.6')
	xlib.XOpenDisplay.restype = ctypes.c_void_p
	xlib.XOpenDisplay.argtypes = [ctypes.c_char_p]
	xlib.XFlush.argtypes = xlib.XCloseDisplay.argtypes = [ctypes.c_void_p]
	xtst.XTestFakeMotionEvent.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_ulong]
	xtst.XTestFakeButtonEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_int, ctypes.c_ulong]
	dpy = xlib.XOpenDisplay(display.encode())
	assert dpy, f'cannot open {display}'
	xtst.XTestFakeMotionEvent(dpy, -1, x, y, 0)
	xtst.XTestFakeButtonEvent(dpy, 1, 1, 0)
	xtst.XTestFakeButtonEvent(dpy, 1, 0, 0)
	xlib.XFlush(dpy)
	xlib.XCloseDisplay(dpy)


def _person_browser(tmp: Path, relay: BridgeRelay, display: str, urls: list[str], **settings) -> subprocess.Popen:
	"""Start a browser the way a person does, with the bridge extension in it and nothing else added.

	By default this is the Chromium found here, with the extension added by --load-extension (standing in for Load
	unpacked). BRIDGE_TEST_BROWSER picks another binary, e.g. Edge. Branded Chrome 137+ ignores --load-extension:
	for it, set BRIDGE_TEST_PROFILE to a profile where the extension was added with Load unpacked from the folder
	BRIDGE_TEST_EXTENSION. That folder's settings are rewritten for this run and the profile is copied, so run the
	idle test (its own browser) in a separate pytest invocation then. BRIDGE_TEST_PROFILE alone is a profile past
	first run (Vivaldi shows only its welcome page on a fresh one): copied, with the extension added as usual.
	"""
	relay_url = f'ws://127.0.0.1:{relay.port}/extension'
	binary = os.environ.get('BRIDGE_TEST_BROWSER') or LocalBrowserWatchdog._find_installed_browser_path()
	assert binary, 'no Chromium found'
	profile = tmp / 'profile'
	if template := os.environ.get('BRIDGE_TEST_PROFILE'):
		shutil.copytree(template, profile, ignore=shutil.ignore_patterns('Singleton*'))
	if loaded := os.environ.get('BRIDGE_TEST_EXTENSION'):
		write_extension(Path(loaded), relay=relay_url, **settings)
		args = [binary, f'--user-data-dir={profile}']
	else:
		ext = write_extension(tmp / 'ext', relay=relay_url, **settings)
		args = [binary, f'--user-data-dir={profile}', f'--load-extension={ext}', f'--disable-extensions-except={ext}']
	# A window that fits the 1280x900 test screen, as a person's does (the default can run past its bottom edge).
	args += ['--no-first-run', '--no-default-browser-check', '--window-position=0,0', '--window-size=1200,860', *urls]
	if os.geteuid() == 0:
		# Chromium refuses to run as root without --no-sandbox (a person's browser doesn't run as root); --test-type
		# drops the warning bar that flag adds, which would otherwise queue the debugging bar behind it.
		args[1:1] = ['--no-sandbox', '--test-type']
	return _launch(args, display)


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
	assert re.match(r'^(Chrome|Edge|Opera|Vivaldi)/\d+\.', version['Browser']), version['Browser']
	assert version['webSocketDebuggerUrl'].startswith('ws://127.0.0.1:')

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
	# Another extension is not a CDP client either: only the pinned one may talk to the relay, and on its own channel.
	other = 'chrome-extension://' + 'b' * 32
	assert (await get(relay, '/json/version', Origin=other)).status_code == 403
	ws_url = (await get(relay, '/json/version')).json()['webSocketDebuggerUrl']
	async with aiohttp.ClientSession() as http:
		with pytest.raises(aiohttp.WSServerHandshakeError):
			await http.ws_connect(f'ws://127.0.0.1:{relay.port}/extension', origin='chrome-extension://' + 'a' * 32)
		with pytest.raises(aiohttp.WSServerHandshakeError):
			await http.ws_connect(f'ws://127.0.0.1:{relay.port}/cdp/not-the-token')
		with pytest.raises(aiohttp.WSServerHandshakeError):
			await http.ws_connect(ws_url, origin=other)
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
		await asyncio.sleep(0.8)
		assert relay.holder == 'agent', "the AI's own click must not read as the person taking over"

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
			('DOM.setFileInputFiles', {'files': ['/etc/passwd'], 'nodeId': 1}, sid),  # files off the person's disk
			('Target.createTarget', {'url': 'about:blank'}, sid),  # a tab opened past the tab tools
		]:
			reply = await cdp.call(method, params, session)
			assert 'refused through the extension bridge' in reply['error']['message'], method
		ok = await cdp.call('Runtime.evaluate', {'expression': '6*7', 'returnByValue': True}, sid)
		assert ok['result']['result']['value'] == 42

		relay.set_holder('human')
		held = await cdp.call('Input.dispatchMouseEvent', {'type': 'mouseMoved', 'x': 5, 'y': 5}, sid)
		assert 'the person is using the browser' in held['error']['message']
		looked = await cdp.call('DOM.getDocument', {'depth': 1}, sid)
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


def test_one_policy_file_says_what_only_looks_for_both_the_relay_and_the_extension(tmp_path):
	from browser_use.bridge import EXTENSION_DIR
	from browser_use.bridge.policy import acts

	worker = (EXTENSION_DIR / 'worker.js').read_text()
	assert "getURL('policy.json')" in worker and 'ACTING' not in worker, 'the worker reads the shared list, keeps none of its own'
	assert (write_extension(tmp_path / 'ext') / 'policy.json').exists()
	assert not acts('DOM.getDocument') and not acts('Page.captureScreenshot') and not acts('Accessibility.getFullAXTree')
	for method in ('Runtime.evaluate', 'Runtime.callFunctionOn', 'Input.dispatchKeyEvent', 'Page.navigate', 'Made.upMethod'):
		assert acts(method), method


def test_manifest_v2_variant_for_old_chromium(tmp_path):
	out = write_extension(tmp_path / 'mv2', relay='ws://127.0.0.1:9444/extension', manifest_version=2)
	manifest = json.loads((out / 'manifest.json').read_text())
	assert manifest['manifest_version'] == 2 and manifest['background'] == {'scripts': ['worker.js'], 'persistent': True}
	assert 'browser_action' in manifest and 'action' not in manifest
	assert json.loads((out / 'settings.json').read_text())['relay'] == 'ws://127.0.0.1:9444/extension'


def _free_port() -> int:
	import socket

	with socket.socket() as sock:
		sock.bind(('127.0.0.1', 0))
		return sock.getsockname()[1]


async def test_doctor_names_what_is_missing_and_how_to_fix_it(bridge):
	"""After BrowserSkill's `bsk doctor`: one command that says which link of person -> extension -> relay -> AI is
	broken, with the fix in the person's words. Nothing running, a relay alone, then the real extension sharing a tab."""
	from browser_use.bridge.doctor import checks, diagnose

	nothing = {c.name: c for c in await diagnose(_free_port())}
	assert nothing['relay'].status == 'fail' and 'python -m browser_use.bridge' in nothing['relay'].fix
	assert {c.status for n, c in nothing.items() if n != 'relay'} == {'na'}

	lonely = await BridgeRelay(port=0).start()
	try:
		alone = {c.name: c for c in await diagnose(lonely.port)}
		said = await _call(RetinatServer(bridge=lonely), 'retinat_open', {'url': 'about:blank'})  # type: ignore[arg-type]
	finally:
		await lonely.stop()
	assert alone['relay'].status == 'ok'
	assert alone['extension'].status == 'fail' and 'Load unpacked' in alone['extension'].fix
	assert 'not connected' in said and 'Load unpacked' in said, f'the AI gets the fix to pass on: {said}'

	relay, _ = bridge
	live = {c.name: c for c in await diagnose(relay.port)}
	assert {n: c.status for n, c in live.items()} == {
		'relay': 'ok',
		'extension': 'ok',
		'browser': 'ok',
		'policy': 'ok',
		'tabs': 'ok',
		'wheel': 'ok',
	}, live
	assert 'Chrom' in live['browser'].detail and '1 shared' in live['tabs'].detail
	assert live['extension'].detail.startswith(f'Retinat bridge {EXPECTED_VERSION}') and 'answers in' in live['extension'].detail
	assert {c.name: c.status for c in checks(await relay.status())} == {n: c.status for n, c in live.items()}, (
		'in process and over HTTP agree'
	)
	assert (await get(relay, '/bridge/status', Origin='https://evil.test')).status_code == 403


def test_doctor_reads_old_browsers_a_held_wheel_and_a_stale_extension():
	"""The judgement part, on states that are slow or impossible to stage live (old Chromium, an outdated copy)."""
	from browser_use.bridge.doctor import checks
	from browser_use.bridge.policy import PASSIVE_GLOBS

	ua = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{v}.0.0.0 Safari/537.36'

	def status(**over):
		base = {
			'relay': 'retinat-bridge',
			'url': 'http://127.0.0.1:9333',
			'clients': 0,
			'expected_version': '0.1.0',
			'extension': {'version': '0.1.0', 'manifest': 3, 'userAgent': ua.format(v=155), 'answers_ms': 4.0},
			'policy': list(PASSIVE_GLOBS),
			'holder': 'agent',
			'stopped': False,
			'tabs': [{'title': 'Inbox', 'url': 'https://mail.test/'}],
		}
		return {c.name: c for c in checks({**base, **over})}

	assert status()['browser'].status == 'ok'
	old = status(extension={'version': '0.1.0', 'manifest': 3, 'userAgent': ua.format(v=120), 'answers_ms': 4.0})
	assert old['browser'].status == 'warn' and '125' in old['browser'].detail
	older = status(extension={'version': '0.1.0', 'manifest': 3, 'userAgent': ua.format(v=100), 'answers_ms': 4.0})
	assert older['browser'].status == 'warn' and 'idle' in older['browser'].detail
	stale = status(extension={'version': '0.0.9', 'manifest': 3, 'userAgent': ua.format(v=155), 'answers_ms': 4.0})
	assert stale['extension'].status == 'warn' and 'Reload' in stale['extension'].fix
	asleep = status(extension={'version': '0.1.0', 'manifest': 3, 'userAgent': ua.format(v=155), 'answers_ms': None})
	assert asleep['extension'].status == 'fail' and 'not answering' in asleep['extension'].detail
	assert status(policy=None)['policy'].status == 'warn', 'an extension too old to report its policy'
	assert status(policy=['DOM.*'])['policy'].status == 'fail', 'a copy that thinks more methods only look'
	held = status(holder='human')['wheel']
	assert held.status == 'warn' and 'Alt+Shift+Z' in held.fix
	stopped = status(stopped=True)['wheel']
	assert stopped.status == 'warn' and 'Cancel' in stopped.detail
	assert status(tabs=[])['tabs'].status == 'warn' and 'Alt+Shift+A' in status(tabs=[])['tabs'].fix
	vivaldi = status(
		extension={'version': '0.1.0', 'manifest': 3, 'userAgent': ua.format(v=155) + ' Vivaldi/8.2', 'answers_ms': 4.0}
	)
	assert vivaldi['browser'].status == 'ok' and 'pill' in vivaldi['browser'].detail


def test_doctor_command_exits_non_zero_when_a_link_is_broken():
	import sys

	port = _free_port()
	out = subprocess.run(
		[sys.executable, '-m', 'browser_use.bridge', 'doctor', '--port', str(port)], capture_output=True, text=True, timeout=60
	)
	assert out.returncode == 1, out
	assert 'relay' in out.stdout and f'127.0.0.1:{port}' in out.stdout and '→' in out.stdout, out.stdout
	as_json = subprocess.run(
		[sys.executable, '-m', 'browser_use.bridge', 'doctor', '--port', str(port), '--json'],
		capture_output=True,
		text=True,
		timeout=60,
	)
	assert json.loads(as_json.stdout)[0]['name'] == 'relay'


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
		relay.set_holder('human')
		try:
			held = await _call(server, 'retinat_click', {'x': 160, 'y': 60, 'expect': 'Sign in'})
		finally:
			relay.set_holder('agent')
		assert 'the person is using the browser' in held, f'a checked click under a hold gives the real reason: {held}'
		assert 'effect: none' in held, 'refused before anything was sent, so the AI knows a retry is safe'
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
		assert 'wheel' not in json.dumps(state).lower(), "the bridge's own sharing pill is not part of the page the AI reads"
		field = next(e['index'] for e in state['interactive_elements'] if e['tag'] == 'input')
		assert 'they enter those themselves' in await _call(server, 'browser_type', {'index': field, 'text': 'hunter2'})
	finally:
		await server._close_all_sessions()
	assert proc.poll() is None


async def test_the_ai_pauses_while_the_person_uses_a_shared_tab_and_resumes_after(bridge, display, site):
	relay, _ = bridge
	http, cdp = await raw_cdp(relay)
	try:
		target = (await cdp.call('Target.getTargets'))['result']['targetInfos'][0]['targetId']
		sid = (await cdp.call('Target.attachToTarget', {'targetId': target, 'flatten': True}))['result']['sessionId']
		await cdp.call('Page.navigate', {'url': site.url_for('/shared')}, sid)  # the page with the corner marker

		async def marked():
			r = await cdp.call('Runtime.evaluate', {'expression': 'document.title', 'returnByValue': True}, sid)
			return r.get('result', {}).get('result', {}).get('value') == 'Shared page'

		await until(marked)
		await cdp.call('Input.dispatchMouseEvent', {'type': 'mouseMoved', 'x': 1, 'y': 1}, sid)  # brings the tab forward
		await asyncio.sleep(0.8)  # past the window in which input counts as the AI's own
		assert relay.holder == 'agent'

		from PIL import ImageGrab

		x, y = _page_origin(ImageGrab.grab(xdisplay=display).convert('RGB'))
		x_click(display, x + 400, y + 300)  # in the page, away from its button

		async def paused():
			return relay.holder == 'human'

		await until(paused, timeout=5)
		held = await cdp.call('Input.dispatchMouseEvent', {'type': 'mouseMoved', 'x': 5, 'y': 5}, sid)
		assert 'the person is using the browser' in held['error']['message']

		async def resumed():
			return relay.holder == 'agent'

		await until(resumed, timeout=6)  # resume_after_ms=1500 in this fixture
		assert 'result' in await cdp.call('Input.dispatchMouseEvent', {'type': 'mouseMoved', 'x': 5, 'y': 5}, sid)
	finally:
		await http.close()


async def test_an_idle_extension_stays_connected_past_the_service_worker_timeout(display, tmp_path):
	"""MV3 stops an idle service worker after 30 s; the relay's pings must keep it (and its connection) alive.

	Its own browser with nothing shared: an attached debugger session would also keep the worker alive and hide this.
	"""
	relay = await BridgeRelay(port=0).start()
	proc = _person_browser(tmp_path, relay, display, ['about:blank'])
	try:
		await relay.wait_for_extension(timeout=30)
		first = relay._ext
		await asyncio.sleep(40)
		assert relay._ext is first and first is not None and not first.closed, 'the idle extension dropped its connection'
	finally:
		await relay.stop()
		_stop(proc)


def _page_origin(shot) -> tuple[int, int]:
	"""Screen position of the shared page's top-left corner, found by its magenta marker."""
	for y in range(min(shot.height, 400)):
		for x in range(0, shot.width, 2):
			r, g, b = shot.getpixel((x, y))  # type: ignore[misc]  # an RGB image gives a 3-tuple
			if r > 170 and b > 170 and g < 70 and abs(r - b) < 30:  # rendered #f0f comes out near (211, 14, 213)
				return x, y
	raise AssertionError('the shared page is not on screen')


def _find_cancel(display: str) -> tuple[int, int] | None:
	"""Where the button of the bar just above the page is: its longest solid run of colour, or None with no bar.

	The page's top-left corner is found on screen by its magenta marker, not from screenX/outerHeight: Brave
	farbles those against fingerprinting. Chrome draws Cancel blue and Edge near-black; the bar's text is ink too,
	but it breaks into short runs letter by letter, and the close (x) at the right end is left out.
	"""
	from PIL import ImageGrab

	shot = ImageGrab.grab(xdisplay=display).convert('RGB')
	left, top = _page_origin(shot)
	# The window ends where the bare X screen (pure black) begins; past it everything would read as one long run.
	right = next((x for x in range(left, shot.width) if shot.getpixel((x, top + 2)) == (0, 0, 0)), shot.width)

	def ink(x: int, y: int) -> bool:
		r, g, b = shot.getpixel((x, y))  # type: ignore[misc]  # an RGB image gives a 3-tuple
		return max(r, g, b) < 120 or b - r > 80

	band = range(max(0, top - 48), top - 4)
	best = (0, 0, 0)  # run length, x at its middle, y
	for y in band:
		run = 0
		for x in range(left, right - 60):
			run = run + 1 if ink(x, y) else 0
			# A button is filled: solid colour well down its middle too, unlike a link's 1 px underline.
			if run >= 30 and run > best[0] and sum(ink(x - run // 2, v) for v in band) >= 12:
				best = (run, x - run // 2, y)
	return (best[1], best[2]) if best[0] >= 30 else None


async def test_cancel_on_the_debugging_bar_stops_the_ai_until_the_person_shares_again(own_display, tmp_path, site):
	"""Cancel is the person's stop button: everything is unshared and the AI may not open a tab of its own instead."""
	relay = await BridgeRelay(port=0).start()
	display = own_display
	proc = _person_browser(tmp_path, relay, display, [site.url_for('/shared')], always_share=[site.url_for('/shared') + '*'])
	http = None
	try:
		await relay.wait_for_tab(timeout=30)
		http, cdp = await raw_cdp(relay)
		target = (await cdp.call('Target.getTargets'))['result']['targetInfos'][0]['targetId']
		sid = (await cdp.call('Target.attachToTarget', {'targetId': target, 'flatten': True}))['result']['sessionId']
		assert 'result' in await cdp.call('Runtime.evaluate', {'expression': '1'}, sid)  # attaches: the bar appears
		await asyncio.sleep(1)

		async def stopped():
			return relay.stopped and not relay.tabs

		# Bars queue: a browser's own notice (Brave's analytics bar) can stand in front of the debugging bar, as it
		# does for the person. Press the button of whichever bar is showing until the stop arrives.
		for attempt in range(3):
			button = _find_cancel(display)
			if button is None:
				if attempt == 0 and os.environ.get('BRIDGE_TEST_BROWSER'):
					pytest.skip('this browser shows no debugging bar (Vivaldi draws its own UI), so there is no Cancel')
				assert attempt, 'no debugging bar above the page'
				break
			x_click(display, *button)
			try:
				await until(stopped, timeout=2.5)
				break
			except TimeoutError:
				await asyncio.sleep(0.5)
		await until(stopped, timeout=5)
		refused = await cdp.call('Target.createTarget', {'url': site.url_for('/shared/next')})
		assert 'pressed Cancel' in refused['error']['message']
		await asyncio.sleep(1.5)
		assert not relay.tabs, 'the AI opened a tab of its own after the person pressed Cancel'
	finally:
		if http:
			await http.close()
		await relay.stop()
		_stop(proc)


async def _shared_session(relay: BridgeRelay, site: HTTPServer):
	"""A raw CDP client attached to the shared tab, on a fresh copy of the shared page."""
	http, cdp = await raw_cdp(relay)
	target = next(i['targetId'] for i in (await cdp.call('Target.getTargets'))['result']['targetInfos'] if '/shared' in i['url'])
	sid = (await cdp.call('Target.attachToTarget', {'targetId': target, 'flatten': True}))['result']['sessionId']
	await cdp.call('Page.navigate', {'url': site.url_for('/shared')}, sid)

	async def fresh():
		r = await cdp.call(
			'Runtime.evaluate', {'expression': 'document.title + Array.isArray(window.clicks)', 'returnByValue': True}, sid
		)
		return r.get('result', {}).get('result', {}).get('value') == 'Shared pagetrue'

	await until(fresh)
	return http, cdp, sid


async def test_while_the_person_holds_the_wheel_page_script_cannot_act_either(bridge, site):
	"""Script can click, type and navigate as well as input can, and nobody can tell a read from a write in it.

	So while the person drives (or after Cancel) only looking passes: screenshots, the DOM, the accessibility tree.
	"""
	relay, _ = bridge
	http, cdp, sid = await _shared_session(relay, site)
	try:
		relay.set_holder('human')
		for method, params in [
			('Runtime.evaluate', {'expression': "document.getElementById('b').click()"}),
			('Runtime.callFunctionOn', {'functionDeclaration': 'function () { this.click() }', 'executionContextId': 1}),
			('Page.addScriptToEvaluateOnNewDocument', {'source': 'document.title = 1'}),
			('DOM.focus', {'nodeId': 1}),
			('Made.upMethod', {}),
		]:
			reply = await cdp.call(method, params, sid)
			assert 'the person is using the browser' in reply.get('error', {}).get('message', ''), (method, reply)
		for method, params in [
			('Page.captureScreenshot', {}),
			('DOM.getDocument', {'depth': 1}),
			('Accessibility.getFullAXTree', {}),
		]:
			assert 'result' in await cdp.call(method, params, sid), method
	finally:
		relay.set_holder('agent')
	try:
		clicks = await cdp.call('Runtime.evaluate', {'expression': 'clicks.length', 'returnByValue': True}, sid)
		assert clicks['result']['result']['value'] == 0, 'nothing was clicked while the person held the wheel'
	finally:
		await http.close()


async def test_cookies_of_sites_that_are_not_shared_stay_hidden(bridge, site, mail):
	relay, _ = bridge
	http, cdp, sid = await _shared_session(relay, site)
	try:
		for method, params, session in [
			('Network.getAllCookies', {}, sid),
			('Network.getCookies', {'urls': [mail.url_for('/private'), site.url_for('/shared')]}, sid),
			('Storage.getCookies', {}, sid),
			('Storage.getCookies', {}, None),
		]:
			reply = await cdp.call(method, params, session)
			names = {c['name'] for c in reply['result']['cookies']}
			assert 'mail_session' not in names, f'{method} handed over a cookie of a site the person did not share'
			assert 'shared_pref' in names, f"{method} should still show the shared site's own cookie: {names}"
	finally:
		await http.close()


def _near(rgb, target, tolerance: int = 40) -> bool:
	return all(abs(a - b) <= tolerance for a, b in zip(rgb, target))


def _pill_button(shot, colour: tuple[int, int, int]) -> tuple[int, int]:
	"""Screen point of the button at the right end of the pill drawn in `colour`. The pill is a wide band of that
	colour in the lower half; browser chrome in a similar colour (Vivaldi's zoom slider) is thin, so only rows where
	the colour runs wide count."""
	rows: dict[int, list[int]] = {}
	for y in range(shot.height // 2, shot.height, 2):
		xs = [x for x in range(0, shot.width, 2) if _near(shot.getpixel((x, y)), colour)]
		if len(xs) > 60:
			rows[y] = xs
	assert len(rows) > 3, 'the pill is not on screen'
	right = max(max(xs) for xs in rows.values())
	return right - 50, (min(rows) + max(rows)) // 2  # its button sits at the right end


async def _pill(cdp: RawCDP, sid: str) -> dict | None:
	"""The sharing pill's box in the page, or None when it is not there."""
	expr = "(() => { const h = document.querySelector('retinat-bridge-pill'); if (!h) return null;"
	expr += ' const b = h.getBoundingClientRect(); return {x: b.left, y: b.top, w: b.width, h: b.height,'
	expr += " exclude: h.getAttribute('data-browser-use-exclude')}; })()"
	reply = await cdp.call('Runtime.evaluate', {'expression': expr, 'returnByValue': True}, sid)
	return reply.get('result', {}).get('result', {}).get('value')


async def test_network_events_do_not_carry_cookies_of_sites_that_are_not_shared(bridge, site, mail):
	"""Cookie reads were cut down to shared sites, but Network events carry raw Cookie and Set-Cookie headers for
	every request a shared page makes, to any host. A shared page that embeds a pixel from the person's mail must not
	hand the AI the mail's session through the event stream either."""
	relay, _ = bridge
	site.expect_request('/shared/embeds').respond_with_data(
		f'<!doctype html><title>Embeds</title><img id="px" src="{mail.url_for("/pixel")}">', content_type='text/html'
	)
	http, cdp, sid = await _shared_session(relay, site)
	try:
		await cdp.call('Network.enable', {}, sid)
		cdp.events.clear()
		await cdp.call('Page.navigate', {'url': site.url_for('/shared/embeds')}, sid)

		async def pixel_loaded():
			r = await cdp.call(
				'Runtime.evaluate', {'expression': "document.getElementById('px')?.complete === true", 'returnByValue': True}, sid
			)
			return r.get('result', {}).get('result', {}).get('value') is True

		await until(pixel_loaded)
		await asyncio.sleep(0.5)
		await cdp.call('Runtime.evaluate', {'expression': '1'}, sid)  # drain what arrived meanwhile
		seen = json.dumps(cdp.events)
		assert any(e.get('method') == 'Network.responseReceivedExtraInfo' for e in cdp.events), 'no extra info to check'
		assert 'r3fr3sh-t0k3n' not in seen and 's3cr3t' not in seen, 'the mail session leaked through Network events'
		assert '/pixel' in seen, 'the request itself is still visible; only its cookies are withheld'
		assert (await cdp.call('Network.disable', {}, sid)).get('result') == {}
	finally:
		await http.close()


async def test_storage_of_sites_that_are_not_shared_stays_hidden(bridge, site, mail):
	"""DOMStorage, IndexedDB and CacheStorage take any origin, and Network.loadNetworkResource fetches any address
	with the person's cookies past CORS: through one shared tab, each could read a site the person never shared."""
	relay, _ = bridge
	http, cdp, sid = await _shared_session(relay, site)
	theirs = mail.url_for('/').rstrip('/')
	ours = site.url_for('/').rstrip('/')
	try:
		frame = (await cdp.call('Page.getFrameTree', {}, sid))['result']['frameTree']['frame']['id']
		await cdp.call('DOMStorage.enable', {}, sid)
		await cdp.call('IndexedDB.enable', {}, sid)
		asks = {
			'DOMStorage': ('DOMStorage.getDOMStorageItems', {'storageId': {'securityOrigin': theirs, 'isLocalStorage': True}}),
			'IndexedDB': (
				'IndexedDB.requestData',
				{
					'securityOrigin': theirs,
					'databaseName': 'mail',
					'objectStoreName': 'msgs',
					'indexName': '',
					'skipCount': 0,
					'pageSize': 10,
				},
			),
			'CacheStorage': ('CacheStorage.requestCacheNames', {'securityOrigin': theirs}),
			'loadNetworkResource': (
				'Network.loadNetworkResource',
				{
					'frameId': frame,
					'url': mail.url_for('/private'),
					'options': {'disableCache': True, 'includeCredentials': True},
				},
			),
		}
		leaked = {}
		for name, (method, params) in asks.items():
			reply = await cdp.call(method, params, sid)
			if 'error' not in reply:
				leaked[name] = reply['result']
		assert not leaked, f'read a site that is not shared: {leaked}'
		# Chromium 141 keeps DOMStorage and IndexedDB from extensions and CacheStorage to the tab's frames; the relay
		# holds the same line itself, for builds that don't.
		storage = {'storageId': {'securityOrigin': theirs, 'isLocalStorage': True}}
		with pytest.raises(BridgeError, match='has not shared'):
			relay._check_site('DOMStorage.getDOMStorageItems', storage)
		with pytest.raises(BridgeError, match='has not shared'):
			relay._check_site('CacheStorage.requestEntries', {'cacheId': f'{theirs}/|mail'})
		relay._check_site('DOMStorage.getDOMStorageItems', {'storageId': {'securityOrigin': ours, 'isLocalStorage': True}})
		relay._check_site('IndexedDB.requestDatabaseNames', {'storageKey': ours + '/'})
	finally:
		await http.close()


async def test_a_shared_tab_says_the_ai_is_working_and_one_click_takes_the_wheel(bridge, display, site):
	"""The pill shows in every shared tab, in every browser (Vivaldi has no debugging bar), and is the person's own
	button: "Take the wheel" is an explicit hold, which does not lapse when they stop clicking, unlike a pause."""
	from PIL import ImageGrab

	relay, _ = bridge
	http, cdp, sid = await _shared_session(relay, site)
	try:

		async def shown():
			return await _pill(cdp, sid) is not None

		await until(shown)
		pill = await _pill(cdp, sid)
		assert pill and pill['exclude'] == 'true' and pill['w'] > 100, pill
		await cdp.call('Input.dispatchMouseEvent', {'type': 'mouseMoved', 'x': 1, 'y': 1}, sid)  # tab to the front
		await asyncio.sleep(0.8)
		assert relay.holder == 'agent'

		# Find it on screen as the person sees it: the page's own geometry lags the debugging bar's arrival.
		button = _pill_button(ImageGrab.grab(xdisplay=display).convert('RGB'), (26, 127, 55))
		x_click(display, *button)

		async def held():
			return relay.holder == 'human'

		await until(held, timeout=5)
		await asyncio.sleep(3)  # past resume_after_ms (1.5 s here): a pause would have lapsed, a hold does not
		assert relay.holder == 'human', 'Take the wheel is a hold until handed back, not a pause'

		x_click(display, *button)  # now it reads "Hand back"

		async def back():
			return relay.holder == 'agent'

		await until(back, timeout=5)
	finally:
		relay.set_holder('agent')
		await http.close()


async def test_a_tab_the_person_opens_from_a_shared_tab_waits_for_their_say_so(bridge, display, site):
	"""After BrowserSkill's confirmed tab borrow. A tab opened from a shared tab used to be shared outright, so a
	person middle-clicking from a shared mail to their bank handed the bank to the AI. Now a tab the AI opened
	follows the AI, and one the person opened stays theirs until they press Share on its pill."""
	from PIL import ImageGrab

	relay, _ = bridge
	http, cdp, sid = await _shared_session(relay, site)

	async def listed(path: str) -> bool:
		return any(t['url'].endswith(path) for t in (await get(relay, '/json/list')).json())

	try:
		await cdp.call('Page.navigate', {'url': site.url_for('/shared/opener')}, sid)

		async def loaded():
			r = await cdp.call('Runtime.evaluate', {'expression': 'document.title', 'returnByValue': True}, sid)
			return r.get('result', {}).get('result', {}).get('value') == 'Opener'

		await until(loaded)
		# A pointer reaches a link before pressing it; a press out of nowhere on a fresh page is sometimes dropped (Brave)
		await cdp.call('Input.dispatchMouseEvent', {'type': 'mouseMoved', 'x': 150, 'y': 70}, sid)
		for kind in ('mousePressed', 'mouseReleased'):  # the AI clicks its link
			await cdp.call('Input.dispatchMouseEvent', {'type': kind, 'x': 150, 'y': 70, 'button': 'left', 'clickCount': 1}, sid)
		await until(lambda: listed('/opened/ai'))
		ai_tab = next(t for t in (await get(relay, '/json/list')).json() if t['url'].endswith('/opened/ai'))
		await cdp.call('Target.closeTarget', {'targetId': ai_tab['id']})
		await cdp.call('Input.dispatchMouseEvent', {'type': 'mouseMoved', 'x': 1, 'y': 1}, sid)  # opener to the front
		await asyncio.sleep(1.0)

		ox, oy = _page_origin(ImageGrab.grab(xdisplay=display).convert('RGB'))
		x_click(display, ox + 150, oy + 190)  # the person clicks theirs
		await asyncio.sleep(2.5)
		assert not await listed('/opened/me'), 'a tab the person opened was handed to the AI without asking'

		x_click(display, *_pill_button(ImageGrab.grab(xdisplay=display).convert('RGB'), (57, 73, 171)))  # Share this tab
		await until(lambda: listed('/opened/me'), timeout=5)
		mine = next(t for t in (await get(relay, '/json/list')).json() if t['url'].endswith('/opened/me'))
		await cdp.call('Target.closeTarget', {'targetId': mine['id']})

		async def resumed():
			return relay.holder == 'agent'

		await until(resumed)  # the person's clicks paused the AI; quiet hands it back
	finally:
		await http.close()


async def test_an_outdated_extension_reloads_itself_once_its_files_are_current(own_display, tmp_path):
	"""Branded Chrome keeps running an unpacked extension's old service worker after its files change, until someone
	presses reload on chrome://extensions. Features then go missing without a word. A relay that sees an older
	extension asks it to reload itself, once per version; it reloads only if its files on disk differ from the code
	running, so old files cannot make it loop.

	Chromium unloads an extension loaded with --load-extension when it reloads, so the healing half needs one added
	through Load unpacked: it runs with BRIDGE_TEST_PROFILE and BRIDGE_TEST_EXTENSION (branded Chrome). With
	--load-extension, the test checks the guard: old files that are the ones running leave the extension connected."""
	port = _free_port()
	relay = await BridgeRelay(port=port).start()
	loaded = os.environ.get('BRIDGE_TEST_EXTENSION')  # a folder added through Load unpacked in BRIDGE_TEST_PROFILE
	ext = write_extension(Path(loaded) if loaded else tmp_path / 'ext', relay=f'ws://127.0.0.1:{port}/extension')
	manifest = json.loads((ext / 'manifest.json').read_text())
	current = manifest['version']
	(ext / 'manifest.json').write_text(json.dumps({**manifest, 'version': '0.0.1'}))  # an older copy
	chrome = os.environ.get('BRIDGE_TEST_BROWSER') or LocalBrowserWatchdog._find_installed_browser_path()
	assert chrome
	if loaded:
		profile = tmp_path / 'profile'
		shutil.copytree(os.environ['BRIDGE_TEST_PROFILE'], profile, ignore=shutil.ignore_patterns('Singleton*'))
		args = [chrome, f'--user-data-dir={profile}']
	else:
		args = [
			chrome,
			f'--user-data-dir={tmp_path / "profile"}',
			f'--load-extension={ext}',
			f'--disable-extensions-except={ext}',
		]
	args += ['--no-first-run', '--no-default-browser-check', 'about:blank']
	if os.geteuid() == 0:
		args[1:1] = ['--no-sandbox', '--test-type']
	proc = _launch(args, own_display)
	try:
		assert (await relay.wait_for_extension(timeout=30)).get('extension') == '0.0.1'
		await asyncio.sleep(3)  # asked once; its files are the ones running, so it stays as it is
		assert relay.hello.get('extension') == '0.0.1' and relay._ext is not None, 'it must not loop or unload itself'
		if not loaded:
			return  # Chromium would unload a --load-extension extension on reload: the healing half needs Load unpacked
		await relay.stop()

		write_extension(ext, relay=f'ws://127.0.0.1:{port}/extension')  # the files become current
		relay = await BridgeRelay(port=port).start()

		async def healed():
			return relay.hello.get('extension') == current and relay.hello.get('policy') is not None

		await until(healed, timeout=30)
	finally:
		await relay.stop()
		_stop(proc)


# -- Sites the person allows ---------------------------------------------------------------------------------------
# After Claude in Chrome's per-site permissions. Sharing a tab lets the AI use that tab's site; for any other site the
# person is asked first, in a window of the extension's own that no shared tab can reach.

ALLOW, ALWAYS, NO = (106, 27, 154), (0, 105, 92), (198, 40, 40)  # the ask window's buttons


@pytest.fixture(scope='module')
def elsewhere():
	"""Sites the person has not shared, each on its own loopback address (so each is its own site)."""
	servers = []
	for host in ('127.0.0.2', '127.0.0.3', '127.0.0.4', '127.0.0.5', '127.0.0.6'):
		server = HTTPServer(host=host)
		server.start()
		server.expect_request('/page').respond_with_data(
			f'<!doctype html><title>Elsewhere {host}</title><body style="margin:0;height:100vh">{host}',
			content_type='text/html',
		)
		servers.append(server)
	yield servers
	for server in servers:
		server.clear()
		if server.is_running():
			server.stop()


def _button(shot, colour: tuple[int, int, int]) -> tuple[int, int] | None:
	"""The middle of a solid button of `colour` anywhere on screen, or None."""
	points = [
		(x, y) for y in range(0, shot.height, 3) for x in range(0, shot.width, 3) if _near(shot.getpixel((x, y)), colour, 24)
	]
	if len(points) < 60:
		return None
	xs, ys = sorted(p[0] for p in points), sorted(p[1] for p in points)
	return xs[len(xs) // 2], ys[len(ys) // 2]


async def _asked(display: str, colour: tuple[int, int, int], timeout: float = 10.0) -> tuple[int, int]:
	from PIL import ImageGrab

	for _ in range(int(timeout / 0.25)):
		if where := _button(ImageGrab.grab(xdisplay=display).convert('RGB'), colour):
			return where
		await asyncio.sleep(0.25)
	raise AssertionError('no ask window on screen')


async def _until_ok(cdp: RawCDP, method: str, params: dict, sid: str | None = None, timeout: float = 10.0) -> dict:
	"""Retry a call the person's answer will let through."""
	reply: dict = {}
	for _ in range(int(timeout / 0.25)):
		reply = await cdp.call(method, params, sid)
		if 'error' not in reply:
			return reply
		await asyncio.sleep(0.25)
	raise AssertionError(f'still refused: {reply}')


async def test_the_ai_asks_before_taking_a_shared_tab_to_a_site_the_person_has_not_allowed(bridge, display, site, elsewhere):
	"""A shared tab used to go anywhere the AI sent it, with the person's cookies. Now a navigation to a site they
	haven't allowed sends nothing: a window asks them. Allow lets it through; No is remembered."""
	from PIL import ImageGrab

	relay, _ = bridge
	new, unwanted = elsewhere[0], elsewhere[3]
	http, cdp, sid = await _shared_session(relay, site)
	try:
		refused = await cdp.call('Page.navigate', {'url': new.url_for('/page')}, sid)
		message = refused.get('error', {}).get('message', '')
		assert 'http://127.0.0.2' in message and 'not a site the person has allowed' in message, refused
		assert not new.log, 'the request left before the person allowed the site'
		where = await _asked(display, ALLOW)
		listed = (await get(relay, '/json/list')).json()
		assert not any('ask.html' in t['url'] for t in listed), 'the AI can see the window that asks the person'
		spoof = f'chrome-extension://{EXTENSION_ID}/ask.html#x'
		refused_page = await cdp.call('Page.navigate', {'url': spoof}, sid)
		assert 'only web pages' in refused_page.get('error', {}).get('message', ''), 'the AI opened the ask page itself'

		x_click(display, *where)  # the person allows it
		await _until_ok(cdp, 'Page.navigate', {'url': new.url_for('/page')}, sid)

		async def there():
			r = await cdp.call('Runtime.evaluate', {'expression': 'document.title', 'returnByValue': True}, sid)
			return r.get('result', {}).get('result', {}).get('value') == 'Elsewhere 127.0.0.2'

		await until(there)  # still shared there: the AI reads the page
		assert f'http://127.0.0.2:{new.port}' in (await relay.status())['extension']['sites']['allowed']

		refused = await cdp.call('Page.navigate', {'url': unwanted.url_for('/page')}, sid)
		assert 'not a site the person has allowed' in refused.get('error', {}).get('message', ''), refused
		x_click(display, *await _asked(display, NO))  # the person says no
		await asyncio.sleep(1.0)
		again = await cdp.call('Page.navigate', {'url': unwanted.url_for('/page')}, sid)
		assert 'declined' in again.get('error', {}).get('message', ''), again
		await asyncio.sleep(1.0)
		assert _button(ImageGrab.grab(xdisplay=display).convert('RGB'), ALLOW) is None, 'asked again after a no'
		assert not unwanted.log
	finally:
		await cdp.call('Page.navigate', {'url': site.url_for('/shared')}, sid)
		await http.close()


async def test_a_shared_tab_a_link_takes_to_a_new_site_stops_being_shared_until_the_person_allows_it(
	bridge, display, site, elsewhere
):
	"""A link the AI clicks can't be checked before the request leaves, but the tab stops being shared the moment it
	reaches a site the person hasn't allowed: the AI loses it, and its pill asks the person."""
	from PIL import ImageGrab

	relay, _ = bridge
	away = elsewhere[1]
	site.expect_request('/shared/away').respond_with_data(
		'<!doctype html><title>Away</title><body style="margin:0;height:100vh">'
		f'<a href="{away.url_for("/page")}" style="position:fixed;left:40px;top:40px;width:220px;height:60px;'
		'display:block;background:#ddd">Elsewhere</a></body>',
		content_type='text/html',
	)
	http, cdp, sid = await _shared_session(relay, site)

	async def tab_on(host: str) -> dict | None:
		return next((t for t in (await get(relay, '/json/list')).json() if host in t['url']), None)

	try:
		await cdp.call('Page.navigate', {'url': site.url_for('/shared/away')}, sid)

		async def loaded():
			r = await cdp.call('Runtime.evaluate', {'expression': 'document.title', 'returnByValue': True}, sid)
			return r.get('result', {}).get('result', {}).get('value') == 'Away'

		await until(loaded)
		await cdp.call('Page.enable', {}, sid)
		await cdp.call('Runtime.enable', {}, sid)
		seen = len(cdp.events)
		await cdp.call('Input.dispatchMouseEvent', {'type': 'mouseMoved', 'x': 150, 'y': 70}, sid)
		for kind in ('mousePressed', 'mouseReleased'):  # the AI clicks the link
			await cdp.call('Input.dispatchMouseEvent', {'type': kind, 'x': 150, 'y': 70, 'button': 'left', 'clickCount': 1}, sid)
		# Edge holds a navigation to a new site for tens of seconds here before sending it (its own check of the
		# address, which has no way out of this sandbox), so wait long; Chromium goes at once.
		await until(lambda: _gone(relay, sid), timeout=90)
		assert await tab_on('127.0.0.3') is None, 'a tab on a site the person never allowed is still shared'
		gone = await cdp.call('Runtime.evaluate', {'expression': 'document.title', 'returnByValue': True}, sid)
		assert 'error' in gone, gone
		after = [e for e in cdp.events[seen:] if e.get('method') in ('Page.frameNavigated', 'Runtime.executionContextCreated')]
		assert not any('127.0.0.3' in json.dumps(e) for e in after), f"the new site's page reached the AI: {after}"

		await asyncio.sleep(1.0)  # the pill asks in the tab itself
		x_click(display, *_pill_button(ImageGrab.grab(xdisplay=display).convert('RGB'), (57, 73, 171)))
		await until(lambda: tab_on('127.0.0.3'))
		back = await tab_on('127.0.0.3')
		assert back is not None
		sid3 = (await cdp.call('Target.attachToTarget', {'targetId': back['id'], 'flatten': True}))['result']['sessionId']
		title = await cdp.call('Runtime.evaluate', {'expression': 'document.title', 'returnByValue': True}, sid3)
		assert title.get('result', {}).get('result', {}).get('value') == 'Elsewhere 127.0.0.3', 'allowed, yet unusable'
		assert f'http://127.0.0.3:{away.port}' in (await relay.status())['extension']['sites']['allowed']
	finally:
		back = await tab_on('127.0.0.3')
		if back:  # hand the tab back on the shared page for the tests after this one
			sid2 = (await cdp.call('Target.attachToTarget', {'targetId': back['id'], 'flatten': True}))['result']['sessionId']
			await cdp.call('Page.navigate', {'url': site.url_for('/shared')}, sid2)
		await http.close()


async def _gone(relay: BridgeRelay, sid: str) -> bool:
	return not any('/shared/away' in t['url'] for t in (await get(relay, '/json/list')).json())


async def test_a_tab_the_ai_opens_on_a_new_site_waits_for_the_person_and_always_keeps_it(bridge, display, elsewhere):
	relay, _ = bridge
	wanted = elsewhere[2]
	http, cdp = await raw_cdp(relay)
	try:
		refused = await cdp.call('Target.createTarget', {'url': wanted.url_for('/page')})
		assert 'not a site the person has allowed' in refused.get('error', {}).get('message', ''), refused
		assert not wanted.log
		x_click(display, *await _asked(display, ALWAYS))
		opened = await _until_ok(cdp, 'Target.createTarget', {'url': wanted.url_for('/page')})
		assert (await relay.status())['extension']['sites']['always'] == [f'http://127.0.0.4:{wanted.port}']
		await cdp.call('Target.closeTarget', {'targetId': opened['result']['targetId']})
	finally:
		await http.close()


async def test_browser_navigate_says_a_site_is_waiting_for_the_person_instead_of_claiming_it_went_there(
	bridge, display, site, elsewhere
):
	"""Off the bridge, browser_navigate keeps navigation errors quiet; on it, a refused site used to come back as
	"Navigated to". Now it is an error with effect none: nothing was sent, and the person is being asked."""
	relay, _ = bridge
	new = elsewhere[4]
	server = BrowserUseServer()
	server.bridge, server.cdp_url = relay, relay.cdp_url
	handler = server.server.get_request_handler('tools/call')
	assert handler is not None

	async def navigate(url: str = new.url_for('/page')) -> types.CallToolResult:
		params = types.CallToolRequestParams(name='browser_navigate', arguments={'url': url})
		result = await handler.handler(None, params)  # type: ignore[arg-type]
		assert isinstance(result, types.CallToolResult)
		return result

	try:
		asked = await navigate()
		text = asked.content[0].text  # type: ignore[union-attr]
		assert asked.is_error and (asked.structured_content or {}).get('effect_state') == 'none', text
		assert f'127.0.0.6:{new.port}' in text and 'not a site the person has allowed' in text
		x_click(display, *await _asked(display, NO))
		await asyncio.sleep(1.0)
		declined = await navigate()
		assert declined.is_error and 'declined' in declined.content[0].text  # type: ignore[union-attr]
		assert not new.log
		relay.set_holder('human')  # any other refusal on the bridge is an error too, not "Navigated to"
		held = await navigate(site.url_for('/shared'))
		assert held.is_error and 'the person is using the browser' in held.content[0].text  # type: ignore[union-attr]
	finally:
		relay.set_holder('agent')
		await server._close_all_sessions()

	retinat = RetinatServer(bridge=relay)  # Retinat's open takes the same road
	try:
		opened = await _call(retinat, 'retinat_open', {'url': new.url_for('/page')})
		assert 'declined' in opened and 'effect: none' in opened, opened
	finally:
		await retinat._close_all_sessions()
	assert not new.log


def test_the_servers_know_the_words_the_extension_refuses_a_site_with():
	"""A refused site is reported as effect none because these words mark a refusal made before anything was sent."""
	from browser_use.bridge import EXTENSION_DIR
	from browser_use.bridge.policy import NOT_ALLOWED

	assert NOT_ALLOWED in (EXTENSION_DIR / 'worker.js').read_text()
