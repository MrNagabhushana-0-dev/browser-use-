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

# A page slow to draw: every frame does a fixed amount of work (counted, not timed: the clock stands still)
SLOW = """<!doctype html><title>Slow</title><body><canvas id="c" width="300" height="100"></canvas><script>
const g = document.getElementById('c').getContext('2d');
window.drawn = 0;
function frame() {
  let x = 0;
  for (let i = 0; i < 3e7; i++) x += i % 7;
  g.fillStyle = drawn++ % 2 ? '#f00' : '#00f'; g.fillRect(0, 0, 300, 100 + (x % 1));
  requestAnimationFrame(frame);
}
requestAnimationFrame(frame);
</script></body>"""

# A page that always has work queued, as pages full of ads and trackers do: a message loop that never ends
BUSY = """<!doctype html><title>Busy</title><body><canvas id="c" width="200" height="100"></canvas><script>
const g = document.getElementById('c').getContext('2d');
const channel = new MessageChannel();
window.messages = 0;
channel.port1.onmessage = () => { messages++; let x = 0; for (let i = 0; i < 2e5; i++) x += i; channel.port2.postMessage(x % 1); };
channel.port2.postMessage(0);
function frame() { g.fillStyle = messages % 2 ? '#f00' : '#0f0'; g.fillRect(0, 0, 200, 100); requestAnimationFrame(frame); }
requestAnimationFrame(frame);
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
	main.expect_request('/slow').respond_with_data(SLOW, content_type='text/html')
	main.expect_request('/busy').respond_with_data(BUSY, content_type='text/html')
	main.expect_request('/still').respond_with_data(
		'<!doctype html><title>Still</title><p>Nothing here moves.</p>', content_type='text/html'
	)
	# a page that finds its canvas the way many engines do, the first in the document, with a script in <head> first
	main.expect_request('/engine').respond_with_data(
		'<!doctype html><title>Engine</title><head><script>window.early = 1</script></head><body>'
		'<canvas id="game" width="800" height="600"></canvas><script>window.found = document.querySelector("canvas").id;'
		' window.canvases = document.getElementsByTagName("canvas").length</script></body>',
		content_type='text/html',
	)
	# a page that renders itself anew once loaded, over everything in <html>, as some client-rendered shells do
	main.expect_request('/rewrite').respond_with_data(
		'<!doctype html><title>Rewrite</title><script>addEventListener("load", () => setTimeout(() => {'
		' document.documentElement.innerHTML = "<head><title>Rewritten</title></head><body><p>written anew</p></body>";'
		' window.rewritten = true }, 1000))</script><p>first</p>',
		content_type='text/html',
	)
	other.expect_request('/runner').respond_with_data(GAME, content_type='text/html')
	# the game in a frame from another origin of the same site, as poki.com hosts its games on *.gdn.poki.com
	framed = f'<!doctype html><title>Portal</title><body><iframe id="g" src="{other.url_for("/runner")}" width="420" height="220"></iframe></body>'
	main.expect_request('/portal').respond_with_data(framed, content_type='text/html')
	# the same, with a button that puts the game's frame full screen, as Poki's does
	full = (
		'<!doctype html><title>Full</title><body style="margin:0">'
		'<button id="go" style="position:fixed;left:0;top:0;width:200px;height:60px" '
		'onclick="document.getElementById(\'g\').requestFullscreen()">Full screen</button>'
		f'<iframe id="g" src="{other.url_for("/runner")}" style="margin-top:80px" width="420" height="220"></iframe></body>'
	)
	main.expect_request('/full').respond_with_data(full, content_type='text/html')
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


async def test_a_new_page_in_a_tab_on_our_clock_is_drawn_from_its_first_moment(session, games):
	"""Found when a model played Poki games one after another: after the first was played turn by turn and resumed, the
	next one, opened in the same tab, was never drawn again, and every look waited for a frame until the client gave up.
	The tab stays on our clock, and the old page's beat (what keeps a page that draws nothing drawing while the clock
	stands still) went with the old page. Until the pump gave the new page one, a capture made while the clock stood
	still waited for a frame that never came, and from then on the tab drew nothing at all; and pausing it again kept
	the old page's world, so the new page had no beat at all. Each new page now has its beat from its first moment.

	The pump is stopped here so that only the page itself can have its beat."""
	from browser_use.browser.capture import capture_screenshot

	await _opened(session, games.url_for('/runner'))
	pc = PageClock(session)
	await pc.pause()
	await pc.step(100)
	await pc.resume()
	await pc._stop_pump()
	try:
		cdp = await session.get_or_create_cdp_session(focus=False)
		await session.navigate_to(games.url_for('/still'))
		assert await _in_our_world(session, 'typeof window.__browserUseBeat') == 'function', 'the new page has no beat'
		await pc.pause()  # the next game, played turn by turn: its clock stands still and it draws nothing itself
		stepped = await asyncio.wait_for(pc.step(100), 10)
		assert stepped.game_ms == pytest.approx(100, abs=1) and stepped.draw_s < 2, stepped
		shot = await asyncio.wait_for(capture_screenshot(cdp.cdp_client, {'format': 'jpeg'}, cdp.session_id), 10)
		assert shot['data']
		stepped = await asyncio.wait_for(pc.step(100), 10)
		assert stepped.game_ms == pytest.approx(100, abs=1) and stepped.draw_s < 2, stepped
	finally:
		await pc.close()


async def test_a_capture_that_gets_no_frame_gives_up_and_does_not_hold_up_the_next(session, games):
	"""Found when a model played Poki games: a capture waits for the compositor's next frame while holding the lock that
	every other capture through the connection waits on. In a tab that drew no frame, the first look's capture never
	answered, and every look, zoom and step after it waited behind it until the client gave up on each after 60 s. A
	page whose main thread never yields draws no frame either. A capture now gives up, and frees the next one."""
	from browser_use.browser.capture import CaptureTimeout, capture_screenshot

	await session.navigate_to(games.url_for('/still'))
	cdp = await session.get_or_create_cdp_session(focus=False)
	await cdp.cdp_client.send.Runtime.evaluate(
		params={'expression': 'setTimeout(() => { for (;;) {} }, 50); 1'}, session_id=cdp.session_id
	)
	await asyncio.sleep(0.5)
	for _ in range(2):  # the second is not held up behind the first
		began = time.monotonic()
		with pytest.raises(CaptureTimeout):
			await capture_screenshot(cdp.cdp_client, {'format': 'jpeg'}, cdp.session_id, timeout=2)
		assert time.monotonic() - began < 4


async def test_no_capture_is_sent_over_one_chrome_is_still_taking(session, games):
	"""Found in review: a capture that gave up let the next one go, but Chrome cannot be told to drop a capture, so the
	abandoned one was still running on that tab when the next was sent over it, and overlapping captures can hand each
	other's picture back (see browser/capture.py). Nothing is sent on a tab while an earlier capture there is unanswered:
	the next waits for it, within its own time, and gives up unsent if it does not end."""
	from browser_use.browser import capture
	from browser_use.browser.capture import CaptureTimeout, capture_screenshot

	await session.navigate_to(games.url_for('/still'))
	cdp = await session.get_or_create_cdp_session(focus=False)
	client, sid = cdp.cdp_client, cdp.session_id
	await client.send.Runtime.evaluate(
		params={
			'expression': 'setTimeout(() => { const end = performance.now() + 4000; while (performance.now() < end) {} }, 50); 1'
		},
		session_id=sid,
	)
	await asyncio.sleep(0.3)
	sent = capture.captures_taken
	with pytest.raises(CaptureTimeout):
		await capture_screenshot(client, {'format': 'jpeg'}, sid, timeout=1.5)
	with pytest.raises(CaptureTimeout):  # the first is still being taken: this one is not sent over it
		await capture_screenshot(client, {'format': 'jpeg'}, sid, timeout=1.0)
	assert capture.captures_taken - sent == 1, 'a capture was sent over one Chrome was still taking'
	shot = await capture_screenshot(client, {'format': 'jpeg'}, sid, timeout=10)  # once the page is free again
	assert shot['data'] and capture.captures_taken - sent == 2


async def _in_our_world(session: BrowserSession, expression: str):
	"""`expression` in the clock's own world of the top frame, made here rather than by PageClock."""
	cdp = await session.get_or_create_cdp_session(focus=False)
	tree = await cdp.cdp_client.send.Page.getFrameTree(session_id=cdp.session_id)
	world = await cdp.cdp_client.send.Page.createIsolatedWorld(
		params={'frameId': tree['frameTree']['frame']['id'], 'worldName': 'browser_use_clock'}, session_id=cdp.session_id
	)
	r = await cdp.cdp_client.send.Runtime.evaluate(
		params={'expression': expression, 'contextId': world['executionContextId'], 'returnByValue': True},
		session_id=cdp.session_id,
	)
	return r['result'].get('value')


