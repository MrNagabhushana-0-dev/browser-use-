"""Step mode: the page's clock stands still while the agent thinks, and moves only as far as each step asks.

A model's turn takes seconds; a game moves on in milliseconds. Benchmarks of models on real-time games pause the game
while the model thinks (VideoGameBench Lite, Orak, NitroGen) or step it a few frames per action (the Arcade Learning
Environment's frame skip). A browser can do the same: CDP virtual time freezes `performance.now()`, `Date.now()` and
timers, in the page and in the frames its process draws; `requestAnimationFrame`, which CDP does not hold back, is made
to wait for that clock too.

Real browser, real canvas games served by pytest-httpserver; nothing mocked.
"""

import asyncio
import json
import time

import pytest
from pytest_httpserver import HTTPServer

from browser_use.browser import BrowserProfile, BrowserSession
from browser_use.eyes.clock import FRAME_MS, PageClock

# A runner goes forward with the game's time, as games keep it: from performance.now(), from the frame timestamp
# requestAnimationFrame hands out (?clock=raf), or by counting frames (?clock=frames). A tapped arrow changes lane; Up
# held speeds it up.
GAME = """<!doctype html><title>Runner</title><body style="margin:0;background:#222">
<canvas id="c" width="400" height="200" style="display:block"></canvas>
<script>
const byRaf = location.search.includes('clock=raf'), byFrames = location.search.includes('clock=frames');
const c = document.getElementById('c'), g = c.getContext('2d');
let last = null;
window.game = {t: 0, lane: 0, frames: 0, boosted: 0, up: false, wasUp: false, presses: 0, downs: 0, ups: 0, timers: 0, gaps: []};
addEventListener('keydown', e => {
  if (e.repeat) return;
  game.downs++;
  if (e.key === 'ArrowLeft') game.lane--;
  if (e.key === 'ArrowRight') game.lane++;
  if (e.key === 'ArrowUp') game.up = true;
});
addEventListener('keyup', e => { game.ups++; if (e.key === 'ArrowUp') game.up = false; });
setInterval(() => game.timers++, 100);
function frame(ts) {
  const now = byFrames ? game.frames * 1000 / 60 : byRaf ? ts : performance.now();
  if (last !== null) { const dt = now - last; game.t += dt; if (dt > 0) game.gaps.push(dt); }
  last = now;
  game.frames++;
  if (game.up) game.boosted++;
  if (game.up && !game.wasUp) game.presses++;  // a new press, as games that poll keys each frame see one
  game.wasUp = game.up;
  g.fillStyle = '#222'; g.fillRect(0, 0, 400, 200);
  g.fillStyle = '#f00'; g.fillRect((game.t / 10) % 400, 90 + game.lane * 30, 16, 16);
  requestAnimationFrame(frame);
}
requestAnimationFrame(frame);
</script></body>"""

# Two loops: once told to, the first cancels the frame the second has asked for, in the same frame, as a game stops an
# animation it started; and a key that busy-waits on the page's clock, which never moves while it is paused.
LOOPS = """<!doctype html><title>Loops</title><body><script>
window.runs = {a: 0, b: 0}; window.cancelB = false; let bId;
function a() { runs.a++; if (cancelB) cancelAnimationFrame(bId); requestAnimationFrame(a); }
function b() { runs.b++; bId = requestAnimationFrame(b); }
requestAnimationFrame(a); requestAnimationFrame(b);
addEventListener('keydown', () => { const t0 = performance.now(); while (performance.now() - t0 < 1000) {} });
</script></body>"""

LATER = """<!doctype html><title>Later</title><body><script>
window.fired = false; setTimeout(() => { window.fired = true; }, 300);
</script></body>"""


@pytest.fixture(scope='module')
def games():
	main, other = HTTPServer(), HTTPServer()
	main.start()
	other.start()
	main.expect_request('/runner').respond_with_data(GAME, content_type='text/html')
	main.expect_request('/later').respond_with_data(LATER, content_type='text/html')
	main.expect_request('/loops').respond_with_data(LOOPS, content_type='text/html')
	other.expect_request('/runner').respond_with_data(GAME, content_type='text/html')
	# the game in a frame from another origin of the same site, as poki.com hosts its games on *.gdn.poki.com
	framed = f'<!doctype html><title>Portal</title><body><iframe id="g" src="{other.url_for("/runner")}" width="420" height="220"></iframe></body>'
	main.expect_request('/portal').respond_with_data(framed, content_type='text/html')
	yield main
	for s in (main, other):
		s.clear()
		if s.is_running():
			s.stop()


@pytest.fixture
async def session():
	s = BrowserSession(browser_profile=BrowserProfile(headless=True, user_data_dir=None, keep_alive=False))
	await s.start()
	yield s
	await s.kill()


# An isolated world sees a frame's DOM but not its globals: a script it adds runs in the page's own world and copies the
# game out onto the DOM
COPY_OUT = """(() => {
	const s = document.createElement('script');
	s.textContent = 'document.body.dataset.game = JSON.stringify(game)';
	document.body.append(s);
	s.remove();
	return document.body.dataset.game;
})()"""


