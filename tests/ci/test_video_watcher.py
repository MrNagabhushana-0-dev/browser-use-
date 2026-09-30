"""Watching a video from its pixels, for the price of the frames that matter.

The ground truth here is a real WebM with scene cuts at exactly 2s, 4s and 6s and a white
box sliding through every scene. The box is the point: motion inside a shot must not read
as a cut, and a cut must not hide behind it. Nothing is mocked — a real browser seeks a
real video served over HTTP with range support.
"""

import re
import subprocess
from io import BytesIO

import pytest
from PIL import Image
from pytest_httpserver import HTTPServer
from werkzeug.wrappers import Response

from browser_use.browser.events import NavigateToUrlEvent
from browser_use.vision.video import (
	NoVideoError,
	TokenLedger,
	VideoWatcher,
	estimate_image_tokens,
)

CUTS = [2.0, 4.0, 6.0]
DURATION = 8.0
# Two frames at 25fps: how close a detected cut must be to count as found.
TOLERANCE = 0.45

PAGE = """<!DOCTYPE html><html><head><title>Player</title></head>
<body style="margin:0;background:#111">
<video id="v" src="{src}" muted playsinline preload="auto" width="480" height="270"></video>
</body></html>"""


def _ffmpeg() -> str:
	import imageio_ffmpeg

	return imageio_ffmpeg.get_ffmpeg_exe()


def _encode(path, scenes: list[str], seconds_each: float | list[float]) -> None:
	"""A WebM of solid scenes back to back with a white box sliding across all of them."""
	lengths = seconds_each if isinstance(seconds_each, list) else [seconds_each] * len(scenes)
	inputs = []
	for colour, length in zip(scenes, lengths):
		inputs += ['-f', 'lavfi', '-i', f'color=c={colour}:s=480x270:d={length}:r=25']
	inputs += ['-f', 'lavfi', '-i', f'color=c=white:s=40x40:d={sum(lengths)}:r=25']
	concat = ''.join(f'[{i}]' for i in range(len(scenes)))
	graph = f'{concat}concat=n={len(scenes)}:v=1:a=0[bg];[bg][{len(scenes)}]overlay=x=20+t*50:y=110[out]'
	subprocess.run(
		[_ffmpeg(), '-y', '-loglevel', 'error', *inputs, '-filter_complex', graph, '-map', '[out]']
		+ ['-c:v', 'libvpx', '-b:v', '600k', '-g', '25', '-pix_fmt', 'yuv420p', str(path)],
		check=True,
	)


@pytest.fixture(scope='module')
def videos(tmp_path_factory):
	root = tmp_path_factory.mktemp('videos')
	cuts = root / 'cuts.webm'
	_encode(cuts, ['red', '0x00a000', 'blue', '0xdddd00'], 2.0)
	still = root / 'still.webm'
	_encode(still, ['0x808080'], 6.0)
	# Two real scenes, then a blue flash of 0.3s at the very end.
	sliver = root / 'sliver.webm'
	_encode(sliver, ['red', '0x00a000', 'blue'], [2.0, 2.0, 0.3])
	return {'cuts': cuts.read_bytes(), 'still': still.read_bytes(), 'sliver': sliver.read_bytes()}


def _serve(server: HTTPServer, path: str, data: bytes, cors: bool = False) -> None:
	"""Serve bytes with Range support, which a browser needs in order to seek."""

	def handler(request):
		headers = {'Accept-Ranges': 'bytes', 'Content-Type': 'video/webm'}
		if cors:
			headers['Access-Control-Allow-Origin'] = '*'
		match = re.match(r'bytes=(\d+)-(\d*)', request.headers.get('Range', ''))
		if not match:
			return Response(data, headers=headers)
		start = int(match.group(1))
		end = min(int(match.group(2)) if match.group(2) else len(data) - 1, len(data) - 1)
		headers['Content-Range'] = f'bytes {start}-{end}/{len(data)}'
		return Response(data[start : end + 1], status=206, headers=headers)

	server.expect_request(path).respond_with_handler(handler)


@pytest.fixture(scope='module')
def media_origin(videos):
	"""The origin that hosts the video files — a different origin from the page."""
	server = HTTPServer()
	server.start()
	_serve(server, '/cuts.webm', videos['cuts'])
	_serve(server, '/still.webm', videos['still'])
	_serve(server, '/cuts-cors.webm', videos['cuts'], cors=True)
	yield server
	server.stop()


