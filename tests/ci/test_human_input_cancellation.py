"""A cancelled input must not leave the browser holding the key or the button.

Every hold in `browser_use.human.input` is a sleep between a down event and an up event,
and the agent cancels its step task on timeout — mid-sleep, most of the time, because the
sleep is where the time goes. The Python task dies; the renderer does not hear about it.
Blink keeps the key or the mouse button down, so the next action types into a page that is
still holding ArrowRight, or drags instead of clicking, and nothing in the logs says why.

So these tests cancel mid-hold and read back the event stream the page actually saw. The
assertion is on the last event for that key or button: it has to be the release.
"""

import asyncio
import json

import pytest
from pytest_httpserver import HTTPServer

from browser_use.browser.events import NavigateToUrlEvent
from browser_use.human import HumanInput

# Ordered log of every down/up the page observes, keys and mouse in one list so "what was
# the last thing that happened to ArrowRight" is answerable.
RECORDER = """<!DOCTYPE html>
<html><head><title>Hold recorder</title>
<style>
	body { margin: 0; }
	#box { position: absolute; left: 300px; top: 220px; width: 160px; height: 60px; background: #ccc; }
</style></head>
<body>
	<div id="box">box</div>
<script>
	window.__log = [];
	const rec = (kind, name) => window.__log.push([kind, name]);
	addEventListener('keydown', e => rec('keydown', e.key), true);
	addEventListener('keyup', e => rec('keyup', e.key), true);
	addEventListener('mousedown', e => rec('mousedown', 'button' + e.button), true);
	addEventListener('mouseup', e => rec('mouseup', 'button' + e.button), true);
</script>
</body></html>"""

BOX = (300.0, 220.0, 160.0, 60.0)


@pytest.fixture(scope='module')
def hold_server():
	server = HTTPServer()
	server.start()
	server.expect_request('/holds').respond_with_data(RECORDER, content_type='text/html')
	yield server
	server.stop()


async def _goto(session, url):
	event = session.event_bus.dispatch(NavigateToUrlEvent(url=url))
	await event
	await event.event_result(raise_if_any=True, raise_if_none=False)


async def _log(session) -> list[list[str]]:
	result = await session.run_page_script('return window.__log;')
	assert result.ok, result.error
	return json.loads(result.value)


async def _wait_for(session, kind: str, name: str, timeout: float = 3.0) -> None:
	"""Block until the page has seen `kind` on `name`, so the cancel lands mid-hold.

	Without this the test races the pointer travel that precedes the press, and a slow CDP
	round trip would cancel before the button was ever down — which passes for the wrong
	reason.
	"""
	deadline = asyncio.get_event_loop().time() + timeout
	while asyncio.get_event_loop().time() < deadline:
		if any(e == [kind, name] for e in await _log(session)):
			return
		await asyncio.sleep(0.05)
	raise AssertionError(f'page never saw {kind} on {name}: {await _log(session)}')


async def test_cancelling_a_key_hold_still_releases_the_key(browser_session, hold_server):
	await _goto(browser_session, hold_server.url_for('/holds'))
	human = HumanInput(browser_session, seed=17)

	task = asyncio.create_task(human.hold('ArrowRight', 5.0))
	await _wait_for(browser_session, 'keydown', 'ArrowRight')
	await asyncio.sleep(0.4)

	task.cancel()
	with pytest.raises(asyncio.CancelledError):
		await task

	# The release is dispatched from the finally, so give the round trip a moment to land.
	await asyncio.sleep(0.3)

	arrows = [e for e in await _log(browser_session) if e[1] == 'ArrowRight']
	assert arrows, 'the hold never reached the page at all'
	# Auto-repeat means many keydowns; the only one that matters is that a keyup follows.
	assert arrows[-1][0] == 'keyup', f'key left stuck down, last events: {arrows[-4:]}'


async def test_cancelling_a_press_and_hold_still_releases_the_button(browser_session, hold_server):
	await _goto(browser_session, hold_server.url_for('/holds'))
	human = HumanInput(browser_session, seed=23)

	# Park the pointer on the box first: press_and_hold travels before it presses, and the
	# travel is not what we are cancelling.
	await human.move_to(BOX[0] + BOX[2] / 2, BOX[1] + BOX[3] / 2)

	task = asyncio.create_task(human.press_and_hold(BOX, 5.0))
	await _wait_for(browser_session, 'mousedown', 'button0')
	await asyncio.sleep(0.4)

	task.cancel()
	with pytest.raises(asyncio.CancelledError):
		await task

	await asyncio.sleep(0.3)

	buttons = [e for e in await _log(browser_session) if e[1] == 'button0']
	assert buttons, 'the press never reached the page at all'
	assert buttons[-1][0] == 'mouseup', f'button left stuck down, last events: {buttons[-4:]}'


async def test_an_uncancelled_hold_is_unchanged(browser_session, hold_server):
	"""The cleanup path must not have moved the release off the happy path or doubled it."""
	await _goto(browser_session, hold_server.url_for('/holds'))
	human = HumanInput(browser_session, seed=29)

	await human.hold('ArrowLeft', 0.3)
	await asyncio.sleep(0.2)

	arrows = [e for e in await _log(browser_session) if e[1] == 'ArrowLeft']
	assert arrows[0] == ['keydown', 'ArrowLeft']
	assert arrows[-1] == ['keyup', 'ArrowLeft']
	assert [e[0] for e in arrows].count('keyup') == 1, f'released more than once: {arrows}'
	# Auto-repeat is the reason hold() exists rather than a bare keyDown.
	assert [e[0] for e in arrows].count('keydown') >= 3, f'no auto-repeat: {arrows}'
