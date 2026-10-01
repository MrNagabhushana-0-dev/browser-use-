"""The eyes, against ground truth: generated videos whose every cut, tone and beat is known.

Real browser, real media pipeline, real CDP input; nothing mocked. Media is generated with
ffmpeg at test time and served by pytest-httpserver. The one committed asset,
`assets/speech_librivox_sun_tzu.opus`, is 6 s of LibriVox's public-domain recording of
The Art of War (https://archive.org/details/art_of_war_librivox), used as real speech.

The calibration clip is 10 s in four 2.5 s sections:

    picture   red         | test pattern | blue        | yellow
    sound     silence     | 440 Hz tone  | clicks at 120 bpm | white noise
"""

import asyncio
import io
import random
import re
import subprocess
from pathlib import Path

import pytest
from PIL import Image
from pytest_httpserver import HTTPServer
from werkzeug.wrappers import Response

from browser_use.browser import BrowserProfile, BrowserSession
from browser_use.eyes import Eyes, FrameSample, asr, estimate_image_tokens, hearing, sight
from browser_use.human.touch import FLICK_RELEASE, min_jerk, release_speed, stroke

ASSETS = Path(__file__).parent / 'assets'
SECTION = 2.5


def _ffmpeg() -> str:
	import imageio_ffmpeg

	return imageio_ffmpeg.get_ffmpeg_exe()


def _run(*args: str) -> None:
	subprocess.run([_ffmpeg(), '-y', '-loglevel', 'error', *args], check=True)


def _calibration(path: Path) -> None:
	d = SECTION
	_run(
		*['-f', 'lavfi', '-i', f'color=c=red:s=360x640:r=30:d={d}'],
		*['-f', 'lavfi', '-i', f'testsrc2=s=360x640:r=30:d={d}'],
		*['-f', 'lavfi', '-i', f'color=c=blue:s=360x640:r=30:d={d}'],
		*['-f', 'lavfi', '-i', f'color=c=yellow:s=360x640:r=30:d={d}'],
		*['-f', 'lavfi', '-i', f'anullsrc=r=48000:cl=mono:d={d}'],
		*['-f', 'lavfi', '-i', f'sine=f=440:r=48000:d={d}'],
		*['-f', 'lavfi', '-i', f"aevalsrc='if(lt(mod(t,0.5),0.03),0.8*sin(2*PI*1500*t),0)':s=48000:d={d}"],
		*['-f', 'lavfi', '-i', f'anoisesrc=r=48000:a=0.3:d={d}'],
		*['-filter_complex', '[0][1][2][3]concat=n=4:v=1:a=0,format=yuv420p[v];[4][5][6][7]concat=n=4:v=0:a=1[a]'],
		*['-map', '[v]', '-map', '[a]', '-c:v', 'libvpx-vp9', '-b:v', '300k', '-deadline', 'realtime', '-cpu-used', '8'],
		*['-c:a', 'libopus', '-b:a', '96k', str(path)],
	)


def _solid(path: Path, colour: str, seconds: float, tone_hz: float | None = None) -> None:
	audio = ['-f', 'lavfi', '-i', f'sine=f={tone_hz}:r=48000:d={seconds}'] if tone_hz else []
	maps = ['-map', '0:v'] + (['-map', '1:a', '-c:a', 'libopus'] if tone_hz else [])
	_run(
		*['-f', 'lavfi', '-i', f'color=c={colour}:s=360x640:r=30:d={seconds}'],
		*audio,
		*maps,
		*['-c:v', 'libvpx-vp9', '-b:v', '100k', '-deadline', 'realtime', '-cpu-used', '8', str(path)],
	)


