"""Reflexes: rules the page runs every animation frame, from pixels, firing trusted input within milliseconds.

A language model thinks in seconds, so it cannot sit inside a 16 ms frame loop. After the dual-system designs (a
slow reasoner steering a fast policy: Figure's Helix, the slow-fast channel of real-time game agents) and
event-gated streaming perception (the model is woken only by events it asked for), the agent writes rules once; the
page evaluates them every frame against what is drawn, real key and mouse input goes out on their edges, and the agent
hears back only the events it asked to be told about.

Real browser, a real canvas game served by pytest-httpserver; nothing mocked.
"""

import asyncio
import io

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


@pytest.fixture(scope='module')
def clip(tmp_path_factory) -> bytes:
	"""4 s: red with silence, then at 2 s blue with an 880 Hz tone. Generated, not committed."""
	import subprocess

	import imageio_ffmpeg

	out = tmp_path_factory.mktemp('clip') / 'clip.webm'
	subprocess.run(
		[imageio_ffmpeg.get_ffmpeg_exe(), '-loglevel', 'error', '-y']
		+ ['-f', 'lavfi', '-i', 'color=c=red:s=320x180:r=30:d=2', '-f', 'lavfi', '-i', 'color=c=blue:s=320x180:r=30:d=2']
		+ ['-f', 'lavfi', '-i', 'anullsrc=r=48000:cl=mono:d=2', '-f', 'lavfi', '-i', 'sine=f=880:r=48000:d=2']
		+ ['-filter_complex', '[0:v][2:a][1:v][3:a]concat=n=2:v=1:a=1[v][a]', '-map', '[v]', '-map', '[a]']
		+ ['-c:v', 'libvpx', '-b:v', '300k', '-c:a', 'libopus', str(out)],
		check=True,
		timeout=120,
	)
	return out.read_bytes()


async def test_reflexes_hear_and_see_a_video_and_say_where_in_it(clip, tmp_path):
	"""Reflexes are not only for canvas games: on a playing video they see a colour, a cut and a sound start, each
	within a frame or two, and say where in the video it happened."""
	server = HTTPServer()
	server.start()
	server.expect_request('/clip.webm').respond_with_data(clip, content_type='video/webm')
	server.expect_request('/v').respond_with_data(
		'<!doctype html><body style="margin:0;background:#000"><video src="/clip.webm" autoplay playsinline style="width:640px"></video></body>',
		content_type='text/html',
	)
	session = BrowserSession(
		browser_profile=BrowserProfile(
			headless=True, user_data_dir=None, keep_alive=False, args=['--autoplay-policy=no-user-gesture-required']
		)
	)
	await session.start()
	try:
		await session.navigate_to(server.url_for('/v'))
		reflexes = Reflexes(session)
		await reflexes.install(
			[
				{
					'when': "see.locate([0,0,255], {from: 'video'}) && see.locate([0,0,255], {from: 'video'}).n > 200",
					'notify': 'blue',
				},
				{'when': "see.changed({from: 'video'}) > 0.5", 'notify': 'cut'},
				{'when': 'see.onset(15)', 'notify': 'sound'},
			]
		)
		seen = {}
		while len(seen) < 3:
			event = await reflexes.wait(timeout=8)
			if event is None:
				break
			seen.setdefault(event.label, event.media_t)
		assert set(seen) == {'blue', 'cut', 'sound'}, (seen, reflexes.stats())
		for label, at in seen.items():
			assert at is not None and abs(at - 2.0) < 0.2, f'{label} at {at}s of the video, expected 2.0s'
	finally:
		await session.kill()
		server.clear()
		server.stop()


