"""Seeing a page scroll, from pixels, in a handful of tokens.

The existing pan detector looks for a horizontal shift along one row, because it was built for
side-scrolling games. A reader scrolling a page moves the content vertically, and fast enough
that more than half the cells change between frames, which used to be reported as a scene cut.
These tests use real screenshots of a real page scrolled by a known amount, so the estimate is
checked against the truth and not against itself.
"""

import base64
import random
import re
from io import BytesIO

import pytest
from PIL import Image
from pytest_httpserver import HTTPServer

from browser_use.browser.events import NavigateToUrlEvent
from browser_use.vision import PerceptionStream
from browser_use.vision.perceive import scroll_estimate, vertical_scroll

# Two hundred rows, each with a coloured bar at its own place and width across the middle of
# the page: what a real article looks like to a row-by-row matcher, and what a left-aligned
# column of text, where the middle is blank, does not.
LONG_PAGE = (
	'<!DOCTYPE html><html><body style="margin:0;background:#fff;color:#111;font:20px monospace">'
	+ ''.join(
		f'<div style="position:relative;height:40px;line-height:40px;padding-left:10px">Line {i:03d}'
		f'<div style="position:absolute;top:10px;height:20px;left:{22 + (i * 13) % 26}%;width:{8 + (i * 17) % 40}%;'
		f'background:hsl({(i * 47) % 360},65%,{35 + (i * 11) % 40}%)"></div></div>'
		for i in range(200)
	)
	+ '</body></html>'
)

# Five distinct row types repeating: a scroll by a line or two changes the frame, but the new frame
# fits the old one at several offsets, so how far it moved cannot be told.
PERIODIC_PAGE = (
	'<!DOCTYPE html><html><body style="margin:0;background:#fff">'
	+ ''.join(
		f'<div style="position:relative;height:40px"><div style="position:absolute;top:8px;height:24px;'
		f'left:{24 + (i % 5) * 9}%;width:{10 + (i % 5) * 7}%;background:hsl({(i % 5) * 70},70%,45%)"></div></div>'
		for i in range(200)
	)
	+ '</body></html>'
)

# Every row identical: a scroll here is invisible to any row matcher, and the honest answer is
# "cannot tell", not a number.
IDENTICAL_PAGE = (
	'<!DOCTYPE html><html><body style="margin:0;background:#fff">'
	+ '<div style="height:40px;background:linear-gradient(90deg,#fff,#ccc 50%,#fff)"></div>' * 200
	+ '</body></html>'
)

# A sprite that moves sideways on a fixed page: change, but not a scroll.
SPRITE_PAGE = (
	'<!DOCTYPE html><html><body style="margin:0;background:#fff">'
	'<div id="s" style="position:absolute;top:300px;left:100px;width:120px;height:120px;background:#c0392b"></div>'
	'</body></html>'
)


@pytest.fixture(scope='module')
def site():
	server = HTTPServer()
	server.start()
	server.expect_request('/long').respond_with_data(LONG_PAGE, content_type='text/html')
	server.expect_request('/periodic').respond_with_data(PERIODIC_PAGE, content_type='text/html')
	server.expect_request('/identical').respond_with_data(IDENTICAL_PAGE, content_type='text/html')
	server.expect_request('/sprite').respond_with_data(SPRITE_PAGE, content_type='text/html')
	yield server
	server.stop()


async def _goto(session, url):
	event = session.event_bus.dispatch(NavigateToUrlEvent(url=url))
	await event
	await event.event_result(raise_if_any=True, raise_if_none=False)


async def _js(session, expression: str):
	cdp = await session.get_or_create_cdp_session(focus=False)
	result = await cdp.cdp_client.send.Runtime.evaluate(
		params={'expression': expression, 'returnByValue': True}, session_id=cdp.session_id
	)
	return result['result'].get('value')


async def _frame(session) -> bytes:
	cdp = await session.get_or_create_cdp_session(focus=False)
	shot = await cdp.cdp_client.send.Page.captureScreenshot(params={'format': 'jpeg', 'quality': 70}, session_id=cdp.session_id)
	return base64.b64decode(shot['data'])


async def _scrolled(session, to: int) -> bytes:
	await _js(session, f'window.scrollTo(0, {to})')
	return await _frame(session)


async def test_a_scroll_is_measured_as_a_fraction_of_the_screen_in_the_right_direction(browser_session, site):
	await _goto(browser_session, site.url_for('/long'))
	height = await _js(browser_session, 'window.innerHeight')
	top = await _scrolled(browser_session, 0)
	down = await _scrolled(browser_session, 300)

	moved = vertical_scroll(top, down)
	assert moved is not None and moved > 0, 'scrolling down must read as positive'
	assert moved == pytest.approx(300 / height, abs=0.04), f'scrolled 300px of {height}px, measured {moved:.3f} of a screen'

	back = vertical_scroll(down, top)
	assert back is not None and back == pytest.approx(-300 / height, abs=0.04), f'scrolling back up measured {back}'


async def test_a_page_that_did_not_move_reports_no_scroll(browser_session, site):
	await _goto(browser_session, site.url_for('/long'))
	first = await _scrolled(browser_session, 600)
	second = await _frame(browser_session)

	assert vertical_scroll(first, second) is None


