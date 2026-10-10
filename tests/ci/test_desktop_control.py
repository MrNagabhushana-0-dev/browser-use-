"""Computer use: the AI uses desktop apps through X input, within the person's grants, and never over their hands.

The apps are real X11 windows: Chromium app windows (`--app`) whose WM_CLASS is set with `--class`, so one can be a
"notes" app (granted in full), one a terminal-like app (click tier) and a plain browser (read tier). The AI acts only
through XTest. CDP is used by the test alone, as the oracle for what each page received.
"""

import asyncio
import ctypes
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time

import aiohttp
import pytest
from pytest_httpserver import HTTPServer

from browser_use.browser.watchdogs.local_browser_watchdog import LocalBrowserWatchdog
from browser_use.desktop.service import DesktopControl, Tier, parse_grants
from browser_use.mcp.effects import Refused

pytestmark = pytest.mark.skipif(not shutil.which('Xvfb'), reason='Xvfb not installed')

APP = (
	'<!doctype html><title>{title}</title><body style="margin:0;font:16px sans-serif;background:{bg}">'
	'<textarea id="t" style="position:absolute;left:20px;top:20px;width:400px;height:120px"></textarea>'
	'<button id="b" style="position:absolute;left:20px;top:170px;width:160px;height:50px">Count</button>'
	'<script>window.hits = {{left: 0, right: 0}};'
	"document.getElementById('b').onclick = () => hits.left++;"
	"addEventListener('contextmenu', e => {{ hits.right++; e.preventDefault(); }});</script></body>"
)


def _free_port() -> int:
	with socket.socket() as s:
		s.bind(('127.0.0.1', 0))
		return s.getsockname()[1]