# A runner, drawn as Chrome's T-Rex game is: the player, the ground and every obstacle are the same grey. Obstacles
# slide in from the right; a trusted Space makes the player jump for 600 ms. Where all that grey sits on average says
# nothing; only a reading of one region (just ahead of the player, above the ground) tells an obstacle is coming.
RUNNER = """<!doctype html><title>Run</title><body style="margin:0;background:#fff">
<canvas id="c" width="800" height="300" style="display:block"></canvas><div id="passed">0</div>
<script>
const g = document.getElementById('c').getContext('2d');
let obstacles = [], jumpUntil = 0, next = 0, t0 = performance.now();
window.track = {hits: 0, passed: 0, jumps: 0};
addEventListener('keydown', e => { if (e.key === ' ' && e.isTrusted && performance.now() > jumpUntil) { jumpUntil = performance.now() + 600; track.jumps++; } });
function frame(now) {
  if (now > next && now - t0 > 500) { obstacles.push({x: 800}); next = now + 900 + (obstacles.length % 3) * 250; }
  const up = now < jumpUntil;
  g.fillStyle = '#fff'; g.fillRect(0, 0, 800, 300);
  g.fillStyle = '#535353';
  g.fillRect(0, 270, 800, 3);
  g.fillRect(100, up ? 150 : 230, 40, 40);
  for (const o of obstacles) {
    o.x -= 6;
    g.fillRect(o.x, 230, 20, 40);
    if (!o.hit && o.x < 140 && o.x + 20 > 100 && !up) { o.hit = true; track.hits++; }
    if (!o.counted && o.x + 20 < 100) { o.counted = true; track.passed++; document.getElementById('passed').textContent = String(track.passed); }
  }
  obstacles = obstacles.filter(o => o.x > -30);
  requestAnimationFrame(frame);
}
requestAnimationFrame(frame);
</script></body>"""


async def test_a_rule_reads_one_region_so_it_can_jump_what_is_coming(session, game):
	"""Found at the reflexes' first live use, on the open-source T-Rex runner: see.locate only gave where all of a
	colour sits on average, and there the player, the ground and the obstacles share one grey. `in` reads one region."""
	game.expect_request('/runner').respond_with_data(RUNNER, content_type='text/html')
	await session.navigate_to(game.url_for('/runner'))
	reflexes = Reflexes(session)
	await reflexes.install(
		[
			{'when': 'see.locate([83, 83, 83], {in: [150, 235, 90, 30], tol: 30})', 'key': ' '},
			{'when': "see.text('#passed') === '6'", 'notify': 'six passed'},
		]
	)
	event = await reflexes.wait(timeout=20)
	assert event is not None and event.label == 'six passed', (event, reflexes.stats())
	hits, jumps = await _value(session, 'track.hits'), await _value(session, 'track.jumps')
	assert hits == 0 and jumps >= 6, f'{hits} hit(s), {jumps} jump(s): {reflexes.stats()}'
	await reflexes.clear()
	await session.navigate_to(game.url_for('/runner'))
	await asyncio.sleep(5.0)
	assert await _value(session, 'track.hits') >= 3, 'the control: nobody at the keys, the player runs into them'


async def test_new_rules_start_clean_an_old_rule_s_event_is_not_handed_to_the_new_wait(session, game):
	"""Found live on the T-Rex runner: rules replaced after one of them had notified (nobody waiting) left that event
	queued, and the next wait returned it, from rules no longer running; the stats carried the old rules' inputs."""
	await session.navigate_to(game.url_for('/follow'))
	reflexes = Reflexes(session)
	await reflexes.install([{'when': 'see.frame > 2', 'notify': 'old'}, {'when': 'see.frame === 3', 'key': 'x'}])
	await asyncio.sleep(1.0)  # the old rules notify and act; nobody waits for them
	assert reflexes.stats()['actions'] == 1
	await reflexes.install([{'when': 'see.frame > 30', 'notify': 'new'}])
	assert reflexes.stats()['actions'] == 0, reflexes.stats()
	event = await reflexes.wait(timeout=5)
	assert event is not None and event.label == 'new', event
	await reflexes.clear()


async def test_reflexes_keep_their_speed_while_the_eyes_watch_the_same_tab(session, game, tmp_path):
	"""Found live on the T-Rex runner through Retinat: decision to input was ~80 ms at p50, against ~2 ms here. In
	Retinat the eyes are open on the tab too, sampling the same canvas every frame into the same event loop."""
	from browser_use.eyes.service import Eyes

	await session.navigate_to(game.url_for('/follow'))
	eyes = Eyes(session, seed=7, speech=False, now_path=tmp_path / 'now.json')
	await eyes.open()
	try:
		reflexes = Reflexes(session)
		await reflexes.install(FOLLOW)
		event = await reflexes.wait(timeout=10)
		assert event is not None and event.label == 'game over', event
		stats = reflexes.stats()
		await reflexes.clear()
	finally:
		await eyes.close()
	assert stats['actions'] >= 4, stats
	assert stats['latency_ms_p50'] is not None and stats['latency_ms_p50'] < 40, f'decision to key arriving: {stats}'