async def test_a_page_that_cannot_reveal_its_scroll_is_reported_as_unknown_not_guessed(browser_session, site):
	"""Identical rows fit every offset equally. A number here would be invented."""
	await _goto(browser_session, site.url_for('/identical'))
	top = await _scrolled(browser_session, 0)
	down = await _scrolled(browser_session, 300)

	assert vertical_scroll(top, down) is None


async def test_a_sprite_sliding_sideways_is_not_a_scroll(browser_session, site):
	await _goto(browser_session, site.url_for('/sprite'))
	before = await _frame(browser_session)
	await _js(browser_session, "document.getElementById('s').style.left = '600px'")
	after = await _frame(browser_session)

	assert vertical_scroll(before, after) is None


async def test_the_stream_calls_a_big_scroll_a_scroll_not_a_scene_cut_and_tracks_position(browser_session, site):
	await _goto(browser_session, site.url_for('/long'))
	stream = PerceptionStream()
	assert stream.observe(await _scrolled(browser_session, 0), at=0.0) is None  # first frame primes it
	line = stream.observe(await _scrolled(browser_session, 400), at=0.5)
	assert line is not None
	assert 'scroll=down' in line and 'scene cut' not in line, line

	second = stream.observe(await _scrolled(browser_session, 800), at=1.0)
	assert second is not None and 'scroll=down' in second
	# Position is cumulative, in screens, so a reader of the stream knows where they are in the page.
	height = await _js(browser_session, 'window.innerHeight')
	assert _position(second) == pytest.approx(800 / height, abs=0.08), second

	back = stream.observe(await _scrolled(browser_session, 400), at=1.5)
	assert back is not None and 'scroll=up' in back, back
	assert _position(back) == pytest.approx(400 / height, abs=0.08), back


def _position(line: str) -> float:
	match = re.search(r'pos=(-?\d+\.\d)h', line)
	assert match, f'no position in {line!r}'
	return float(match.group(1))


# -- the guards, exercised directly -------------------------------------------------------
# These use generated frames because each guard exists for a case a real page only produces by
# accident: a page that repeats itself exactly, and one whose frames are too noisy to match.


def _rows_frame(row_levels: list[int], noise: float = 0.0, seed: int = 0) -> bytes:
	"""A 200px-wide frame with one brightness level per pixel row, optionally noisy."""
	rng = random.Random(seed)
	image = Image.new('L', (200, len(row_levels)))
	pixels = []
	for level in row_levels:
		pixels.extend(max(0, min(255, round(level + (x % 7) * 3 + rng.gauss(0, noise)))) for x in range(200))
	image.putdata(pixels)
	out = BytesIO()
	image.convert('RGB').save(out, format='JPEG', quality=95)
	return out.getvalue()


def test_content_that_repeats_itself_cannot_be_scrolled_by_the_matcher_so_it_refuses():
	"""With a period of 25 rows, a shift of 10 fits as well as a shift of -15 or 35. Choosing one of
	them would be a confident wrong number."""
	rng = random.Random(7)
	pattern = [rng.randrange(20, 235) for _ in range(25)]
	before = _rows_frame([pattern[r % 25] for r in range(400)])
	after = _rows_frame([pattern[(r + 10) % 25] for r in range(400)])

	assert vertical_scroll(before, after) is None


def test_a_match_that_is_barely_better_than_no_movement_is_not_believed():
	"""Most of the frame changed for some other reason (a video playing, a feed updating) and only
	a minority of rows are the old content shifted. The shift is a little closer than standing
	still, but that is not evidence the page scrolled."""
	rng = random.Random(3)
	base = [rng.randrange(20, 235) for _ in range(440)]
	before = _rows_frame(base[:400])
	shifted = base[40:440]
	after = _rows_frame([level if rng.random() < 0.35 else rng.randrange(20, 235) for level in shifted])

	assert vertical_scroll(before, after) is None


def test_the_same_content_shifted_cleanly_is_measured():
	"""The control for the two above: without repetition or noise, the shift is found exactly."""
	rng = random.Random(11)
	base = [rng.randrange(20, 235) for _ in range(440)]
	before = _rows_frame(base[:400])
	after = _rows_frame(base[40:440])

	assert vertical_scroll(before, after) == pytest.approx(40 / 400, abs=0.01)


def test_a_refusal_because_the_content_is_ambiguous_is_told_apart_from_no_scroll():
	"""Both return no distance. The difference is whether the reader should know: an ambiguous
	shift means the page probably scrolled and the position can no longer be trusted."""
	rng = random.Random(7)
	pattern = [rng.randrange(20, 235) for _ in range(25)]
	before = _rows_frame([pattern[r % 25] for r in range(400)])
	shifted = _rows_frame([pattern[(r + 10) % 25] for r in range(400)])

	assert scroll_estimate(before, shifted) == (None, True), 'periodic content: moved, but cannot say how far'
	assert scroll_estimate(before, before) == (None, False), 'identical frames: nothing moved'


async def test_the_stream_says_so_and_stops_vouching_for_its_position_when_it_cannot_tell(browser_session, site):
	await _goto(browser_session, site.url_for('/periodic'))
	stream = PerceptionStream()
	stream.observe(await _scrolled(browser_session, 0), at=0.0)
	line = stream.observe(await _scrolled(browser_session, 120), at=0.5)

	assert line is not None and 'scroll=unknown' in line, line
	assert stream.position_exact is False
	assert line.rstrip().endswith('?'), f'the position must be marked uncertain: {line}'