async def _game(session: BrowserSession, framed: bool = False) -> dict:
	"""The runner's state, from the page or from its frame."""
	cdp = await session.get_or_create_cdp_session(focus=False)
	params: dict = {'expression': 'JSON.stringify(game)', 'returnByValue': True}
	if framed:
		tree = await cdp.cdp_client.send.Page.getFrameTree(session_id=cdp.session_id)
		child = tree['frameTree'].get('childFrames', [])[0]['frame']['id']
		world = await cdp.cdp_client.send.Page.createIsolatedWorld(
			params={'frameId': child, 'worldName': 'test_reader'}, session_id=cdp.session_id
		)
		params = {'expression': COPY_OUT, 'contextId': world['executionContextId'], 'returnByValue': True}
	r = await cdp.cdp_client.send.Runtime.evaluate(params=params, session_id=cdp.session_id)  # type: ignore[arg-type]
	return json.loads(r['result'].get('value') or 'null')


async def _paused(pc: PageClock) -> None:
	await pc.pause()
	await asyncio.sleep(0.1)  # the frame drawing as the clock stopped catches the game up to where it stopped


async def _opened(session: BrowserSession, url: str) -> None:
	await session.navigate_to(url)
	await asyncio.sleep(0.6)  # a few frames of real play first


@pytest.mark.parametrize('clock', ['perf', 'raf', 'frames'])
async def test_a_paused_game_stands_still_while_the_agent_thinks_and_a_step_plays_the_time_asked(session, games, clock):
	await _opened(session, games.url_for('/runner') + f'?clock={clock}')
	pc = PageClock(session)
	await _paused(pc)
	held = await _game(session)
	await asyncio.sleep(1.0)  # the agent thinking
	still = await _game(session)
	assert still['t'] == pytest.approx(held['t'], abs=0.5), f'the game moved while paused: {held["t"]} -> {still["t"]}'
	assert still['timers'] == held['timers'], 'timers fired while paused'
	began = time.monotonic()
	stepped = await pc.step(500)
	took = time.monotonic() - began
	after = await _game(session)
	assert stepped.game_ms == pytest.approx(500, abs=FRAME_MS)
	assert after['t'] - still['t'] == pytest.approx(500, abs=FRAME_MS + 1), f'the game played {after["t"] - still["t"]} ms'
	# each frame of the step is one even tick of the game's clock, not one jump
	gaps = after['gaps'][len(still['gaps']) :]
	assert len(gaps) >= 25 and max(gaps) <= FRAME_MS + 1, gaps
	if clock != 'frames':
		assert after['timers'] - still['timers'] in (4, 5, 6), 'timers keep to the game clock'
	assert took < 3.0, f'500 ms of play took {took:.1f}s'
	await asyncio.sleep(0.5)
	assert (await _game(session))['t'] == pytest.approx(after['t'], abs=0.5), 'paused again after the step'
	await pc.resume()


async def test_keys_reach_the_game_as_a_tap_or_held_until_a_step_lets_go(session, games):
	"""A tap is down for the first frames of its step; a held key stays down from step to step, with no key edge between
	them (a throttle held through turns), until a step leaves it out."""
	await _opened(session, games.url_for('/runner'))
	pc = PageClock(session)
	await _paused(pc)
	before = await _game(session)
	await pc.step(200, tap=['ArrowLeft'])
	tapped = await _game(session)
	assert tapped['lane'] == before['lane'] - 1, 'one tap, one lane'
	assert tapped['downs'] - before['downs'] == 1 and tapped['ups'] - before['ups'] == 1
	await pc.step(400, hold=['ArrowUp'])
	await pc.step(400, hold=['ArrowUp'])
	held = await _game(session)
	frames = held['frames'] - tapped['frames']
	assert held['boosted'] - tapped['boosted'] >= frames - 2, (
		f'Up held for {held["boosted"] - tapped["boosted"]} of {frames} frames'
	)
	assert held['up'] is True and held['downs'] - tapped['downs'] == 1, 'held across the two steps with one press'
	await pc.step(100)
	let_go = await _game(session)
	assert let_go['up'] is False and let_go['ups'] - held['ups'] == 1, 'a step without it lets go'
	# held one step and tapped the next: the game sees it up for a frame, then a new press
	await pc.step(100, hold=['ArrowUp'])
	again = await _game(session)
	await pc.step(100, tap=['ArrowUp'])
	tapped_again = await _game(session)
	assert tapped_again['presses'] - again['presses'] == 1, 'a tap of the key just held is a new press'
	await pc.step(100, hold=['ArrowUp'])
	await pc.resume()
	assert (await _game(session))['up'] is False, 'resume lets go of what was held'