@pytest.fixture(scope='module')
def screen():
	for n in range(121, 160):
		if not os.path.exists(f'/tmp/.X11-unix/X{n}') and not os.path.exists(f'/tmp/.X{n}-lock'):
			break
	proc = subprocess.Popen(
		['Xvfb', f':{n}', '-screen', '0', '1280x900x24'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
	)
	for _ in range(50):
		if os.path.exists(f'/tmp/.X11-unix/X{n}'):
			break
		time.sleep(0.1)
	yield f':{n}'
	proc.terminate()
	proc.wait(timeout=10)


@pytest.fixture(scope='module')
def pages():
	server = HTTPServer()
	server.start()
	server.expect_request('/notes').respond_with_data(APP.format(title='Notes', bg='#eef'), content_type='text/html')
	server.expect_request('/term').respond_with_data(APP.format(title='Term', bg='#222'), content_type='text/html')
	server.expect_request('/web').respond_with_data(APP.format(title='Web', bg='#fff'), content_type='text/html')
	yield server
	server.clear()
	server.stop()


class App:
	"""One Chromium window standing in for a desktop app, with a CDP port the test reads it through."""

	def __init__(self, display: str, url: str, wm_class: str | None, x: int, y: int, w: int, h: int):
		self.port = _free_port()
		self.dir = tempfile.mkdtemp(prefix='desktop-app-')
		chrome = LocalBrowserWatchdog._find_installed_browser_path()
		assert chrome, 'no Chromium found'
		args = [chrome, f'--user-data-dir={self.dir}', '--no-first-run', '--no-default-browser-check']
		args += [f'--remote-debugging-port={self.port}', f'--window-position={x},{y}', f'--window-size={w},{h}']
		args += [f'--class={wm_class}', f'--app={url}'] if wm_class else [url]
		if os.geteuid() == 0:
			args[1:1] = ['--no-sandbox', '--test-type']
		self.proc = subprocess.Popen(
			args, env={**os.environ, 'DISPLAY': display}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
		)
		self.origin = (x, y)

	async def eval(self, expression: str):
		async with aiohttp.ClientSession() as http:
			for _ in range(100):
				try:
					async with http.get(f'http://127.0.0.1:{self.port}/json') as r:
						pages = [p for p in await r.json() if p.get('type') == 'page']
					if pages:
						break
				except aiohttp.ClientError:
					pass
				await asyncio.sleep(0.1)
			async with http.ws_connect(pages[0]['webSocketDebuggerUrl']) as ws:
				await ws.send_json(
					{'id': 1, 'method': 'Runtime.evaluate', 'params': {'expression': expression, 'returnByValue': True}}
				)
				reply = await ws.receive_json()
		return reply['result']['result'].get('value')

	async def ready(self) -> None:
		for _ in range(100):
			try:
				if await self.eval('document.readyState') == 'complete':
					await asyncio.sleep(0.5)
					return
			except Exception:
				pass
			await asyncio.sleep(0.1)
		raise TimeoutError('app window never loaded')

	def close(self) -> None:
		self.proc.terminate()
		try:
			self.proc.wait(timeout=10)
		except subprocess.TimeoutExpired:
			self.proc.kill()
		shutil.rmtree(self.dir, ignore_errors=True)


@pytest.fixture(scope='module')
async def apps(screen, pages):
	notes = App(screen, pages.url_for('/notes'), 'TestNotes', 0, 0, 620, 420)
	term = App(screen, pages.url_for('/term'), 'XTerm', 640, 0, 620, 420)
	web = App(screen, pages.url_for('/web'), None, 0, 440, 620, 440)  # a plain browser window: WM_CLASS Chromium
	try:
		for app in (notes, term, web):
			await app.ready()
		yield {'notes': notes, 'term': term, 'web': web}
	finally:
		for app in (notes, term, web):
			app.close()


@pytest.fixture
def control(screen):
	grants = {'testnotes': Tier.FULL, 'xterm': Tier.CLICK, 'chromium': Tier.READ, 'chromium-browser': Tier.READ}
	desk = DesktopControl(display=screen, enabled=True, grants=grants, resume_after_s=1.5, seed=7)
	desk.scale = 1.0
	yield desk
	desk.close()


def _page_box(app: App, element: str) -> tuple[int, int]:
	"""Screen centre of an element, from the window origin plus the element's place in the page (no window frames
	without a window manager)."""
	x, y = app.origin
	return {'t': (x + 120, y + 60), 'b': (x + 100, y + 195)}[element]


async def test_desktop_control_is_off_unless_turned_on(screen, monkeypatch):
	monkeypatch.delenv('BROWSER_USE_DESKTOP_CONTROL', raising=False)
	with pytest.raises(Refused, match='Desktop control is off'):
		DesktopControl(display=screen)
	assert parse_grants('gedit, chromium:full, xterm:full') == {
		'gedit': Tier.FULL,
		'chromium': Tier.READ,  # a browser is looked at here, used through the bridge
		'xterm': Tier.CLICK,  # a terminal is clicked, never typed into
	}


async def test_the_ai_clicks_and_types_in_a_granted_app(apps, control):
	notes = apps['notes']
	said = await control.click(*_page_box(notes, 't'))
	assert 'in testnotes' in said, said
	typed = await control.type_text('Hello ✓ 日本')
	assert 'Typed 10 characters in testnotes' in typed and 'the screen changed' in typed, typed
	await control.key('ctrl+a')
	value = await notes.eval('JSON.stringify([t.value, t.selectionStart, t.selectionEnd])')
	assert json.loads(value) == ['Hello ✓ 日本', 0, 10]
	clicked = await control.click(*_page_box(notes, 'b'), count=2)
	assert (await notes.eval('hits.left')) == 2, clicked
	await control.click(*_page_box(notes, 'b'), button='right')
	assert (await notes.eval('hits.right')) == 1
	assert control.person.quiet_for() == float('inf'), "the AI's own input is never taken for the person's"


async def test_each_app_gets_only_what_its_tier_allows(apps, control):
	term, web = apps['term'], apps['web']
	await control.click(*_page_box(term, 'b'))
	assert (await term.eval('hits.left')) == 1, 'a terminal-like app can be clicked'
	await control.click(*_page_box(term, 't'))
	with pytest.raises(Refused, match='granted at the click tier'):
		await control.type_text('rm -rf ~')
	with pytest.raises(Refused, match='granted at the click tier'):
		await control.click(*_page_box(term, 'b'), button='right')
	assert (await term.eval('t.value')) == '' and (await term.eval('hits.right')) == 0, 'refused means nothing was sent'

	with pytest.raises(Refused, match='granted at the read tier'):
		await control.click(*_page_box(web, 'b'))
	assert (await web.eval('hits.left')) == 0

	strict = DesktopControl(display=control.display, enabled=True, grants={'testnotes': Tier.FULL}, seed=1)
	try:
		with pytest.raises(Refused, match='has not granted'):
			await strict.click(*_page_box(term, 'b'))
	finally:
		strict.close()
	assert (await term.eval('hits.left')) == 1


def _person_presses_a_key(display: str, keycode: int = 50) -> None:
	"""The person's own keyboard: a second XInput2 master's XTEST keyboard, which is not the AI's input device."""
	x, xi, xtst = ctypes.CDLL('libX11.so.6'), ctypes.CDLL('libXi.so.6'), ctypes.CDLL('libXtst.so.6')
	x.XOpenDisplay.restype = ctypes.c_void_p
	x.XOpenDisplay.argtypes = [ctypes.c_char_p]
	x.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
	x.XFlush.argtypes = x.XCloseDisplay.argtypes = [ctypes.c_void_p]

	class AddMaster(ctypes.Structure):
		_fields_ = [('type', ctypes.c_int), ('name', ctypes.c_char_p), ('send_core', ctypes.c_int), ('enable', ctypes.c_int)]

	class DeviceInfo(ctypes.Structure):
		_fields_ = [
			('deviceid', ctypes.c_int),
			('name', ctypes.c_char_p),
			('use', ctypes.c_int),
			('attachment', ctypes.c_int),
			('enabled', ctypes.c_int),
			('num_classes', ctypes.c_int),
			('classes', ctypes.c_void_p),
		]

	xi.XIChangeHierarchy.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int]
	xi.XIQueryDevice.restype = ctypes.POINTER(DeviceInfo)
	xi.XIQueryDevice.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
	xi.XOpenDevice.restype = ctypes.c_void_p
	xi.XOpenDevice.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
	xtst.XTestFakeDeviceKeyEvent.argtypes = [
		ctypes.c_void_p,
		ctypes.c_void_p,
		ctypes.c_uint,
		ctypes.c_int,
		ctypes.c_void_p,
		ctypes.c_int,
		ctypes.c_ulong,
	]
	dpy = x.XOpenDisplay(display.encode())

	def find() -> int | None:
		n = ctypes.c_int()
		devices = xi.XIQueryDevice(dpy, 0, ctypes.byref(n))
		return next((devices[i].deviceid for i in range(n.value) if devices[i].name == b'person XTEST keyboard'), None)

	if find() is None:
		master = AddMaster(1, b'person', 1, 1)
		xi.XIChangeHierarchy(dpy, ctypes.byref(master), 1)
		x.XSync(dpy, 0)
	keyboard = xi.XOpenDevice(dpy, find())
	xtst.XTestFakeDeviceKeyEvent(dpy, keyboard, keycode, 1, None, 0, 0)  # 50, a shift key, types nothing anywhere
	xtst.XTestFakeDeviceKeyEvent(dpy, keyboard, keycode, 0, None, 0, 0)
	x.XFlush(dpy)
	x.XCloseDisplay(dpy)