@pytest.fixture(scope='module')
def media(tmp_path_factory):
	root = tmp_path_factory.mktemp('eyes_media')
	_calibration(root / 'calib.webm')
	for name, colour, tone in (('a', 'red', 330), ('b', 'green', 440), ('c', 'blue', 550)):
		_solid(root / f'{name}.webm', colour, 6.0, tone)
	_run(
		*['-f', 'lavfi', '-i', 'color=c=0x224466:s=360x640:r=30:d=6', '-i', str(ASSETS / 'speech_librivox_sun_tzu.opus')],
		*[
			'-map',
			'0:v',
			'-map',
			'1:a',
			'-c:v',
			'libvpx-vp9',
			'-b:v',
			'100k',
			'-deadline',
			'realtime',
			'-c:a',
			'libopus',
			'-shortest',
		],
		str(root / 'speech.webm'),
	)
	return {p.name: p.read_bytes() for p in root.glob('*.webm')}


def _serve(server: HTTPServer, path: str, data: bytes) -> None:
	def handler(request):
		headers = {'Accept-Ranges': 'bytes', 'Content-Type': 'video/webm'}
		match = re.match(r'bytes=(\d+)-(\d*)', request.headers.get('Range', ''))
		if not match:
			return Response(data, headers=headers)
		start = int(match.group(1))
		end = min(int(match.group(2)) if match.group(2) else len(data) - 1, len(data) - 1)
		headers['Content-Range'] = f'bytes {start}-{end}/{len(data)}'
		return Response(data[start : end + 1], status=206, headers=headers)

	server.expect_request(path).respond_with_handler(handler)


PLAYER = '<!doctype html><body style="margin:0;background:#000"><video src="{src}" {attrs} playsinline style="height:100vh;display:block;margin:auto"></video>{extra}</body>'

FEED = """<!doctype html><html><head><style>
html,body{margin:0;height:100%} #feed{height:100vh;overflow-y:scroll;scroll-snap-type:y mandatory}
.reel{height:100vh;scroll-snap-align:start;display:flex;align-items:center;justify-content:center;background:#000;position:relative}
video{height:100%} .cap{position:absolute;bottom:40px;left:20px;color:#fff;font:16px sans-serif}
</style></head><body><div id=feed>
<div class=reel><video src="/a.webm" loop playsinline></video><div class=cap>@first red reel</div></div>
<div class=reel><video src="/b.webm" loop playsinline></video><div class=cap>@second green reel</div></div>
<div class=reel><video src="/c.webm" loop playsinline></video><div class=cap>@third blue reel</div></div>
</div><script>
const io = new IntersectionObserver(es => es.forEach(e => { const v = e.target;
  if (e.isIntersecting) { v.currentTime = 0; v.play().catch(() => {}) } else v.pause() }), {threshold: [0.6]});
document.querySelectorAll('video').forEach(v => io.observe(v));
window.__input = {touchstart: 0, touchmove: 0, touchend: 0, scroll: 0, last: ''};
for (const k of ['touchstart', 'touchmove', 'touchend']) addEventListener(k, e => { __input[k]++; __input.last = k + '@' + Math.round(performance.now()) }, {passive: true});
document.getElementById('feed').addEventListener('scroll', () => __input.scroll++, {passive: true});
/*EXTRA*/
</script></body></html>"""

# A feed that ignores touch entirely and only advances on the wheel, as some desktop players do.
WHEEL_ONLY = """
const feed = document.getElementById('feed'); feed.style.overflowY = 'hidden'; feed.style.touchAction = 'none';
let busy = false;
feed.addEventListener('wheel', e => { e.preventDefault(); if (busy || Math.abs(e.deltaY) < 30) return; busy = true;
  feed.scrollBy({top: Math.sign(e.deltaY) * innerHeight}); setTimeout(() => busy = false, 700) }, {passive: false});
"""


# A feed whose first flick flies past the next item, as a real one can under a fast thumb.
OVERSHOOT = """
const feed = document.getElementById('feed'); feed.style.overflowY = 'hidden'; feed.style.touchAction = 'none';
let y0 = null, flicks = 0;
feed.addEventListener('touchstart', e => { y0 = e.touches[0].clientY }, {passive: true});
feed.addEventListener('touchend', e => { if (y0 === null) return;
  const dy = (e.changedTouches[0] || {}).clientY - y0; y0 = null; if (Math.abs(dy) < 80) return;
  const items = dy < 0 ? (flicks++ === 0 ? 2 : 1) : -1;
  feed.scrollBy({top: items * innerHeight}) }, {passive: true});
"""


