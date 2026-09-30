"""The token meter and cursor drawn inside the page.

Both exist to be seen by a person, which leaves three ways to get them wrong that a test can
actually catch: drawing something that steals clicks, losing the meter on navigation, and
showing a cursor that does not follow the pointer the page is being driven with.
"""

import pytest
from pytest_httpserver import HTTPServer

from browser_use.browser.events import NavigateToUrlEvent
from browser_use.human import HumanInput
from browser_use.vision.overlay import Overlay
from browser_use.vision.video import TokenLedger

PAGE = """<!DOCTYPE html><html><head><title>{title}</title></head>
<body style="margin:0"><button id="b" style="position:fixed;top:0;right:0;width:100%;height:120px">under the meter</button></body></html>"""


@pytest.fixture(scope='module')
def site():
	server = HTTPServer()
	server.start()
	server.expect_request('/one').respond_with_data(PAGE.format(title='one'), content_type='text/html')
	server.expect_request('/outer').respond_with_data(
		'<html><body><iframe id="f" src="/one" width="400" height="200"></iframe></body></html>', content_type='text/html'
	)
	server.expect_request('/two').respond_with_data(PAGE.format(title='two'), content_type='text/html')
	yield server
	server.stop()


async def _goto(session, url):
	event = session.event_bus.dispatch(NavigateToUrlEvent(url=url))
	await event
	await event.event_result(raise_if_any=True, raise_if_none=False)


async def _js(session, expression: str):
	cdp = await session.get_or_create_cdp_session(focus=False)
	result = await cdp.cdp_client.send.Runtime.evaluate(
		params={'expression': expression, 'returnByValue': True}, session_id=cdp.session_id
	)
	return result['result'].get('value')


HUD_TEXT = "document.getElementById('__bu_overlay').shadowRoot.getElementById('hud').textContent"


async def test_the_meter_shows_the_text_it_is_given(browser_session, site):
	await _goto(browser_session, site.url_for('/one'))
	overlay = Overlay(browser_session)
	await overlay.install()
	await overlay.show(['browser-use', 'sent ~483 tok'])

	assert await _js(browser_session, HUD_TEXT) == 'browser-use\nsent ~483 tok'


async def test_the_overlay_does_not_steal_clicks(browser_session, site):
	"""The meter sits over the page's own controls. A click there must reach the page."""
	await _goto(browser_session, site.url_for('/one'))
	overlay = Overlay(browser_session)
	await overlay.install()
	await overlay.show(['x' * 30] * 4)

	target = await _js(browser_session, 'document.elementFromPoint(window.innerWidth - 60, 30).id')
	assert target == 'b', f'the point under the meter resolved to {target!r}, so the overlay intercepts clicks'


async def test_the_meter_survives_a_same_origin_navigation(browser_session, site):
	await _goto(browser_session, site.url_for('/one'))
	overlay = Overlay(browser_session)
	await overlay.install()
	await overlay.show(['still here'])
	await _goto(browser_session, site.url_for('/two'))

	assert await _js(browser_session, HUD_TEXT) == 'still here'


async def test_the_cursor_follows_the_pointer_the_agent_drives(browser_session, site):
	"""Chrome does not move the OS cursor for synthetic input, so the page draws its own."""
	await _goto(browser_session, site.url_for('/one'))
	await Overlay(browser_session).install()
	await HumanInput(browser_session, seed=1).move_to(321, 222)

	cursor = await _js(
		browser_session,
		"(() => { const c = document.getElementById('__bu_overlay').shadowRoot.getElementById('cur');"
		' return [c.style.display, parseFloat(c.style.left), parseFloat(c.style.top)]; })()',
	)
	assert cursor[0] == 'block'
	assert abs(cursor[1] - 321) < 2 and abs(cursor[2] - 222) < 2


async def test_an_iframe_does_not_get_its_own_stale_copy(browser_session, site):
	"""The init script runs in every frame. A second meter inside an iframe would be a copy that
	`show()` never updates, sitting on top of the page showing old numbers."""
	await _goto(browser_session, site.url_for('/outer'))
	overlay = Overlay(browser_session)
	await overlay.install()
	await _goto(browser_session, site.url_for('/outer'))
	await overlay.show(['fresh'])

	inside = await _js(browser_session, "document.getElementById('f').contentDocument.getElementById('__bu_overlay') !== null")
	assert inside is False, 'an iframe grew its own overlay'
	assert await _js(browser_session, HUD_TEXT) == 'fresh'


def test_ledger_lines_state_what_was_sent_and_what_the_alternative_costs():
	ledger = TokenLedger()
	ledger.add_image(800, 450, 'contact sheet')
	lines = Overlay.ledger_lines(ledger, seconds=600, width=1280, height=720)

	text = '\n'.join(lines)
	assert '480' in text  # 800*450/750, what was sent
	assert '737,400' in text  # 600 one-a-second screenshots at 1280x720
	assert 'saved' in text and '99.' in text
	assert 'estimates' in text, 'the meter must say these are estimates'
