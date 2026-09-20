"""Watching a page over time, for the price of the frames that actually differ.

A single screenshot is the wrong instrument for anything that moves, and screenshotting
in a loop is the most expensive thing an agent can do. These tests pin the bargain: frames
stream continuously and cost nothing, and only the ones that differ are kept.
"""

import pytest
from pytest_httpserver import HTTPServer

from browser_use.browser.events import NavigateToUrlEvent
from browser_use.vision import LiveView, frame_signature, signature_distance

STATIC_PAGE = (
	'<!DOCTYPE html><html><head><title>Still</title></head>'
	'<body style="margin:0;background:#222;color:#eee;font:48px sans-serif">'
	'<div style="padding:80px">Nothing happens here</div></body></html>'
)

# Repaints the whole viewport on a timer, so consecutive frames genuinely differ.
ANIMATED_PAGE = """<!DOCTYPE html>
<html><head><title>Moving</title></head>
<body style="margin:0">
	<div id="stage" style="width:100vw;height:100vh;font:64px sans-serif;color:#fff;
	     display:flex;align-items:center;justify-content:center;background:#000">0</div>
<script>
	const colours = ['#c0392b', '#27ae60', '#2980b9', '#8e44ad', '#f39c12', '#16a085'];
	let i = 0;
	setInterval(() => {
		i++;
		const s = document.getElementById('stage');
		s.style.background = colours[i % colours.length];
		s.textContent = String(i);
	}, 600);
</script>
</body></html>"""


@pytest.fixture(scope='module')
def motion_server():
	server = HTTPServer()
	server.start()
	server.expect_request('/still').respond_with_data(STATIC_PAGE, content_type='text/html')
	server.expect_request('/moving').respond_with_data(ANIMATED_PAGE, content_type='text/html')
	yield server
	server.stop()


async def _goto(session, url):
	event = session.event_bus.dispatch(NavigateToUrlEvent(url=url))
	await event
	await event.event_result(raise_if_any=True, raise_if_none=False)


def test_the_metric_sees_colour_changes_not_just_structure():
	"""The reason this is a thumbnail difference and not an average hash.

	Average hash thresholds every pixel against the frame's own mean, so a solid black
	frame and a solid white one hash identically and a full-viewport scene cut reads as no
	change at all.
	"""
	from io import BytesIO

	from PIL import Image

	def jpeg(colour):
		buf = BytesIO()
		Image.new('RGB', (64, 64), colour).save(buf, format='JPEG')
		return buf.getvalue()

	black = frame_signature(jpeg((0, 0, 0)))
	white = frame_signature(jpeg((255, 255, 255)))
	red = frame_signature(jpeg((200, 20, 20)))

	assert signature_distance(black, black) == 0
	assert signature_distance(black, white) > 90, 'a full repaint must register as a large change'
	assert signature_distance(black, red) > 10, 'a colour change must register at all'

	assert frame_signature(b'not a jpeg at all') == b'', 'undecodable frames must not raise'
	assert signature_distance(b'', black) == 0, 'a missing signature compares as no change'


async def test_frames_actually_stream(browser_session, motion_server):
	"""More than one frame means the acknowledgement loop is alive.

	Chrome sends the next frame only after the previous one is acked, so a missing ack
	looks exactly like a working stream that happens to be frozen on frame one.
	"""
	await _goto(browser_session, motion_server.url_for('/moving'))
	view = LiveView(browser_session)

	result = await view.watch(seconds=3.0)
	assert result.frames_captured > 1, f'stream froze after {result.frames_captured} frame(s)'


async def test_a_static_page_costs_one_keyframe(browser_session, motion_server):
	"""The whole point: watching something that does not move must be nearly free."""
	await _goto(browser_session, motion_server.url_for('/still'))
	view = LiveView(browser_session)

	result = await view.watch(seconds=3.0)
	assert result.frames_captured >= 1
	assert len(result.keyframes) == 1, f'a still page yielded {len(result.keyframes)} keyframes'
	assert result.changed is False
	assert 'static' in result.describe()


async def test_a_moving_page_yields_the_moments_that_differ(browser_session, motion_server):
	await _goto(browser_session, motion_server.url_for('/moving'))
	view = LiveView(browser_session)

	result = await view.watch(seconds=5.0, max_keyframes=6)

	assert result.changed, result.describe()
	assert 2 <= len(result.keyframes) <= 6
	# Keyframes are distinct moments, in order, not near-duplicates.
	times = [f.at for f in result.keyframes]
	assert times == sorted(times)
	signatures = [f.signature for f in result.keyframes]
	assert len(set(signatures)) == len(signatures), 'keyframes should not repeat the same image'
	assert all(f.data.startswith(b'\xff\xd8') for f in result.keyframes), 'frames should be JPEG'


async def test_the_keyframe_budget_is_respected(browser_session, motion_server):
	"""A busy page must not be able to spend unbounded image tokens."""
	await _goto(browser_session, motion_server.url_for('/moving'))
	view = LiveView(browser_session)

	result = await view.watch(seconds=5.0, max_keyframes=3)
	assert len(result.keyframes) <= 3
	# The opening frame is always kept: "what it looked like when I arrived" is an answer.
	assert result.keyframes[0].at == min(f.at for f in result.keyframes)


async def test_watching_is_cheaper_than_screenshotting_the_same_span(browser_session, motion_server):
	"""Measured, not asserted by assumption."""
	await _goto(browser_session, motion_server.url_for('/moving'))
	view = LiveView(browser_session)

	result = await view.watch(seconds=5.0, max_keyframes=6)
	kept_bytes = sum(len(f.data) for f in result.keyframes)
	all_bytes = sum(len(f.data) for f in view.frames)

	print(
		f'\ncaptured {result.frames_captured} frames ({all_bytes // 1024}KB), kept {len(result.keyframes)} ({kept_bytes // 1024}KB)'
	)
	assert kept_bytes < all_bytes, 'keyframe selection should discard most frames'


async def test_the_agent_action_returns_frames_as_images(browser_session, motion_server):
	"""End to end through the registry, the way the agent invokes it."""
	from browser_use.tools.service import Tools

	await _goto(browser_session, motion_server.url_for('/moving'))
	tools = Tools()

	result = await tools.registry.execute_action(
		'watch_page',
		{'seconds': 3.0, 'reason': 'see whether the counter advances'},
		browser_session=browser_session,
	)
	assert result.error is None
	assert result.images, 'keyframes should reach the model as images'
	assert all(image['data'] for image in result.images)
	assert result.extracted_content and 'Watched' in result.extracted_content


async def test_a_watch_cannot_park_the_run(browser_session, motion_server):
	"""The duration is clamped, so a model cannot ask for ten minutes."""
	await _goto(browser_session, motion_server.url_for('/still'))
	import time as _time

	from browser_use.tools.service import Tools

	started = _time.monotonic()
	await Tools().registry.execute_action(
		'watch_page',
		{'seconds': 600.0, 'reason': 'try to stall'},
		browser_session=browser_session,
	)
	assert _time.monotonic() - started < 25, 'watch_page ignored its upper bound'