async def _on_our_clock(session: BrowserSession, games) -> PageClock:
	"""A tab once played turn by turn and resumed: on our clock for good."""
	await _opened(session, games.url_for('/runner'))
	pc = PageClock(session)
	await pc.pause()
	await pc.step(50)
	await pc.resume()
	return pc


async def test_the_beat_is_no_canvas_the_page_can_find(session, games):
	"""Found in review: on each new page from its first moment, the beat's canvas went into <html> between <head> and
	<body> whenever a script in <head> ran first, so the page's own `document.querySelector('canvas')` found it: a 1 px
	canvas that already has a 2d context, where an engine asks for WebGL. The beat is now drawn in a closed shadow root,
	after <body>."""
	pc = await _on_our_clock(session, games)
	try:
		await session.navigate_to(games.url_for('/engine'))
		assert await _value(session, 'window.found') == 'game', 'the page found the beat as its canvas'
		assert await _value(session, 'window.canvases') == 1
		order = await _value(session, '[...document.documentElement.children].map(e => e.tagName.toLowerCase()).join()')
		assert order.index('body') < order.index('browser-use-beat'), order
		assert await _in_our_world(session, 'typeof window.__browserUseBeat') == 'function', 'the page has no beat'
	finally:
		await pc.close()


async def test_a_page_that_writes_itself_anew_keeps_its_beat(session, games):
	"""Found in review: a page that renders itself anew over everything in <html> takes the beat out, while the beat's
	world, and the sign that it has one, live on; pausing it then left a page that draws nothing."""
	pc = await _on_our_clock(session, games)
	try:
		await session.navigate_to(games.url_for('/rewrite'))
		await pc._check_beat()  # our world is the new page's, and its beat is in, before the page renders itself anew
		assert not await _value(session, 'window.rewritten === true')
		for _ in range(30):
			if await _value(session, 'window.rewritten === true'):
				break
			await asyncio.sleep(0.1)
		assert await _value(session, 'document.title') == 'Rewritten'
		await pc._stop_pump()
		await pc.pause()
		assert await _value(session, '!!document.querySelector("browser-use-beat")?.isConnected'), 'no beat on the page'
		stepped = await asyncio.wait_for(pc.step(100), 10)
		assert stepped.draw_s < 2, f'frames did not come: {stepped}'
	finally:
		await pc.close()


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