async def test_the_ai_waits_while_the_person_uses_the_keyboard(apps, control, screen):
	notes = apps['notes']
	before = await notes.eval('hits.left')
	_person_presses_a_key(screen)
	await asyncio.sleep(0.3)
	assert control.person.last_kind == 'key'
	with pytest.raises(Refused, match='the person is using the computer'):
		await control.click(*_page_box(notes, 'b'))
	assert (await notes.eval('hits.left')) == before
	await asyncio.sleep(1.6)  # resume_after_s is 1.5 here
	await control.click(*_page_box(notes, 'b'))
	assert (await notes.eval('hits.left')) == before + 1, 'after the person is idle the AI carries on'


async def test_zoom_shows_a_region_at_full_resolution(apps, control):
	png = await control.zoom(20, 170, 160, 50, out_width=640)
	from io import BytesIO

	from PIL import Image

	with Image.open(BytesIO(png)) as img:
		assert img.size == (640, 200)
	jpeg, size = await control.screenshot(max_width=640)
	assert size == (640, 450) and control.scale == 2.0
	before = await apps['notes'].eval('hits.left')
	await control.click(50, 97)  # the button's centre, in the pixels of the half-size image just taken
	assert (await apps['notes'].eval('hits.left')) == before + 1, 'coordinates are in the pixels of the last image'