@pytest.fixture(scope='module')
def site(media):
	server = HTTPServer()
	server.start()
	for name, data in media.items():
		_serve(server, f'/{name}', data)
	server.expect_request('/calib').respond_with_data(
		PLAYER.format(src='/calib.webm', attrs='autoplay', extra=''), content_type='text/html'
	)
	server.expect_request('/calib-muted').respond_with_data(
		PLAYER.format(src='/calib.webm', attrs='autoplay muted', extra=''), content_type='text/html'
	)
	server.expect_request('/still').respond_with_data(
		PLAYER.format(src='/a.webm', attrs='autoplay loop', extra=''), content_type='text/html'
	)
	server.expect_request('/speech').respond_with_data(
		PLAYER.format(src='/speech.webm', attrs='autoplay', extra=''), content_type='text/html'
	)
	server.expect_request('/feed').respond_with_data(FEED.replace('/*EXTRA*/', ''), content_type='text/html')
	server.expect_request('/wheel-feed').respond_with_data(FEED.replace('/*EXTRA*/', WHEEL_ONLY), content_type='text/html')
	server.expect_request('/overshoot-feed').respond_with_data(FEED.replace('/*EXTRA*/', OVERSHOOT), content_type='text/html')
	server.expect_request('/none').respond_with_data('<!doctype html><p>no video here</p>', content_type='text/html')
	yield server
	server.stop()


@pytest.fixture(scope='module')
async def session():
	s = BrowserSession(
		browser_profile=BrowserProfile(
			headless=True, user_data_dir=None, keep_alive=True, args=['--autoplay-policy=no-user-gesture-required']
		)
	)
	await s.start()
	yield s
	await s.kill()


@pytest.fixture
async def eyes(session, tmp_path):
	e = Eyes(session, seed=7, speech=False, now_path=tmp_path / 'now.json')
	yield e
	await e.close()


async def _open(eyes: Eyes, session: BrowserSession, url: str) -> None:
	await session.navigate_to(url)
	await eyes.open()


def _near(value: float, target: float, tol: float) -> bool:
	return abs(value - target) <= tol


# -- sight and hearing against ground truth -------------------------------------------------


def test_a_heuristic_speech_sliver_the_voice_model_rejects_does_not_become_its_own_segment():
	# The heuristic sometimes calls the half-second straddling a boundary "speech"; the voice model
	# then (rightly) finds none and it becomes "sound". It is a mix of both sides, not a sound of
	# its own, and must fold into a neighbour like any other sliver: this was a flaky CI failure.
	from browser_use.eyes.hearing import Hearing, Segment, apply_speech_regions

	h = Hearing(
		segments=[
			Segment(0.0, 2.5, 'silence', -90.0),
			Segment(2.5, 5.0, 'tone', -20.0, '441 Hz'),
			Segment(5.0, 7.4, 'beats', -25.0, '~120 bpm'),
			Segment(7.4, 8.1, 'speech', -22.0),
			Segment(8.1, 10.0, 'noise', -24.0),
		]
	)
	out = apply_speech_regions(h, regions=[])
	assert [s.kind for s in out.segments] == ['silence', 'tone', 'beats', 'noise'], out.segments
	assert out.segments[-1].t0 == 7.4 and out.speech_by == 'vad'

	# Real speech the model confirms stays speech, however short the heuristic made it.
	h2 = Hearing(segments=[Segment(0.0, 1.0, 'silence', -90.0), Segment(1.0, 1.6, 'speech', -20.0)])
	assert [s.kind for s in apply_speech_regions(h2, regions=[(1.0, 1.6)]).segments] == ['silence', 'speech']