@pytest.fixture(scope='module')
def page_origin(videos):
	"""The origin that hosts the page and, separately, same-origin copies of the video."""
	server = HTTPServer()
	server.start()
	_serve(server, '/cuts.webm', videos['cuts'])
	_serve(server, '/still.webm', videos['still'])
	_serve(server, '/sliver.webm', videos['sliver'])
	server.expect_request('/sliver').respond_with_data(PAGE.format(src='/sliver.webm'), content_type='text/html')
	server.expect_request('/same-origin').respond_with_data(PAGE.format(src='/cuts.webm'), content_type='text/html')
	server.expect_request('/same-origin-still').respond_with_data(PAGE.format(src='/still.webm'), content_type='text/html')
	# A marked overlay sitting on the video's corner, as a token meter would.
	overlay = (
		'<div id="meter" data-bu-overlay style="position:fixed;left:0;top:0;width:200px;height:120px;background:#ff00ff"></div>'
	)
	server.expect_request('/overlaid').respond_with_data(
		PAGE.format(src='/cuts.webm').replace('</body>', overlay + '</body>'), content_type='text/html'
	)
	server.expect_request('/empty').respond_with_data('<html><body><p>no video here</p></body></html>', content_type='text/html')
	yield server
	server.stop()


async def _goto(session, url):
	event = session.event_bus.dispatch(NavigateToUrlEvent(url=url))
	await event
	await event.event_result(raise_if_any=True, raise_if_none=False)


def _assert_cuts_found(found: list[float]) -> None:
	assert len(found) == len(CUTS), f'expected cuts near {CUTS}, got {found}'
	for want, got in zip(CUTS, found):
		assert abs(want - got) <= TOLERANCE, f'cut at {want}s was located at {got}s'


async def test_finds_the_scene_cuts_using_in_page_signatures(browser_session, page_origin):
	await _goto(browser_session, page_origin.url_for('/same-origin'))
	summary = await VideoWatcher(browser_session).watch()

	assert summary.signature_mode == 'in-page', 'a same-origin video needs no screenshots to be compared'
	_assert_cuts_found(summary.cuts)
	assert len(summary.shots) == len(CUTS) + 1
	assert abs(summary.duration - DURATION) < 0.2
	# Keyframes are real decodable images of the video, not placeholders.
	for shot in summary.shots:
		assert Image.open(BytesIO(shot.keyframe)).size[0] > 0


async def test_motion_inside_a_shot_is_not_a_cut(browser_session, page_origin):
	"""A box slides the whole length of a single grey scene. One shot, no cuts."""
	await _goto(browser_session, page_origin.url_for('/same-origin-still'))
	summary = await VideoWatcher(browser_session).watch()

	assert summary.cuts == []
	assert len(summary.shots) == 1


async def test_a_cross_origin_video_falls_back_to_screenshot_signatures(browser_session, page_origin, media_origin):
	"""The browser taints a canvas that has drawn a cross-origin video, so pixels cannot be read
	back in the page. The watcher must notice and still find the same cuts another way."""
	page_origin.expect_request('/cross-origin').respond_with_data(
		PAGE.format(src=media_origin.url_for('/cuts.webm')), content_type='text/html'
	)
	await _goto(browser_session, page_origin.url_for('/cross-origin'))
	summary = await VideoWatcher(browser_session).watch()

	assert summary.signature_mode == 'screenshot'
	_assert_cuts_found(summary.cuts)


async def test_it_takes_far_fewer_samples_than_looking_at_every_moment(browser_session, page_origin):
	"""Uniform sampling fine enough to localise a cut to min_gap costs duration/min_gap samples.
	Bisecting only where neighbours differ costs a coarse grid plus a few halvings per cut."""
	await _goto(browser_session, page_origin.url_for('/same-origin'))
	min_gap = 0.25
	summary = await VideoWatcher(browser_session).watch(min_gap=min_gap)

	uniform = int(DURATION / min_gap)
	assert summary.samples_taken < uniform * 0.75, f'{summary.samples_taken} samples vs {uniform} uniform'