async def test_retinat_offers_computer_use_only_when_turned_on(apps, screen, monkeypatch):
	import mcp.types as types

	from browser_use.retinat import RetinatServer

	monkeypatch.setenv('DISPLAY', screen)
	for name in ('BROWSER_USE_DESKTOP_CONTROL', 'BROWSER_USE_DESKTOP_EYES'):
		monkeypatch.delenv(name, raising=False)
	server = RetinatServer()

	async def names() -> set[str]:
		handler = server.server.get_request_handler('tools/list')
		assert handler is not None
		listed = await handler.handler(None, types.PaginatedRequestParams())  # type: ignore[arg-type]
		return {t.name for t in listed.tools}  # type: ignore[union-attr]

	async def call(name: str, arguments: dict) -> types.CallToolResult:
		handler = server.server.get_request_handler('tools/call')
		assert handler is not None
		result = await handler.handler(None, types.CallToolRequestParams(name=name, arguments=arguments))  # type: ignore[arg-type]
		assert isinstance(result, types.CallToolResult)
		return result

	assert not {n for n in await names() if n.startswith('retinat_desktop_')}, 'nothing on the desktop unless asked for'
	monkeypatch.setenv('BROWSER_USE_DESKTOP_CONTROL', '1')
	monkeypatch.setenv('BROWSER_USE_DESKTOP_APPS', 'testnotes, xterm:click')
	assert {'retinat_desktop_look', 'retinat_desktop_click', 'retinat_desktop_type', 'retinat_desktop_status'} <= await names()
	try:
		looked = await call('retinat_desktop_look', {})
		image = next(b for b in looked.content if isinstance(b, types.ImageContent))
		from base64 import b64decode
		from io import BytesIO

		from PIL import Image

		with Image.open(BytesIO(b64decode(image.data))) as img:
			factor = 1280 / img.width  # the look image is smaller than the screen
		before = await apps['notes'].eval('hits.left')
		bx, by = _page_box(apps['notes'], 'b')
		clicked = await call('retinat_desktop_click', {'x': bx / factor, 'y': by / factor})
		assert not clicked.is_error and 'testnotes' in clicked.content[0].text, clicked  # type: ignore[union-attr]
		assert (await apps['notes'].eval('hits.left')) == before + 1, 'the click landed where the look image said'

		tx, ty = _page_box(apps['term'], 't')
		await call('retinat_desktop_click', {'x': tx / factor, 'y': ty / factor})
		refused = await call('retinat_desktop_type', {'text': 'echo hi'})
		assert refused.is_error and (refused.structured_content or {}).get('effect_state') == 'none', refused
		assert (await apps['term'].eval('t.value')) == ''
		status = (await call('retinat_desktop_status', {})).content[0].text  # type: ignore[union-attr]
		assert 'testnotes (full)' in status and 'xterm (click)' in status and 'Focused: xterm' in status, status
	finally:
		await server._close_all_sessions()


def _close(rgb, target, tolerance: int = 14) -> bool:
	return all(abs(a - b) <= tolerance for a, b in zip(rgb, target))


async def test_windows_of_apps_not_granted_are_hidden_from_the_ai(apps, screen):
	"""After Anthropic's 'hide other windows while acting': the AI sees the apps it may use, not the rest of the desktop."""
	from io import BytesIO

	from PIL import Image

	from browser_use.desktop.service import HIDDEN

	only_notes = DesktopControl(display=screen, enabled=True, grants={'testnotes': Tier.FULL}, seed=3)
	try:
		jpeg, _ = await only_notes.screenshot(max_width=1280)
		with Image.open(BytesIO(jpeg)) as img:
			shot = img.convert('RGB')
		assert _close(shot.getpixel((500, 300)), (238, 238, 255)), 'the granted app is shown as it is'
		assert _close(shot.getpixel((1100, 300)), HIDDEN), 'an app not granted is covered'
		assert _close(shot.getpixel((500, 800)), HIDDEN), 'so is a browser not granted even to look at'
	finally:
		only_notes.close()


async def test_a_click_can_say_which_app_it_expects_and_hovering_points_without_clicking(apps, control):
	notes = apps['notes']
	before = await notes.eval('hits.left')
	with pytest.raises(Refused, match='Not clicked'):
		await control.click(*_page_box(notes, 'b'), expect='Term')
	assert (await notes.eval('hits.left')) == before, 'refused before anything was sent'
	await control.click(*_page_box(notes, 'b'), expect='notes')
	assert (await notes.eval('hits.left')) == before + 1
	moved = await control.move(*_page_box(notes, 't'))
	assert moved.startswith('Moved the pointer') and (await notes.eval('hits.left')) == before + 1
	assert control.x.pointer() == _page_box(notes, 't')


