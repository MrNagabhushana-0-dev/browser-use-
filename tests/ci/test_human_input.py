"""Driving the UI for real, versus scripting the DOM.

`element.click()` and `dispatchEvent` produce `isTrusted === false`, emit no movement
beforehand, and bypass hit testing. CDP Input events are trusted, tracked and hit-tested.
That distinction decides whether hover menus open, whether custom scroll containers move,
and whether a site that scores automation lets you through — so these tests assert the
event stream a page actually observes, not just that a click "worked".
"""

import json

import pytest
from pytest_httpserver import HTTPServer

from browser_use.browser.events import NavigateToUrlEvent
from browser_use.human import HumanInput

# Records everything a page can observe about how it was driven.
RECORDER = """<!DOCTYPE html>
<html><head><title>Recorder</title>
<style>
	body { margin: 0; font-family: sans-serif; }
	#target { position: absolute; left: 300px; top: 220px; width: 160px; height: 60px; background: #ddd; }
	#target:hover { background: #8cf; }
	#field { position: absolute; left: 40px; top: 40px; width: 240px; }
	#scroller { position: absolute; left: 40px; top: 400px; width: 300px; height: 200px; overflow: auto; }
	#tall { height: 3000px; background: linear-gradient(#fff, #666); }
</style></head>
<body>
	<input id="field" type="text">
	<div id="target">target</div>
	<div id="scroller"><div id="tall"></div></div>
<script>
	window.log = {moves: [], over: 0, down: [], up: [], clicks: [], wheels: [], keys: []};
	addEventListener('mousemove', e => window.log.moves.push([Math.round(e.clientX), Math.round(e.clientY), e.isTrusted]));
	addEventListener('wheel', e => window.log.wheels.push([Math.round(e.deltaY), e.isTrusted]), {passive: true});
	const t = document.getElementById('target');
	t.addEventListener('mouseover', () => window.log.over++);
	t.addEventListener('mousedown', e => window.log.down.push([performance.now(), e.isTrusted]));
	t.addEventListener('mouseup', e => window.log.up.push([performance.now(), e.isTrusted]));
	t.addEventListener('click', e => window.log.clicks.push({trusted: e.isTrusted, x: Math.round(e.clientX), y: Math.round(e.clientY)}));
	const f = document.getElementById('field');
	f.addEventListener('keydown', e => window.log.keys.push([performance.now(), e.key, e.isTrusted]));
</script>
</body></html>"""


@pytest.fixture(scope='module')
def recorder_server():
	server = HTTPServer()
	server.start()
	server.expect_request('/recorder').respond_with_data(RECORDER, content_type='text/html')
	yield server
	server.stop()


async def _goto(session, url):
	event = session.event_bus.dispatch(NavigateToUrlEvent(url=url))
	await event
	await event.event_result(raise_if_any=True, raise_if_none=False)


async def _log(session) -> dict:
	result = await session.run_page_script('return window.log;')
	assert result.ok, result.error
	return json.loads(result.value)


async def test_a_click_arrives_as_a_hand_would_have_made_it(browser_session, recorder_server):
	await _goto(browser_session, recorder_server.url_for('/recorder'))
	human = HumanInput(browser_session, seed=11)

	await human.click_box((300, 220, 160, 60))
	log = await _log(browser_session)

	# Trusted: the single property `element.click()` can never fake.
	assert log['clicks'], 'the click never landed'
	assert log['clicks'][0]['trusted'] is True

	# The pointer travelled there. A teleport would show one move, or none.
	assert len(log['moves']) >= 6, f'expected a path, saw {len(log["moves"])} moves'
	assert all(move[2] is True for move in log['moves']), 'movement must be trusted too'

	# It passed over the element on the way in, which is what opens hover UI.
	assert log['over'] >= 1

	# The button was held, not tapped in zero time.
	held_ms = log['up'][0][0] - log['down'][0][0]
	assert 30 <= held_ms <= 400, f'unrealistic hold: {held_ms:.0f}ms'

	# It landed inside the box but not on the exact geometric centre every time.
	x, y = log['clicks'][0]['x'], log['clicks'][0]['y']
	assert 300 <= x <= 460 and 220 <= y <= 280