async def test_reflexes_keep_their_speed_while_the_session_is_recorded(session, game, tmp_path):
	"""Found live on the T-Rex runner: with retinat_record running, decision to input went from ~2 ms to ~150 ms. The
	recording took every repaint of the moving game as a full-size PNG and decoded it on the event loop the reflexes'
	input goes out on."""
	await session.navigate_to(game.url_for('/follow'))
	recorder = session._recording_watchdog
	assert recorder is not None
	await recorder.start_recording(tmp_path / 'played.mp4', framerate=10)
	try:
		reflexes = Reflexes(session)
		await reflexes.install(FOLLOW)
		event = await reflexes.wait(timeout=10)
		assert event is not None and event.label == 'game over', event
		stats = reflexes.stats()
		await reflexes.clear()
	finally:
		saved = await recorder.stop_recording()
	assert saved is not None and saved.stat().st_size > 10_000, 'the game is on the recording'
	assert stats['actions'] >= 4, stats
	assert stats['latency_ms_p50'] is not None and stats['latency_ms_p50'] < 40, f'decision to key arriving: {stats}'
	assert stats['latency_ms_p95'] < 80, f'decision to key arriving: {stats}'


async def test_a_rule_reads_how_fast_something_moves(session, game):
	"""Found live on the T-Rex runner: the rules had to guess the game's speed from the frame count. see.track gives a
	colour's velocity (viewport px per second) from its last few sightings."""
	page = (
		'<!doctype html><body style="margin:0;background:#000"><canvas id="c" width="800" height="300"></canvas><script>'
		'const g = document.getElementById("c").getContext("2d"); const t0 = performance.now();'
		'function frame(now) { const t = (now - t0) / 1000; const x = t < 1.5 ? 100 + 300 * t : 550 - 180 * (t - 1.5);'
		' g.fillStyle = "#000"; g.fillRect(0, 0, 800, 300); g.fillStyle = "#f00"; g.fillRect(x, 150, 30, 30);'
		' requestAnimationFrame(frame); } requestAnimationFrame(frame);</script></body>'
	)
	game.expect_request('/moving').respond_with_data(page, content_type='text/html')
	await session.navigate_to(game.url_for('/moving'))
	reflexes = Reflexes(session)
	red = '[255, 0, 0]'
	await reflexes.install(
		[
			{
				'when': f'(see.memory.r = see.track({red})) && see.memory.r.vx > 270 && see.memory.r.vx < 330',
				'notify': 'right at ~300',
			},
			{
				'when': f'(see.memory.l = see.track({red})) && see.memory.l.vx < -150 && see.memory.l.vx > -210',
				'notify': 'left at ~180',
			},
			{'when': f'see.track({red}) && Math.abs(see.track({red}).vy) > 30', 'notify': 'it moved up or down'},
		]
	)
	heard = []
	for _ in range(2):
		event = await reflexes.wait(timeout=5)
		heard.append(event.label if event else None)
	await reflexes.clear()
	assert heard == ['right at ~300', 'left at ~180'], (heard, reflexes.stats())