async def test_a_cancelled_click_never_leaves_the_button_held(apps, control):
	task = asyncio.create_task(control.click(*_page_box(apps['notes'], 'b')))
	for _ in range(400):
		if control.x.buttons_down():
			break
		await asyncio.sleep(0.005)
	assert control.x.buttons_down(), 'the button went down'
	task.cancel()
	with pytest.raises(asyncio.CancelledError):
		await task
	assert control.x.buttons_down() == 0, 'released although the click was cancelled half way'


def _x_click(display: str, x: int, y: int) -> None:
	"""A click from outside the AI's controller, standing in for the person's mouse (a plain XTest click)."""
	xlib, xtst = ctypes.CDLL('libX11.so.6'), ctypes.CDLL('libXtst.so.6')
	xlib.XOpenDisplay.restype = ctypes.c_void_p
	xlib.XOpenDisplay.argtypes = [ctypes.c_char_p]
	xlib.XFlush.argtypes = xlib.XCloseDisplay.argtypes = [ctypes.c_void_p]
	xtst.XTestFakeMotionEvent.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_ulong]
	xtst.XTestFakeButtonEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_int, ctypes.c_ulong]
	dpy = xlib.XOpenDisplay(display.encode())
	xtst.XTestFakeMotionEvent(dpy, -1, x, y, 0)
	xtst.XTestFakeButtonEvent(dpy, 1, 1, 0)
	xtst.XTestFakeButtonEvent(dpy, 1, 0, 0)
	xlib.XFlush(dpy)
	xlib.XCloseDisplay(dpy)


async def test_the_ai_asks_on_screen_and_only_the_person_can_answer(apps, screen, control):
	"""After Anthropic's request_access: the person approves apps in a window on their own screen. The AI cannot press
	Allow for them: that window's app can never be granted, so its clicks there are refused."""
	from browser_use.desktop import consent

	asking = asyncio.create_task(consent.ask(screen, {'xterm': Tier.FULL, 'gedit': Tier.FULL}, 'To run the tests.', 60))
	box = None
	for _ in range(150):
		box = next((b for b, app, _ in control.x.toplevels() if app and app.app == consent.CONSENT_CLASS), None)
		if box:
			break
		await asyncio.sleep(0.1)
	assert box, 'no request window on screen'
	await asyncio.sleep(1.5)  # let the page draw
	allow = (box[2] - 18 - 55, box[3] - 16 - 18)  # bottom-right button, 110 px wide

	with pytest.raises(Refused, match='has not granted'):
		await control.click(*allow)
	assert not asking.done(), 'the AI cannot answer for the person'

	_x_click(screen, *allow)  # the person presses Allow
	allowed = await asyncio.wait_for(asking, 20)
	assert allowed == {'xterm': Tier.CLICK, 'gedit': Tier.FULL}, 'a terminal is capped at the click tier'


async def test_session_keys_are_refused_and_typing_can_say_where_it_expects_to_go(apps, control):
	with pytest.raises(Refused, match='session'):
		await control.key('ctrl+alt+Delete')
	with pytest.raises(Refused, match='session'):
		await control.key('ctrl+alt+F2')
	notes = apps['notes']
	said = await control.click(*_page_box(notes, 't'))
	assert 'Keyboard focus: testnotes' in said, said
	before = await notes.eval('t.value')
	with pytest.raises(Refused, match='Not typed'):
		await control.type_text('wrong window', expect='Term')
	assert (await notes.eval('t.value')) == before, 'refused before a single key went out'


async def test_the_persons_escape_stops_the_ai_until_they_let_it_carry_on(apps, control, screen):
	"""After Anthropic's Esc to stop. A pause lifts when the person is idle again; Escape holds until they approve
	more access (their go-ahead), and the AI cannot lift it itself."""
	notes = apps['notes']
	_person_presses_a_key(screen, keycode=9)  # Escape on the default keymap
	await asyncio.sleep(1.8)  # longer than resume_after_s: a plain pause would have lifted
	with pytest.raises(Refused, match='pressed Escape'):
		await control.click(*_page_box(notes, 'b'))
	before = await notes.eval('hits.left')
	control.resume()  # what an approved access request does
	await control.click(*_page_box(notes, 'b'))
	assert (await notes.eval('hits.left')) == before + 1
