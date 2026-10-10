"""Desktop eyes: the same perception as the retina (cuts, motion, keyframes) over a whole X display. Opt-in only.

The display is a private Xvfb; what moves on it is a real headful Chrome window showing a page that turns from red
to blue. Nothing is mocked.
"""

import os
import shutil
import subprocess

import pytest
from pytest_httpserver import HTTPServer

from browser_use.browser import BrowserProfile, BrowserSession
from browser_use.browser.profile import ViewportSize
from browser_use.eyes.desktop import DesktopEyes, DesktopEyesOff

pytestmark = pytest.mark.skipif(not shutil.which('Xvfb'), reason='Xvfb not installed')

SWITCH = (
	'<!doctype html><body style="margin:0;background:#d01010;height:100vh">'
	"<script>setTimeout(() => document.body.style.background = '#1030d0', 1800)</script></body>"
)


@pytest.fixture(scope='module')
def display():
	for n in range(91, 99):
		if not os.path.exists(f'/tmp/.X11-unix/X{n}') and not os.path.exists(f'/tmp/.X{n}-lock'):
			break
	proc = subprocess.Popen(['Xvfb', f':{n}', '-screen', '0', '800x600x24'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
	for _ in range(50):
		if os.path.exists(f'/tmp/.X11-unix/X{n}'):
			break
		subprocess.run(['sleep', '0.1'])
	yield f':{n}'
	proc.terminate()
	proc.wait(timeout=10)


def test_desktop_eyes_are_off_unless_asked_for(display, monkeypatch):
	monkeypatch.delenv('BROWSER_USE_DESKTOP_EYES', raising=False)
	with pytest.raises(DesktopEyesOff, match='BROWSER_USE_DESKTOP_EYES'):
		DesktopEyes(display=display)
	DesktopEyes(display=display, enabled=True)  # an explicit yes in code is enough
	monkeypatch.setenv('BROWSER_USE_DESKTOP_EYES', '1')
	DesktopEyes(display=display)


async def test_desktop_eyes_see_a_window_change_colour_as_a_cut(display, monkeypatch):
	server = HTTPServer()
	server.start()
	server.expect_request('/').respond_with_data(SWITCH, content_type='text/html')
	monkeypatch.setenv('DISPLAY', display)
	session = BrowserSession(
		browser_profile=BrowserProfile(
			headless=False, user_data_dir=None, keep_alive=False, window_size=ViewportSize(width=800, height=600)
		)
	)
	try:
		await session.start()
		await session.navigate_to(server.url_for('/'))
		eyes = DesktopEyes(display=display, enabled=True, fps=6)
		p = await eyes.watch(seconds=4.0)
		assert p.items, p.text
		item = p.items[0]
		assert len(item.sight.cuts) >= 1, p.text
		assert 'screen 800x600' in p.text and 'red' in p.text and 'blue' in p.text, p.text
		assert p.image is not None and len(item.keyframes) >= 2, p.text
		look = await eyes.look()
		assert look.image is not None and 'blue' in look.text, look.text
	finally:
		await session.kill()
		server.stop()


async def test_retinat_lists_desktop_tools_only_when_desktop_eyes_are_on(display, monkeypatch):
	import base64
	import io

	import mcp.types as types
	from PIL import Image

	from browser_use.retinat import RetinatServer

	async def names(server) -> set[str]:
		handler = server.server.get_request_handler('tools/list')
		listed = await handler.handler(None, types.PaginatedRequestParams())  # type: ignore[arg-type]
		return {t.name for t in listed.tools}

	monkeypatch.delenv('BROWSER_USE_DESKTOP_EYES', raising=False)
	assert not {n for n in await names(RetinatServer()) if 'desktop' in n}
	monkeypatch.setenv('BROWSER_USE_DESKTOP_EYES', '1')
	monkeypatch.setenv('DISPLAY', display)
	server = RetinatServer()
	assert {'retinat_desktop_look', 'retinat_desktop_watch'} <= await names(server)
	handler = server.server.get_request_handler('tools/call')
	result = await handler.handler(None, types.CallToolRequestParams(name='retinat_desktop_look', arguments={}))  # type: ignore[arg-type]
	assert isinstance(result, types.CallToolResult), result
	images = [b for b in result.content if isinstance(b, types.ImageContent)]
	assert images, result.content
	with Image.open(io.BytesIO(base64.b64decode(images[0].data))) as img:
		assert img.size == (800, 600)