async def test_reflexes_see_a_cross_origin_video_on_screen(clip):
	"""A film streamed from another origin without CORS (Wikimedia Commons' player) cannot be read by the page: a
	rule's reading of it came back empty, so rules never fired. It is read on screen instead, as the compositor
	draws it, and its colour and its cut are still caught within a few frames of where they are in the video."""
	media, page = HTTPServer(), HTTPServer()
	media.start()
	page.start()
	media.expect_request('/clip.webm').respond_with_data(clip, content_type='video/webm')  # no CORS headers
	page.expect_request('/v').respond_with_data(
		f'<!doctype html><body style="margin:0;background:#000"><video src="{media.url_for("/clip.webm")}" autoplay muted '
		'playsinline style="width:640px"></video></body>',
		content_type='text/html',
	)
	session = BrowserSession(
		browser_profile=BrowserProfile(
			headless=True, user_data_dir=None, keep_alive=False, args=['--autoplay-policy=no-user-gesture-required']
		)
	)
	await session.start()
	try:
		await session.navigate_to(page.url_for('/v'))
		for _ in range(50):  # an empty video draws without complaint: check once it has a frame
			if await _value(session, "document.querySelector('video').readyState >= 2"):
				break
			await asyncio.sleep(0.1)
		tainted = await _value(
			session,
			"(() => { const v = document.querySelector('video'), c = document.createElement('canvas').getContext('2d');"
			' try { c.drawImage(v, 0, 0); c.getImageData(0, 0, 1, 1); return false } catch (e) { return true } })()',
		)
		assert tainted, 'the test needs a video the page may not read'
		reflexes = Reflexes(session)
		await reflexes.install(
			[
				{
					'when': "see.locate([0,0,255], {from: 'video'}) && see.locate([0,0,255], {from: 'video'}).n > 200",
					'notify': 'blue',
				},
				{'when': "see.changed({from: 'video'}) > 0.5", 'notify': 'cut'},
			]
		)
		seen = {}
		while len(seen) < 2:
			event = await reflexes.wait(timeout=8)
			if event is None:
				break
			seen.setdefault(event.label, event.media_t)
		stats = reflexes.stats()
		assert set(seen) == {'blue', 'cut'}, (seen, stats)
		for label, at in seen.items():
			# currentTime can trail the frame on screen a little (1.99-2.04 s over six runs, read on screen)
			assert at is not None and abs(at - 2.0) < 0.2, f'{label} at {at}s of the video, expected 2.0s: {stats}'
		# the screen comes as the compositor draws it (a screencast), not forced captures (~50 ms each)
		assert stats['screen_pictures'] > 10 and stats['screen_ms_p50'] < 25, stats
		assert stats['screen_source'] == 'screencast' and stats['screen_pictures'] > 0, stats
	finally:
		await session.kill()
		for server in (media, page):
			server.clear()
			server.stop()


@pytest.fixture(scope='module')
def cuts(tmp_path_factory) -> bytes:
	"""4 s at 30 fps: red, blue, lime, white, cut at 1, 2 and 3 s. Generated, not committed."""
	import subprocess

	import imageio_ffmpeg

	out = tmp_path_factory.mktemp('cuts') / 'cuts.webm'
	args = [imageio_ffmpeg.get_ffmpeg_exe(), '-loglevel', 'error', '-y']
	for c in ('red', 'blue', 'lime', 'white'):
		args += ['-f', 'lavfi', '-i', f'color=c={c}:s=320x180:r=30:d=1']
	args += ['-filter_complex', '[0][1][2][3]concat=n=4:v=1:a=0[v]', '-map', '[v]', '-c:v', 'libvpx', '-b:v', '300k', str(out)]
	subprocess.run(args, check=True, timeout=120)
	return out.read_bytes()


async def test_a_cut_read_on_screen_is_placed_at_the_frame_it_shows(cuts):
	"""Found live on Wikimedia Commons: a cut rule said the film's cuts were 0.10-0.12 s later than they are (and
	0.13-0.18 s that morning). The event gave the video's time when the rule fired: the screen picture's age later,
	and the video's clock runs ~20-30 ms ahead of the frame on screen besides (measured with requestVideoFrameCallback
	on a 1080p film: 17-29 ms at the median). A picture now carries when it was drawn, and the event the time of the
	frame on screen then."""
	media, page = HTTPServer(), HTTPServer()
	media.start()
	page.start()
	media.expect_request('/cuts.webm').respond_with_data(cuts, content_type='video/webm')  # no CORS
	page.expect_request('/v').respond_with_data(
		f'<!doctype html><body style="margin:0;background:#000"><video src="{media.url_for("/cuts.webm")}" muted '
		'playsinline style="width:640px"></video></body>',
		content_type='text/html',
	)
	session = BrowserSession(
		browser_profile=BrowserProfile(
			headless=True, user_data_dir=None, keep_alive=False, args=['--autoplay-policy=no-user-gesture-required']
		)
	)
	await session.start()
	try:
		await session.navigate_to(page.url_for('/v'))
		for _ in range(50):
			if await _value(session, "document.querySelector('video').readyState >= 3"):
				break
			await asyncio.sleep(0.1)
		reflexes = Reflexes(session)
		await reflexes.install([{'when': "see.changed({from: 'video'}) > 0.5", 'notify': 'cut'}])
		await asyncio.sleep(0.5)  # the screen feed is running before the film starts
		await _value(session, "document.querySelector('video').play().then(() => true)")
		at = []
		while len(at) < 3:
			event = await reflexes.wait(timeout=6)
			if event is None:
				break
			if event.media_t is not None and event.media_t > 0.5:
				at.append(event.media_t)
		assert len(at) == 3, (at, reflexes.stats())
		off = [round((t - cut) * 1000) for t, cut in zip(at, (1.0, 2.0, 3.0))]
		# At the cut's frame, or (under load the feed keeps only the newest picture) the next, which is what it saw;
		# never between frames or before the cut (it was -5 to 42 ms)
		assert all(abs(ms) < 3 or abs(ms - 33) < 3 for ms in off), f'cuts placed this many ms from where they are: {off}'
	finally:
		await session.kill()
		for server in (media, page):
			server.clear()
			server.stop()