async def test_a_step_says_how_much_of_its_time_went_to_drawing(session, games):
	"""Found playing Subway Surfers on Poki: a step took 0.25-0.5 s a frame against 17 ms on test pages. Where that time
	goes decides the fix, so each step says it: waiting for the page to draw, or for its clock to move."""
	await _opened(session, games.url_for('/slow'))
	pc = PageClock(session)
	await _paused(pc)
	stepped = await pc.step(200)
	assert stepped.draw_s > 0.2 and stepped.draw_s >= 0.6 * stepped.real_s, stepped
	assert stepped.clock_s < stepped.draw_s, stepped
	await pc.resume()


async def test_a_page_that_always_has_work_queued_does_not_hold_its_clock_back(session, games):
	"""Found on Poki: of a 300 ms step's 3.5 s, 2.7 s went waiting for the clock. Virtual time moves only when the page
	has nothing ready to run, or after a number of tasks in a row; a page that always has work queued held each frame
	for that many of its tasks."""
	await _opened(session, games.url_for('/busy'))
	pc = PageClock(session)
	await _paused(pc)
	stepped = await pc.step(300)
	assert stepped.clock_s < 0.4 * stepped.real_s, stepped
	await pc.resume()


async def test_a_game_frame_put_full_screen_is_stepped_as_fast_as_the_page(session, games):
	"""On Poki a 500 ms step took 28 s with the game's frame full screen, ~0.94 s a frame against ~0.3 s. The guess was
	that the frame covered the 1 px canvas that keeps frames coming; this test showed it does not (it passed before any
	change). Full screen there drew 3.4x the pixels without a GPU, which fits the slowdown."""
	await _opened(session, games.url_for('/full'))
	cdp = await session.get_or_create_cdp_session(focus=False)
	for kind in ('mousePressed', 'mouseReleased'):  # a real click: full screen needs the person's gesture
		await cdp.cdp_client.send.Input.dispatchMouseEvent(
			params={'type': kind, 'x': 100, 'y': 30, 'button': 'left', 'clickCount': 1}, session_id=cdp.session_id
		)
	await asyncio.sleep(0.5)
	assert await _value(session, 'document.fullscreenElement && document.fullscreenElement.id') == 'g'
	pc = PageClock(session)
	await _paused(pc)
	await asyncio.sleep(0.5)  # nothing on screen changes while paused
	stepped = await pc.step(200)
	assert stepped.real_s < 2.0, stepped
	after = await _game(session, framed=True)
	await asyncio.sleep(0.5)
	assert (await _game(session, framed=True))['t'] == pytest.approx(after['t'], abs=0.5), 'still paused after the step'
	await pc.resume()


async def test_a_step_can_draw_fewer_frames_for_a_page_slow_to_draw(session, games):
	"""Frame skip, as the Arcade Learning Environment's: on a page that takes long to draw (a 3D game without a GPU), a
	step at 20 frames a game second draws a third of the frames and plays the same time; a tap still lasts ~50 ms."""
	await _opened(session, games.url_for('/runner'))
	pc = PageClock(session)
	await _paused(pc)
	before = await _game(session)
	stepped = await pc.step(300, fps=20)
	after = await _game(session)
	assert stepped.frames == 6 and stepped.game_ms == pytest.approx(300, abs=1), stepped
	assert after['t'] - before['t'] == pytest.approx(300, abs=51)
	gaps = after['gaps'][len(before['gaps']) :]
	assert len(gaps) <= 8 and max(gaps) == pytest.approx(50, abs=1.5), gaps
	await pc.step(200, tap=['ArrowLeft'], fps=20)
	assert (await _game(session))['lane'] == after['lane'] - 1, 'a tap at 20 fps is still one lane'
	await pc.resume()