async def test_cuts_and_sounds_are_found_where_they_are(eyes, session, site):
	await _open(eyes, session, site.url_for('/calib'))
	p = await eyes.watch(seconds=4 * SECTION + 1.0, until='time')

	assert len(p.items) == 1, p.text
	item = p.items[0]
	cuts = item.sight.cuts
	assert len(cuts) == 3, f'expected cuts at 2.5/5/7.5, got {cuts}'
	for got, want in zip(cuts, (SECTION, 2 * SECTION, 3 * SECTION)):
		assert _near(got, want, 0.2), f'cut at {got:.2f}s, expected {want}s'

	segments = [s for s in item.hearing.segments if s.duration >= 0.5]
	kinds = [s.kind for s in segments]
	assert kinds[:4] == ['silence', 'tone', 'beats', 'noise'], [hearing.describe_segment(s) for s in segments]
	tone = segments[1]
	assert _near(float(tone.detail.split()[0]), 440, 6), tone.detail
	assert _near(tone.t0, SECTION, 0.25) and _near(tone.t1, 2 * SECTION, 0.25), (tone.t0, tone.t1)
	beats = segments[2]
	assert _near(float(beats.detail.strip('~ bpm')), 120, 6), beats.detail
	clicks = [t for t in item.hearing.onsets if 2 * SECTION - 0.1 <= t <= 3 * SECTION]
	assert len(clicks) >= 4, f'5 clicks in the beats section, onsets there: {clicks}'


async def test_a_muted_video_is_still_heard_and_said_to_be_muted(eyes, session, site):
	# captureStream taps the audio before volume and mute: the agent can listen without
	# making a sound, and must not report a muted reel as silent.
	await _open(eyes, session, site.url_for('/calib-muted'))
	p = await eyes.watch(seconds=2 * SECTION + 0.5, until='time')

	item = p.items[0]
	assert item.muted is True
	assert 'tone' in item.hearing.kinds, [hearing.describe_segment(s) for s in item.hearing.segments]
	assert 'muted for the person watching' in p.text


async def test_keyframes_are_the_videos_own_pixels_not_a_screenshot(eyes, session, site):
	await _open(eyes, session, site.url_for('/calib'))
	p = await eyes.watch(seconds=4 * SECTION + 0.5, until='time', detail='glance')

	item = p.items[0]
	assert len(item.keyframes) >= 4, 'one keyframe per shot fits in the budget'
	images = [Image.open(io.BytesIO(k.jpeg)).convert('RGB') for k in item.keyframes if k.jpeg]
	# The video is 360x640 inside a 16:9 viewport; a screenshot would have the viewport's shape.
	for img in images:
		assert _near(img.width / img.height, 360 / 640, 0.02), img.size
	centre: list[tuple[int, int, int]] = [img.resize((1, 1)).getpixel((0, 0)) for img in images]  # type: ignore[misc]
	reds = [c for c in centre if c[0] > 180 and c[1] < 60 and c[2] < 60]
	blues = [c for c in centre if c[2] > 180 and c[0] < 60]
	assert reds and blues, f'the red and blue shots should both be on the sheet: {centre}'
	assert p.image and p.image_size and p.image_tokens == estimate_image_tokens(*p.image_size)


async def test_the_page_cannot_see_the_retina(eyes, session, site):
	await _open(eyes, session, site.url_for('/calib'))
	cdp = await session.get_or_create_cdp_session(focus=False)
	result = await cdp.cdp_client.send.Runtime.evaluate(
		params={'expression': 'typeof window.__retina + "/" + typeof window.__retina_emit', 'returnByValue': True},
		session_id=cdp.session_id,
	)
	assert result['result']['value'] == 'undefined/undefined'
	assert (await eyes.retina.page_state()).get('running') is True