# A game in a frame from another origin, as on itch.io: it turns a square red at a moment of its own choosing, and
# counts a click that lands on it. It tells the page hosting it when each happened, on its own clock.
FRAMED_GAME = """<!doctype html><body style="margin:0;background:#fff"><canvas id="c" width="400" height="300"></canvas><script>
const c = document.getElementById('c'), g = c.getContext('2d');
let red = null;
g.fillStyle = '#fff'; g.fillRect(0, 0, 400, 300);
setTimeout(() => requestAnimationFrame(() => { g.fillStyle = '#ff0000'; g.fillRect(260, 180, 60, 60);
  requestAnimationFrame(() => { red = performance.now(); }); }), 1500);
c.addEventListener('mousedown', e => { if (!e.isTrusted || red === null) return;
  const hit = e.offsetX >= 260 && e.offsetX < 320 && e.offsetY >= 180 && e.offsetY < 240;
  parent.postMessage({hit, ms: performance.now() - red}, '*'); });
</script></body>"""


async def test_rules_read_a_cross_origin_frame_on_screen_and_click_it(session):
	"""The page cannot see into a frame from another origin at all, so its canvas is out of reach of every other
	reading. {from: 'screen'} reads what is on screen, frames and all: the rule clicks the square soon after it is
	drawn, and the click lands on it."""
	game, host = HTTPServer(), HTTPServer()
	game.start()
	host.start()
	game.expect_request('/game').respond_with_data(FRAMED_GAME, content_type='text/html')
	host.expect_request('/host').respond_with_data(
		f'<!doctype html><body style="margin:0"><iframe src="{game.url_for("/game")}" width="400" height="300" '
		'style="border:0;margin:40px"></iframe><script>window.got = [];'
		'addEventListener("message", e => got.push(e.data));</script></body>',
		content_type='text/html',
	)
	try:
		await session.navigate_to(host.url_for('/host'))
		reflexes = Reflexes(session)
		red = "see.locate([255,0,0], {from: 'screen', tol: 40})"
		await reflexes.install(
			[{'when': f'{red} && {red}.n > 20', 'click': f'[{red}.x, {red}.y]', 'notify': 'red'}],
		)
		event = await reflexes.wait(timeout=8)
		assert event is not None and event.label == 'red', reflexes.stats()
		got = []
		for _ in range(20):
			got = await _value(session, 'window.got') or []
			if got:
				break
			await asyncio.sleep(0.1)
		assert got and got[0]['hit'], (got, reflexes.stats())
		assert got[0]['ms'] < 250, f'clicked {got[0]["ms"]:.0f} ms after the square was drawn: {reflexes.stats()}'
	finally:
		for server in (game, host):
			server.clear()
			server.stop()


async def test_the_screen_feed_ends_with_its_browser(game):
	"""Rules left installed when their browser goes away: the screen feed stops by itself rather than polling a dead
	browser for the rest of the process (found while testing: it fell back to captures that failed every 0.25 s)."""
	game.expect_request('/still').respond_with_data(
		'<!doctype html><body style="background:#0a0"></body>', content_type='text/html'
	)
	session = BrowserSession(browser_profile=BrowserProfile(headless=True, user_data_dir=None, keep_alive=False))
	await session.start()
	await session.navigate_to(game.url_for('/still'))
	reflexes = Reflexes(session)
	await reflexes.install([{'when': "see.locate([0, 170, 0], {from: 'screen'}) && false", 'notify': 'never'}])
	for _ in range(50):
		if reflexes.stats().get('screen_pictures'):
			break
		await asyncio.sleep(0.1)
	assert reflexes.stats().get('screen_pictures'), reflexes.stats()
	feed = reflexes._screen_task
	assert feed is not None
	await session.kill()
	await asyncio.wait({feed}, timeout=8)
	assert feed.done(), f'the screen feed still runs after its browser closed: {reflexes.stats()}'


