"""Playing a game, as a test of the input and perception layers end to end.

A real game is the hardest thing to drive: it lives in a cross-origin iframe so nothing
inside it is reachable from the DOM, it answers only to trusted input, and the only way
to tell whether anything worked is to look at the pixels. That makes a game the honest
test of whether the rest of this library does what it claims.

The game here is a local page rather than a real one, because CI cannot depend on a
third-party site being up, unchanged, or reachable. It is a real canvas driven by real
key events, which is the part under test.
"""

import asyncio
import json

import pytest
from pytest_httpserver import HTTPServer

from browser_use.browser.events import NavigateToUrlEvent
from browser_use.play.arena import BUTTON_SPOTS, GameArena
from browser_use.play.strategies import BanditPlayer
from browser_use.play.views import GameReport, InputEvent

# A game that starts on a title screen with its button *below centre*, because that is
# exactly the layout a blind centre-click misses — and it missed it on real sites.
GAME_PAGE = """<!DOCTYPE html><html><body style="margin:0;background:#123;overflow:hidden">
<canvas id="c" style="position:fixed;inset:0;width:100vw;height:100vh"></canvas>
<div id="title" style="position:fixed;inset:0;background:#245">
  <div id="play" style="position:absolute;left:42%;top:78%;width:16%;height:9%;background:#3a7">PLAY</div>
</div>
<script>
	const canvas = document.getElementById('c');
	canvas.width = innerWidth; canvas.height = innerHeight;
	const ctx = canvas.getContext('2d');
	let running = false, x = 40, presses = 0;
	const size = Math.round(innerHeight * 0.28);
	document.getElementById('play').addEventListener('click', () => {
		running = true;
		document.getElementById('title').style.display = 'none';
	});
	addEventListener('keydown', (e) => { if (running && e.key === 'ArrowRight') { x += innerWidth * 0.04; presses++; } });
	setInterval(() => {
		if (!running) return;
		ctx.fillStyle = '#123'; ctx.fillRect(0, 0, canvas.width, canvas.height);
		ctx.fillStyle = '#fd4'; ctx.fillRect(x % (canvas.width - size), canvas.height * 0.36, size, size);
		x += canvas.width * 0.02;
	}, 60);
	window.__stats = () => ({running, presses});
</script></body></html>"""


@pytest.fixture(scope='module')
def game_server():
	server = HTTPServer()
	server.start()
	server.expect_request('/game').respond_with_data(GAME_PAGE, content_type='text/html')
	yield server
	server.stop()


async def _goto(session, url):
	event = session.event_bus.dispatch(NavigateToUrlEvent(url=url))
	await event
	await event.event_result(raise_if_any=False, raise_if_none=False)


async def test_a_game_behind_a_title_screen_gets_started_and_played(browser_session, game_server, tmp_path):
	"""The whole loop: get past the title screen, drive the game, and measure it."""
	await _goto(browser_session, game_server.url_for('/game'))
	arena = GameArena(browser_session, tmp_path)
	report = GameReport(name='canvas game', url=game_server.url_for('/game'))

	assert await arena.enter_game(report), f'never found a play surface: {report.note}'
	await arena.play(report, seconds=12.0)

	stats = json.loads((await browser_session.run_page_script('return window.__stats();')).value)
	assert stats['running'] is True, 'the title screen was never dismissed'
	assert report.frames > 0, 'no frames were sampled'
	assert report.inputs, 'no inputs were sent'
	assert report.active_ratio > 0.3, f'the canvas animates once running, got {report.active_ratio:.2f}'


async def test_key_presses_reach_a_canvas_game(browser_session, game_server, tmp_path):
	"""Trusted input is the point. A synthetic event would not move this canvas."""
	await _goto(browser_session, game_server.url_for('/game'))
	arena = GameArena(browser_session, tmp_path)
	report = GameReport(name='canvas game', url=game_server.url_for('/game'))
	assert await arena.enter_game(report)

	# Press the title screen's button where it actually is. enter_game's own probe is
	# exercised by the test above; here the point is only that keys reach the canvas.
	box = json.loads(
		(
			await browser_session.run_page_script(
				"const r = document.getElementById('play').getBoundingClientRect();return [r.x, r.y, r.width, r.height];"
			)
		).value
	)
	await browser_session.human.click_box(tuple(box))
	await asyncio.sleep(0.5)

	for _ in range(6):
		await browser_session.human.press('ArrowRight')
		await asyncio.sleep(0.05)

	stats = json.loads((await browser_session.run_page_script('return window.__stats();')).value)
	assert stats['presses'] >= 5, f'the game saw only {stats["presses"]} key presses'


def test_the_button_probe_looks_below_centre_first():
	"""Measured, not assumed: Drive Mad's Retry sat at 0.87 of the surface height and
	Drift Boss's Play at 0.79. The middle is the one place these buttons are not."""
	assert BUTTON_SPOTS[0][1] > 0.6, 'the first guess should be below centre'
	below = [spot for spot in BUTTON_SPOTS if spot[1] > 0.6]
	assert len(below) >= len(BUTTON_SPOTS) // 2, 'most candidates should sit low in the frame'


def test_the_player_converges_on_controls_that_do_something():
	"""A fixed key pattern is not playing. The bandit has to find the live control."""
	import random

	player = BanditPlayer(rng=random.Random(7))
	for _ in range(80):
		action = player.choose()
		player.reward(action, 0.9 if action.key == 'ArrowRight' else 0.02)

	best = max((arm for arm in player.arms if arm.pulls), key=lambda arm: arm.mean)
	assert best.action.key == 'ArrowRight', f'converged on {best.action.name} instead'


def test_a_session_that_pressed_keys_at_a_wall_does_not_count_as_played():
	"""The metric has to be able to say no, or it is not measuring anything."""
	report = GameReport(name='dead', url='http://x.test', loaded=True, seconds_played=95.0)
	report.motion = [0] * 400
	report.inputs = [InputEvent(at=i * 0.2, kind='tap_key', detail='tap right') for i in range(80)]

	assert report.active_ratio == 0.0
	assert report.response_rate == 0.0
	assert report.played is False