async def test_the_retina_survives_a_reload(eyes, session, site):
	await _open(eyes, session, site.url_for('/calib'))
	await eyes.watch(seconds=1.0)
	first = eyes.retina.attended.get('vid')
	await session.navigate_to(site.url_for('/still'))
	p = await eyes.watch(seconds=2.0)
	assert p.items and p.items[-1].vid != first
	assert (p.items[-1].info.get('src') or '').endswith('/a.webm')


# -- watching like a person -----------------------------------------------------------------


async def test_watch_until_event_returns_at_the_first_cut(eyes, session, site):
	await _open(eyes, session, site.url_for('/calib'))
	p = await eyes.watch(seconds=9.0, until='event', min_seconds=0.5)
	# The first thing that happens is the tone starting over silence (2.5 s) together with the cut.
	assert p.stop_reason.startswith(('cut at', 'sound changed')), p.stop_reason
	assert p.ended_at - p.started_at < 5.0, f'should stop soon after 2.5 s, took {p.ended_at - p.started_at:.1f}s'


async def test_watch_until_bored_stops_on_a_still_picture(eyes, session, site):
	await _open(eyes, session, site.url_for('/still'))
	p = await eyes.watch(seconds=15.0, until='bored', min_seconds=1.0)
	assert 'nothing new' in p.stop_reason or 'looped' in p.stop_reason, p.stop_reason
	assert p.ended_at - p.started_at < 8.0


async def test_hold_pauses_the_video_and_the_next_watch_resumes_it(eyes, session, site):
	await _open(eyes, session, site.url_for('/calib'))
	await eyes.watch(seconds=1.5, hold=True)
	t_held = (await eyes.retina.page_state())['attended']['t']
	await asyncio.sleep(1.0)
	assert (await eyes.retina.page_state())['attended']['paused'] is True
	assert _near((await eyes.retina.page_state())['attended']['t'], t_held, 0.05), 'nothing plays unseen while held'
	await eyes.watch(seconds=1.0)
	assert (await eyes.retina.page_state())['attended']['paused'] is False


async def test_no_video_is_said_plainly(eyes, session, site):
	await _open(eyes, session, site.url_for('/none'))
	p = await eyes.watch(seconds=1.0)
	assert p.items == [] and p.image is None and 'no video' in p.text


async def test_the_ambient_line_is_written_for_hooks(eyes, session, site):
	await _open(eyes, session, site.url_for('/calib'))
	await eyes.watch(seconds=3.0)
	import json

	now = json.loads(eyes.now_path.read_text())
	assert now['line'].startswith('👁 watching a video') and now['url'].endswith('/calib')


# -- moving on like a person ----------------------------------------------------------------


async def test_a_flick_moves_the_feed_and_next_confirms_it_by_sight(eyes, session, site):
	await _open(eyes, session, site.url_for('/feed'))
	await eyes.watch(seconds=1.5)
	first = eyes.retina.attended
	moved = await eyes.next()
	assert moved.moved and moved.method == 'swipe', moved
	assert eyes.retina.attended['vid'] != first['vid']
	assert eyes.retina.attended['text'].startswith('@second'), 'one flick is one item, not a fling past it'
	cdp = await session.get_or_create_cdp_session(focus=False)
	scrolled = await cdp.cdp_client.send.Runtime.evaluate(
		params={'expression': 'document.getElementById("feed").scrollTop / innerHeight', 'returnByValue': True},
		session_id=cdp.session_id,
	)
	assert round(scrolled['result']['value']) == 1, scrolled
	back = await eyes.next(direction='up')
	assert back.moved and eyes.retina.attended['text'].startswith('@first')


async def test_next_notices_it_flew_past_an_item_and_comes_back(eyes, session, site):
	await _open(eyes, session, site.url_for('/overshoot-feed'))
	await eyes.watch(seconds=1.0)
	moved = await eyes.next()
	assert moved.moved and 'overshot by 1' in moved.note and 'flicked back' in moved.note, moved
	assert eyes.retina.attended['text'].startswith('@second'), eyes.retina.attended.get('text')