async def test_rules_reading_the_screen_leave_the_eyes_sight_of_a_film_alone(clip):
	"""Found live on Wikimedia Commons: while rules read a cross-origin film on screen, the eyes' own pictures of it
	(captures of the film's box) came back as the whole page, shrunk and tiled: 20-24 of ~30 with the rules capturing,
	4-7 with the screencast, 0 without rules. Two captures in flight on one tab hand each other's picture back."""
	from browser_use.eyes import Eyes

	media, page = HTTPServer(), HTTPServer()
	media.start()
	page.start()
	media.expect_request('/clip.webm').respond_with_data(clip, content_type='video/webm')
	page.expect_request('/v').respond_with_data(
		'<!doctype html><body style="margin:0;background:#fff"><p style="font:40px sans-serif">A page around the film</p>'
		f'<video src="{media.url_for("/clip.webm")}" autoplay muted loop playsinline style="width:640px;margin:40px"></video></body>',
		content_type='text/html',
	)
	session = BrowserSession(
		browser_profile=BrowserProfile(
			headless=True, user_data_dir=None, keep_alive=False, args=['--autoplay-policy=no-user-gesture-required']
		)
	)
	await session.start()
	try:
		await session.navigate_to(page.url_for('/v'))
		eyes = Eyes(session, now_path=False)
		await eyes.open()
		reflexes = Reflexes(session)
		await reflexes.install([{'when': "see.changed({from: 'video'}) > 0.99", 'notify': 'never'}])
		await eyes.watch(seconds=5, until='time', min_seconds=5)
		from PIL import Image

		pictures = list(eyes.retina._local_keyframes.values())
		assert len(pictures) > 15, 'the eyes watch the film from the compositor'
		page_not_film = 0
		for jpeg in pictures:
			with Image.open(io.BytesIO(jpeg)) as im:
				if min(im.convert('RGB').resize((1, 1)).getpixel((0, 0))) > 150:  # mostly white: the page
					page_not_film += 1
		assert page_not_film == 0, f'{page_not_film} of {len(pictures)} of the eyes pictures were the page: {reflexes.stats()}'
		await reflexes.clear()
		await eyes.close()
	finally:
		await session.kill()
		for server in (media, page):
			server.clear()
			server.stop()


@pytest.mark.parametrize('moving', [False, True], ids=['still', 'animated'])
async def test_rules_read_the_screen_where_it_is_on_a_page_with_a_scrollbar(session, moving):
	"""Found playing Temple Run 2 on Poki: a rule aimed at the game's on-screen key, from a look, read ~9 px off it.
	The screen comes to the rules as screencast frames (the whole window, the scrollbar too) and as captures (the
	page's client area only); both were mapped onto the window's width. Each picture now says what it spans."""
	server = HTTPServer()
	server.start()
	spin = (
		'<div style="position:fixed;left:10px;top:300px;width:30px;height:30px;background:#00f;'
		'animation:s 0.5s linear infinite"></div><style>@keyframes s{to{transform:rotate(360deg)}}</style>'
	)
	server.expect_request('/tall').respond_with_data(
		'<!doctype html><body style="margin:0;height:4000px"><div style="position:fixed;right:30px;top:100px;'
		f'width:40px;height:40px;background:#f00"></div>{spin if moving else ""}</body>',
		content_type='text/html',
	)
	try:
		await session.navigate_to(server.url_for('/tall'))
		reflexes = Reflexes(session)
		red = "see.locate([255,0,0], {from: 'screen', tol: 60})"
		off = 'Math.abs(r.x - (document.documentElement.clientWidth - 50)) + Math.abs(r.y - 120)'
		await reflexes.install(
			[
				{'when': f'(r => r && r.n > 3 && {off} < 5)({red})', 'notify': 'on it'},
				{'when': f'(r => r && r.n > 3 && {off} >= 5)({red})', 'notify': 'off it'},
			]
		)
		event = await reflexes.wait(timeout=8)
		assert event is not None and event.label == 'on it', (event, reflexes.stats())
	finally:
		server.clear()
		server.stop()