async def test_charged_thinking_plays_the_time_the_agent_took_before_its_keys(session, games):
	"""How the same play would fare live: the time taken to decide is played first, with the keys of the last step still
	held, then the step (OmniGameArena's latency-charged mode)."""
	await _opened(session, games.url_for('/runner'))
	pc = PageClock(session)
	await _paused(pc)
	await pc.step(100, hold=['ArrowUp'])
	before = await _game(session)
	await asyncio.sleep(0.6)  # deciding
	stepped = await pc.step(100, charge_thinking=True)
	after = await _game(session)
	assert stepped.charged_ms == pytest.approx(600, abs=120), stepped
	assert after['t'] - before['t'] == pytest.approx(stepped.charged_ms + stepped.game_ms, abs=FRAME_MS + 1)
	assert after['boosted'] - before['boosted'] >= 30, 'Up stayed held while the decision was played'
	assert after['up'] is False
	await pc.resume()


async def test_a_game_in_a_frame_from_another_origin_is_paused_and_stepped_with_its_page(session, games):
	await _opened(session, games.url_for('/portal'))
	await asyncio.sleep(0.5)
	pc = PageClock(session)
	await _paused(pc)
	held = await _game(session, framed=True)
	await asyncio.sleep(1.0)
	still = await _game(session, framed=True)
	assert still['t'] == pytest.approx(held['t'], abs=0.5), 'the framed game moved while paused'
	await pc.step(300)
	after = await _game(session, framed=True)
	assert after['t'] - still['t'] == pytest.approx(300, abs=FRAME_MS + 1)
	await pc.resume()


async def test_after_resume_the_page_runs_on_real_time_and_a_new_page_works(session, games):
	await _opened(session, games.url_for('/runner'))
	pc = PageClock(session)
	await pc.pause()
	await pc.step(100)
	await pc.resume()
	assert not pc.paused
	a = await _game(session)
	await asyncio.sleep(1.0)
	b = await _game(session)
	assert b['t'] - a['t'] == pytest.approx(1000, abs=250), f'{b["t"] - a["t"]} ms of play in 1 s of real time'
	assert b['timers'] - a['timers'] >= 7
	# a tab once paused keeps its pumped clock: the next page's timers must still fire
	await session.navigate_to(games.url_for('/later'))
	await asyncio.sleep(1.0)
	cdp = await session.get_or_create_cdp_session(focus=False)
	r = await cdp.cdp_client.send.Runtime.evaluate(
		params={'expression': 'fired', 'returnByValue': True}, session_id=cdp.session_id
	)
	assert r['result']['value'] is True, "the next page's 300 ms timer did not fire"


async def _value(session: BrowserSession, expression: str):
	cdp = await session.get_or_create_cdp_session(focus=False)
	r = await cdp.cdp_client.send.Runtime.evaluate(
		params={'expression': expression, 'returnByValue': True}, session_id=cdp.session_id
	)
	return r['result'].get('value')


async def test_a_frame_cancelled_earlier_in_the_same_frame_does_not_run(session, games):
	"""As browsers run frames: a callback cancelled by one that ran before it in the same frame is skipped."""
	await _opened(session, games.url_for('/loops'))
	pc = PageClock(session)
	await _paused(pc)
	await pc.step(100)
	await _value(session, 'cancelB = true')
	before = await _value(session, 'runs.b')
	await pc.step(200)
	assert await _value(session, 'runs.b') - before <= 1, 'the second loop ran on after the first cancelled it'
	assert await _value(session, 'runs.a') > 10
	await pc.resume()


async def test_a_pump_that_stopped_is_started_again_by_resume(session, games):
	"""The pump moves a resumed tab's clock; if it ever stops while the tab lives, the tab stands still for good unless
	something starts it again."""
	await _opened(session, games.url_for('/runner'))
	pc = PageClock(session)
	await _paused(pc)
	await pc.step(50)
	await pc.resume()
	assert pc._pump is not None
	pc._pump.cancel()  # as when it gave up
	await asyncio.gather(pc._pump, return_exceptions=True)
	await pc.resume()
	a = await _game(session)
	await asyncio.sleep(1.0)
	b = await _game(session)
	assert b['t'] - a['t'] == pytest.approx(1000, abs=250), f'{b["t"] - a["t"]} ms of play in 1 s after resume'


async def test_closing_does_not_wait_on_a_page_that_hangs(session, games):
	"""A page that busy-waits on its clock while it is paused never answers again: closing lets go of it in bounded time."""
	await _opened(session, games.url_for('/loops'))
	pc = PageClock(session)
	await _paused(pc)
	await pc.step(50)
	# its keydown handler now spins on a clock that does not move, and the key is never acknowledged
	pressing = asyncio.create_task(pc._key('ArrowUp', 'keyDown'))
	pc.held = ['ArrowUp']
	await asyncio.sleep(0.5)
	began = time.monotonic()
	await asyncio.wait_for(pc.close(), 10)
	assert time.monotonic() - began < 5, f'close took {time.monotonic() - began:.1f}s'
	pressing.cancel()
