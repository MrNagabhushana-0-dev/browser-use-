"""Reflexes: rules the page runs every animation frame, from pixels, firing trusted input within milliseconds.

A language model thinks in seconds, so it cannot sit inside a 16 ms frame loop. After the dual-system designs (a
slow reasoner steering a fast policy: Figure's Helix, the slow-fast channel of real-time game agents) and
event-gated streaming perception (the model is woken only by events it asked for), the agent writes rules once; the
page evaluates them every frame against what is drawn, real key and mouse input goes out on their edges, and the agent
hears back only the events it asked to be told about.

Real browser, a real canvas game served by pytest-httpserver; nothing mocked.
"""

import asyncio

import pytest
from pytest_httpserver import HTTPServer

from browser_use.browser import BrowserProfile, BrowserSession
from browser_use.eyes.reflex import Reflexes

# A red ball wanders left and right on a canvas; a blue paddle under it moves only while an arrow key is held (real
# keyboard events). The page scores, every frame, whether the paddle is under the ball, and stamps when each key
# arrived so the reaction time can be measured on the page's own clock.
GAME = """<!doctype html><title>Follow</title><body style="margin:0;background:#111">
<canvas id="c" width="800" height="300" style="display:block"></canvas>
<div id="status" style="color:#fff;font:20px sans-serif">PLAYING</div>
<script>
const c = document.getElementById('c'), g = c.getContext('2d');
let bx = 400, paddle = 400, held = {ArrowLeft: false, ArrowRight: false}, t0 = performance.now();
window.track = {frames: 0, under: 0, keys: []};
addEventListener('keydown', e => { if (e.key in held && e.isTrusted) { held[e.key] = true; track.keys.push(performance.now()); } });
addEventListener('keyup', e => { if (e.key in held) held[e.key] = false; });
function frame(now) {
  const s = (now - t0) / 1000;
  bx = 400 + 300 * Math.sin(s * 1.3) * Math.cos(s * 0.4);
  if (held.ArrowLeft) paddle -= 9; if (held.ArrowRight) paddle += 9;
  paddle = Math.max(20, Math.min(780, paddle));
  g.fillStyle = '#111'; g.fillRect(0, 0, 800, 300);
  g.fillStyle = '#ff0000'; g.beginPath(); g.arc(bx, 120, 14, 0, 7); g.fill();
  g.fillStyle = '#0000ff'; g.fillRect(paddle - 40, 260, 80, 14);
  if (s > 1) { track.frames++; if (Math.abs(bx - paddle) < 40) track.under++; }
  if (s > 4.5) document.getElementById('status').textContent = 'GAME OVER';
  requestAnimationFrame(frame);
}
requestAnimationFrame(frame);
</script></body>"""

FOLLOW = [
	{
		'when': 'see.locate([255,0,0]) && see.locate([0,0,255]) && see.locate([255,0,0]).x < see.locate([0,0,255]).x - 12',
		'hold': 'ArrowLeft',
	},
	{
		'when': 'see.locate([255,0,0]) && see.locate([0,0,255]) && see.locate([255,0,0]).x > see.locate([0,0,255]).x + 12',
		'hold': 'ArrowRight',
	},
	{'when': "see.text('#status') === 'GAME OVER'", 'notify': 'game over'},
]


@pytest.fixture(scope='module')
def game():
	server = HTTPServer()
	server.start()
	server.expect_request('/follow').respond_with_data(GAME, content_type='text/html')
	yield server
	server.clear()
	if server.is_running():
		server.stop()


@pytest.fixture
async def session():
	s = BrowserSession(browser_profile=BrowserProfile(headless=True, user_data_dir=None, keep_alive=False))
	await s.start()
	yield s
	await s.kill()


async def _value(session: BrowserSession, expression: str):
	cdp = await session.get_or_create_cdp_session(focus=False)
	r = await cdp.cdp_client.send.Runtime.evaluate(
		params={'expression': expression, 'returnByValue': True}, session_id=cdp.session_id
	)
	return r.get('result', {}).get('value')


async def test_reflexes_play_a_canvas_game_from_pixels_at_frame_rate_and_wake_the_agent_only_on_its_event(session, game):
	await session.navigate_to(game.url_for('/follow'))
	reflexes = Reflexes(session)
	await reflexes.install(FOLLOW)  # one call from the agent; everything after runs in the page and the browser
	event = await reflexes.wait(timeout=10)
	assert event is not None and event.label == 'game over', event

	frames, under = await _value(session, 'track.frames'), await _value(session, 'track.under')
	assert frames > 100, f'only {frames} frames scored'
	assert under / frames >= 0.85, f'the paddle was under the ball in {under}/{frames} frames'

	stats = reflexes.stats()
	assert stats['actions'] >= 4, stats
	assert stats['latency_ms_p50'] is not None and stats['latency_ms_p50'] < 40, f'decision to key arriving: {stats}'
	await reflexes.clear()
	assert await _value(session, 'track.frames') > 0


async def test_without_reflexes_the_paddle_does_not_follow(session, game):
	"""The control: the same game with nobody at the keys."""
	await session.navigate_to(game.url_for('/follow'))
	await asyncio.sleep(4.8)
	frames, under = await _value(session, 'track.frames'), await _value(session, 'track.under')
	assert under / max(1, frames) < 0.5, f'{under}/{frames}: the game scores itself too kindly to test anything'


async def test_the_page_cannot_reach_the_reflex_channel_to_forge_input(session, game):
	"""The binding that turns a rule's decision into real input exists only in the reflexes' isolated world. Were it in
	the page's own world, any page could ask for trusted clicks and keys."""
	from browser_use.eyes.reflex import BINDING

	await session.navigate_to(game.url_for('/follow'))
	reflexes = Reflexes(session)
	await reflexes.install([{'when': 'false', 'notify': 'never'}])
	try:
		assert await _value(session, f'typeof window.{BINDING}') == 'undefined'
		assert await _value(session, 'typeof window.__reflex') == 'undefined', 'the rules live apart from page script'
	finally:
		await reflexes.clear()


async def test_a_rule_that_does_not_compile_is_refused_with_the_reason(session, game):
	await session.navigate_to(game.url_for('/follow'))
	reflexes = Reflexes(session)
	with pytest.raises(ValueError, match='did not compile'):
		await reflexes.install([{'when': 'see.locate([255,0,0]) &&', 'notify': 'x'}])
	assert not reflexes.installed