async def test_next_falls_back_to_the_wheel_when_the_feed_ignores_touch(eyes, session, site):
	await _open(eyes, session, site.url_for('/wheel-feed'))
	await eyes.watch(seconds=1.0)
	moved = await eyes.next()
	assert moved.moved and moved.method == 'wheel' and moved.tries == ['swipe', 'long-swipe', 'wheel'], moved


async def _feed_position(session) -> str:
	cdp = await session.get_or_create_cdp_session(focus=False)
	r = await cdp.cdp_client.send.Runtime.evaluate(
		params={
			'expression': 'JSON.stringify({pos: document.getElementById("feed").scrollTop / innerHeight, input: window.__input})',
			'returnByValue': True,
		},
		session_id=cdp.session_id,
	)
	return r['result']['value']


async def test_browse_watches_each_reel_once_and_puts_them_on_one_sheet(eyes, session, site):
	await _open(eyes, session, site.url_for('/feed'))
	p = await eyes.browse(items=3, max_seconds=4.0, min_seconds=1.0)
	captions = [i.info.get('text', '') for i in p.items]
	assert [c.split()[0] for c in captions] == ['@first', '@second', '@third'], (
		p.text + f'\nscrollTop/innerHeight={await _feed_position(session)}'
	)
	for item, hz in zip(p.items, (330, 440, 550)):
		tone = next((s for s in item.hearing.segments if s.kind == 'tone'), None)
		assert tone and _near(float(tone.detail.split()[0]), hz, 8), [hearing.describe_segment(s) for s in item.hearing.segments]
	assert p.image is not None and p.image_size[1] > 3 * 150, 'one row per reel'


# -- speech (optional extra) ---------------------------------------------------------------


@pytest.mark.skipif(not asr.available(), reason='speech extra (faster-whisper) not installed')
async def test_speech_is_located_by_the_voice_model(session, site, tmp_path):
	eyes = Eyes(session, speech=True, now_path=False)
	try:
		await session.navigate_to(site.url_for('/speech'))
		await eyes.open()
		await eyes.watch(seconds=6.5, transcribe=False)
		vid = eyes.retina.attended['vid']
		hops = [h for h in eyes.retina.hops if h.vid == vid]
		regions = asr.speech_regions(hops)
		assert regions, 'a narrator reads for the whole clip'
		assert sum(b - a for a, b in regions) >= 3.0, regions
	finally:
		await eyes.close()


# -- the pure parts -------------------------------------------------------------------------


def _frames(spec: list[tuple[float, int]]) -> list[FrameSample]:
	"""(media time, flat luma value) -> frames with keyframes everywhere."""
	return [FrameSample(i + 1, 1, t, float(i), bytes([v]) * 256, (v, v, v), True) for i, (t, v) in enumerate(spec)]


def test_keyframe_selection_covers_every_distinct_shot_before_repeating_one():
	# Eight seconds of one shot and one second each of three others.
	spec = [(i / 10, 40) for i in range(80)] + [(8 + i / 10, 120) for i in range(10)]
	spec += [(9 + i / 10, 200) for i in range(10)] + [(10 + i / 10, 250) for i in range(10)]
	frames = _frames(spec)
	chosen = sight.select_keyframes(frames, 4)
	values = sorted({frames[i].luma[0] for i in chosen.indices})
	assert values == [40, 120, 200, 250], values
	assert chosen.coverage > 0.99
	assert chosen.gains == sorted(chosen.gains, reverse=True), 'greedy gains are non-increasing (submodularity)'


def test_a_backward_jump_is_a_loop_only_from_the_end():
	loop = sight.read(_frames([(t / 10, 50) for t in range(60)] + [(t / 10, 50) for t in range(10)]), duration=6.0)
	assert loop.loops and not loop.rewinds
	rewind = sight.read(_frames([(t / 10, 50) for t in range(30)] + [(t / 10, 50) for t in range(10)]), duration=6.0)
	assert rewind.rewinds and not rewind.loops
	restart = sight.read(_frames([(t / 10, 50) for t in range(5)] + [(t / 10, 50) for t in range(20)]), duration=6.0)
	assert not restart.loops and not restart.rewinds, 'a feed rewinding a reel on arrival is neither'