async def test_the_scripted_click_is_distinguishable(browser_session, recorder_server):
	"""The contrast that justifies this whole module."""
	await _goto(browser_session, recorder_server.url_for('/recorder'))

	# Park the pointer away from the target first. Its position survives navigation — as it
	# does in a real browser — so a previous test could leave it resting on the element and
	# fire mouseover the moment the new page paints.
	await HumanInput(browser_session, seed=1).move_to(5, 5)
	await browser_session.run_page_script('window.log.moves = []; window.log.over = 0; return 1;')

	await browser_session.run_page_script("document.getElementById('target').click(); return 'done';")
	log = await _log(browser_session)

	assert log['clicks'], 'the scripted click should still register'
	assert log['clicks'][0]['trusted'] is False, 'element.click() cannot produce a trusted event'
	assert log['moves'] == [], 'a scripted click moves no pointer'
	assert log['over'] == 0, 'and never hovers, so hover-driven UI never opens'


async def test_real_wheel_events_reach_a_custom_scroll_container(browser_session, recorder_server):
	"""window.scrollBy fires no wheel at all, which is why feeds do not advance."""
	await _goto(browser_session, recorder_server.url_for('/recorder'))
	human = HumanInput(browser_session, seed=5)

	await browser_session.run_page_script("window.scrollBy(0, 400); return 'scrolled';")
	assert (await _log(browser_session))['wheels'] == [], 'scrollBy must not produce wheel events'

	await human.move_to(180, 500)
	await human.wheel(600)
	log = await _log(browser_session)

	assert len(log['wheels']) >= 3, f'expected several notches, saw {len(log["wheels"])}'
	assert all(w[1] is True for w in log['wheels'])
	assert sum(w[0] for w in log['wheels']) > 0, 'wheel should have scrolled downward'


async def test_typing_has_human_cadence(browser_session, recorder_server):
	await _goto(browser_session, recorder_server.url_for('/recorder'))
	human = HumanInput(browser_session, seed=3)

	await human.click_box((40, 40, 240, 24))
	await human.type_text('hello world')

	# The field really received the text, through key events rather than a value assignment.
	value = await browser_session.run_page_script("return document.getElementById('field').value;")
	assert json.loads(value.value) == 'hello world'

	log = await _log(browser_session)
	keys = [k for k in log['keys'] if k[2] is True]
	assert len(keys) == len('hello world'), 'every character should arrive as its own trusted keydown'

	gaps = [round(keys[i + 1][0] - keys[i][0]) for i in range(len(keys) - 1)]
	# A constant inter-key delay is the classic synthetic signature; these must vary.
	assert len(set(gaps)) > len(gaps) // 2, f'inter-key gaps look machine-uniform: {gaps}'
	assert min(gaps) > 0


async def test_the_pointer_does_not_teleport_between_clicks(browser_session, recorder_server):
	"""Position persists, so the second move starts where the first one ended."""
	await _goto(browser_session, recorder_server.url_for('/recorder'))
	human = HumanInput(browser_session, seed=2)

	await human.move_to(50, 50)
	assert (human.x, human.y) == (50, 50)
	await human.click_box((300, 220, 160, 60))

	log = await _log(browser_session)
	first = log['moves'][0]
	# The track begins near the previous resting point, not at the target.
	assert abs(first[0] - 50) < 80 and abs(first[1] - 50) < 80, f'path started at {first[:2]}, expected near (50, 50)'


async def test_the_agents_own_scroll_action_produces_real_wheel_events(browser_session, recorder_server):
	"""Pins a property the codebase already has, so a refactor cannot quietly lose it.

	The agent's scroll goes through Input.synthesizeScrollGesture rather than
	window.scrollBy. The difference is invisible in a screenshot and decisive on any page
	that implements its own scrolling on top of `wheel` — swapping one for the other would
	break short-form feeds and virtualized lists with every test still green.
	"""
	from browser_use.browser.events import ScrollEvent

	await _goto(browser_session, recorder_server.url_for('/recorder'))
	await browser_session.run_page_script('window.log.wheels = []; return 1;')

	event = browser_session.event_bus.dispatch(ScrollEvent(direction='down', amount=500))
	await event
	await event.event_result(raise_if_any=False, raise_if_none=False)

	log = await _log(browser_session)
	assert log['wheels'], 'the agent scroll produced no wheel events at all'
	assert all(w[1] is True for w in log['wheels']), 'scroll events must be trusted'