async def test_a_tight_keyframe_budget_keeps_the_biggest_change_and_stops_looking(browser_session, page_origin):
	"""With room for two shots only the single largest change is worth locating. Blue to yellow
	at 6s is the biggest jump in the video, and finding it must not cost a search for the rest."""
	await _goto(browser_session, page_origin.url_for('/same-origin'))
	everything = await VideoWatcher(browser_session).watch(max_frames=8)
	summary = await VideoWatcher(browser_session).watch(max_frames=2)

	assert len(summary.shots) == 2
	assert len(summary.cuts) == 1 and abs(summary.cuts[0] - 6.0) <= TOLERANCE, summary.cuts
	assert summary.shots[0].start == 0.0
	assert summary.shots[-1].end == pytest.approx(summary.duration, abs=0.3)
	assert summary.shots[0].end == summary.shots[1].start  # shots tile the timeline, no gaps
	assert summary.samples_taken < everything.samples_taken, 'a smaller budget must cost fewer samples'


async def test_contact_sheet_is_one_image_carrying_every_keyframe(browser_session, page_origin):
	await _goto(browser_session, page_origin.url_for('/same-origin'))
	summary = await VideoWatcher(browser_session).watch()
	sheet = Image.open(BytesIO(summary.contact_sheet(columns=2, tile_width=240)))

	assert sheet.format == 'JPEG'
	assert sheet.size[0] >= 2 * 240
	# One image, however many shots: that is what makes it cheap to hand to a model.
	tokens = summary.ledger.total_image_tokens
	assert tokens > 0
	assert tokens < estimate_image_tokens(480, 270) * len(summary.shots)


async def test_marked_overlays_are_hidden_for_the_capture_and_restored_after(browser_session, page_origin):
	"""A token meter drawn over the player must not end up in the pixels the model reads."""
	await _goto(browser_session, page_origin.url_for('/overlaid'))
	summary = await VideoWatcher(browser_session).watch()

	for shot in summary.shots:
		keyframe = Image.open(BytesIO(shot.keyframe)).convert('RGB')
		pixel = keyframe.getpixel((20, 20))
		assert isinstance(pixel, tuple)
		r, g, b = pixel[:3]
		assert not (r > 200 and b > 200 and g < 60), f'overlay is baked into the keyframe at {shot.at:.1f}s: {(r, g, b)}'

	cdp = await browser_session.get_or_create_cdp_session(focus=False)
	shown = await cdp.cdp_client.send.Runtime.evaluate(
		params={'expression': "getComputedStyle(document.getElementById('meter')).visibility", 'returnByValue': True},
		session_id=cdp.session_id,
	)
	assert shown['result']['value'] == 'visible', 'the overlay must come back once the capture is done'


async def test_a_sliver_of_a_shot_does_not_spend_a_keyframe(browser_session, page_origin):
	"""A 0.3s flash at the end is a real change and a poor use of one of eight frames. By default
	shots must last `min_shot`; ask for shorter ones and the same flash is found."""
	await _goto(browser_session, page_origin.url_for('/sliver'))
	default = await VideoWatcher(browser_session).watch()
	assert len(default.cuts) == 1 and abs(default.cuts[0] - 2.0) <= TOLERANCE, default.cuts

	await _goto(browser_session, page_origin.url_for('/sliver'))
	fine = await VideoWatcher(browser_session).watch(min_shot=0.2)
	assert len(fine.cuts) == 2 and abs(fine.cuts[1] - 4.0) <= TOLERANCE, fine.cuts


async def test_a_page_without_a_video_says_so(browser_session, page_origin):
	await _goto(browser_session, page_origin.url_for('/empty'))
	with pytest.raises(NoVideoError):
		await VideoWatcher(browser_session).watch()


def test_image_token_estimate_follows_the_published_area_rule():
	# Anthropic documents tokens ~ width*height/750, with large images scaled down first.
	assert estimate_image_tokens(1000, 1000) == round(1000 * 1000 / 750)
	assert estimate_image_tokens(3000, 3000) < 1600  # capped, not 12000
	assert estimate_image_tokens(200, 200) == round(200 * 200 / 750)


def test_ledger_compares_what_was_sent_against_the_naive_alternative():
	ledger = TokenLedger()
	ledger.add_image(800, 450, 'contact sheet')
	ledger.add_text('t=2.0 cut', 'timeline')
	assert ledger.total_image_tokens == estimate_image_tokens(800, 450)
	assert ledger.total > ledger.total_image_tokens
	naive = ledger.naive_screenshot_tokens(seconds=600, width=1280, height=720, every=1.0)
	assert naive == 600 * estimate_image_tokens(1280, 720)
	assert naive > 100 * ledger.total