def test_a_flick_lifts_off_while_still_moving_and_a_drag_does_not():
	rng = random.Random(3)
	flick = stroke((200, 800), (200, 200), 180, rng, release=FLICK_RELEASE)
	drag = stroke((200, 800), (200, 200), 180, rng, release=1.0)
	peak = max(release_speed(flick[i : i + 2]) for i in range(len(flick) - 1))
	assert release_speed(flick) > 0.3 * peak, 'momentum comes from the release speed'
	assert release_speed(drag) < 0.1 * peak
	for points in (flick, drag):
		assert _near(points[0][1], 800, 2) and _near(points[-1][1], 200, 2)
		assert all(b[2] > a[2] for a, b in zip(points, points[1:])), 'time moves forward'
	assert min_jerk(0) == 0 and _near(min_jerk(1), 1, 1e-9)


# -- as Claude Code sees it: MCP tools and the ambient hook ---------------------------------


@pytest.fixture
async def mcp_server(tmp_path, monkeypatch):
	import mcp.types as types  # noqa: F401

	from browser_use.mcp.server import BrowserUseServer

	monkeypatch.setenv('BROWSER_USE_EYES_NOW', str(tmp_path / 'now.json'))
	server = BrowserUseServer()
	server.config.setdefault('browser_profile', {}).update(
		{'headless': True, 'user_data_dir': str(tmp_path / 'profile'), 'args': ['--autoplay-policy=no-user-gesture-required']}
	)
	yield server
	await _mcp(server, 'browser_close_all', {})


async def _mcp(server, name: str, arguments: dict):
	import mcp.types as types

	handler = server.server.get_request_handler('tools/call')
	result = await handler.handler(None, types.CallToolRequestParams(name=name, arguments=arguments))
	assert isinstance(result, types.CallToolResult) and not result.is_error, result
	return result.content


def _text(content) -> str:
	import mcp.types as types

	return '\n'.join(b.text for b in content if isinstance(b, types.TextContent))


async def test_claude_code_gets_the_percept_as_an_image_and_moves_the_feed(mcp_server, site):
	import mcp.types as types

	listed = await mcp_server.server.get_request_handler('tools/list').handler(None, types.PaginatedRequestParams())
	names = {t.name for t in listed.tools}
	assert {'eyes_watch', 'eyes_browse', 'eyes_next', 'eyes_tap', 'eyes_swipe', 'eyes_now'} <= names

	await _mcp(mcp_server, 'browser_navigate', {'url': site.url_for('/feed')})
	content = await _mcp(mcp_server, 'eyes_watch', {'seconds': 2.5, 'until': 'time'})
	images = [b for b in content if isinstance(b, types.ImageContent)]
	texts = [b.text for b in content if isinstance(b, types.TextContent)]
	assert len(images) == 1 and images[0].mime_type == 'image/jpeg'
	assert '@first red reel' in texts[0] and re.search(r'tone 3[23]\d Hz', texts[0]), texts[0]

	moved = _text(await _mcp(mcp_server, 'eyes_next', {}))
	assert moved.startswith('Moved by swipe'), moved
	now = _text(await _mcp(mcp_server, 'eyes_now', {}))
	assert '@second green reel' in now, now


def test_the_hook_injects_a_fresh_reading_and_nothing_when_stale(tmp_path):
	import json
	import os
	import sys
	import time

	now = tmp_path / 'now.json'
	env = {**os.environ, 'BROWSER_USE_EYES_NOW': str(now)}
	event = json.dumps({'hook_event_name': 'PostToolUse'})

	def run() -> str:
		return subprocess.run(
			[sys.executable, '-m', 'browser_use.eyes.hook'], input=event, capture_output=True, text=True, env=env, check=True
		).stdout

	now.write_text(json.dumps({'updated': time.time(), 'line': '👁 watching a video "@x" · sound: speech (moderate)'}))
	out = json.loads(run())
	assert out['hookSpecificOutput']['hookEventName'] == 'PostToolUse'
	assert 'sound: speech' in out['hookSpecificOutput']['additionalContext']

	now.write_text(json.dumps({'updated': time.time() - 120, 'line': 'old news'}))
	assert run().strip() == '', 'a stale reading is not reported'


async def test_two_tabs_each_see_only_their_own_video(session, site):
	"""Both retinas call the same binding name; each must keep only its own tab's batches."""
	await session.navigate_to(site.url_for('/still'))
	first = Eyes(session, speech=False, now_path=False)
	await first.open()
	from browser_use.browser.events import NavigateToUrlEvent

	await session.event_bus.dispatch(NavigateToUrlEvent(url=site.url_for('/calib'), new_tab=True))
	await asyncio.sleep(1.0)
	second = Eyes(session, speech=False, now_path=False)
	await second.open()
	try:
		await asyncio.sleep(3.0)
		assert {f.vid for f in first.retina.frames}.isdisjoint({f.vid for f in second.retina.frames})
		assert (first.retina.attended.get('src') or '').endswith('/a.webm'), first.retina.attended
		assert (second.retina.attended.get('src') or '').endswith('/calib.webm'), second.retina.attended
	finally:
		await second.close()
		await first.close()


# -- pages drawn on canvas: no <video>, nothing useful in the DOM -------------------------

CANVAS_PAGE = """<!doctype html><html><body style="margin:0;background:#000">
<canvas id="c" width="800" height="500" style="width:100%;height:100vh;display:block"></canvas>
<section style="height:100vh;background:#1d4ed8"></section><section style="height:100vh;background:#16a34a"></section>
<script>
const g = document.getElementById('c').getContext('2d'); let t = 0;
(function frame() { t += 1; g.fillStyle = `hsl(${(t * 4) % 360}, 90%, 50%)`; g.fillRect(0, 0, 800, 500); requestAnimationFrame(frame) })();
</script></body></html>"""


@pytest.fixture(scope='module')
def canvas_site():
	server = HTTPServer()
	server.start()
	server.expect_request('/canvas').respond_with_data(CANVAS_PAGE, content_type='text/html')
	yield server
	server.stop()


async def test_look_shows_a_canvas_page_as_drawn_when_nothing_is_playing(eyes, session, canvas_site):
	await _open(eyes, session, canvas_site.url_for('/canvas'))
	p = await eyes.look()
	assert p.image and 'no video playing' in p.text, p.text
	img = Image.open(io.BytesIO(p.image)).convert('RGB')
	r, g, b = img.resize((1, 1)).getpixel((0, 0))  # type: ignore[misc]
	assert max(r, g, b) > 100, 'the canvas colour is in the frame, not a blank page'


async def test_scan_covers_the_whole_page_and_notices_what_moves_on_its_own(eyes, session, canvas_site):
	await _open(eyes, session, canvas_site.url_for('/canvas'))
	p = await eyes.scan(max_screens=6, keyframes=4)
	assert p.image and 'screens tall' in p.text, p.text
	assert '3.0 screens tall' in p.text, p.text
	assert 'moves on its own' in p.text, 'the animating canvas at the top is noticed'
	sheet = Image.open(io.BytesIO(p.image)).convert('RGB')
	strip = sheet.resize((4, 1))
	colours: list[tuple[int, int, int]] = [strip.getpixel((i, 0)) for i in range(4)]  # type: ignore[misc]
	assert any(c[2] > 150 and c[0] < 90 for c in colours) and any(c[1] > 120 and c[0] < 90 for c in colours), (
		f'the blue and green sections further down are on the sheet: {colours}'
	)
