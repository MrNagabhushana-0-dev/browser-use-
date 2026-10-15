"""Tests for the RecordingWatchdog start/stop API and the `browser-use record` CLI command.

The watchdog drives CDP screencast (`Page.startScreencast`/`stopScreencast`) and
`VideoRecorderService` (imageio+ffmpeg) to produce an MP4. These tests exercise
the full stack against a real headless browser.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

try:
	import imageio.v2 as iio  # type: ignore[import-not-found]

	IMAGEIO_AVAILABLE = True
except ImportError:
	IMAGEIO_AVAILABLE = False

from browser_use.browser.events import NavigateToUrlEvent
from browser_use.browser.profile import BrowserProfile
from browser_use.browser.session import BrowserSession

pytestmark = pytest.mark.skipif(
	not IMAGEIO_AVAILABLE,
	reason='Recording requires the [video] extra: pip install "browser-use[video]"',
)


@pytest.fixture
async def browser_session():
	session = BrowserSession(browser_profile=BrowserProfile(headless=True))
	await session.start()
	yield session
	await session.kill()


@pytest.fixture
def page_url(httpserver):
	httpserver.expect_request('/recpage').respond_with_data(
		"""
		<html>
			<body style='background:#f0f;padding:40px;'>
				<h1 id='title'>Recording test</h1>
				<p>This content should appear in the captured video.</p>
			</body>
		</html>
		""",
		content_type='text/html',
	)
	return httpserver.url_for('/recpage')


async def _drive_browser_briefly(bs: BrowserSession, url: str, ticks: int = 8) -> None:
	"""Navigate + poke the page so screencast emits a few frames."""
	await bs.event_bus.dispatch(NavigateToUrlEvent(url=url, new_tab=False))
	# Screencast emits frames as the page changes; give it enough time to collect some
	for _ in range(ticks):
		await asyncio.sleep(0.15)


async def test_start_stop_recording_produces_video(browser_session: BrowserSession, page_url: str, tmp_path: Path):
	"""start_recording → activity → stop_recording should write a valid MP4."""
	watchdog = browser_session._recording_watchdog
	assert watchdog is not None, 'BrowserSession should always attach a RecordingWatchdog'

	out_path = tmp_path / 'session.mp4'
	assert not watchdog.is_recording

	saved = await watchdog.start_recording(out_path)
	assert saved == out_path
	assert watchdog.is_recording

	await _drive_browser_briefly(browser_session, page_url)

	final = await watchdog.stop_recording()
	assert final == out_path
	assert not watchdog.is_recording
	assert out_path.exists(), 'recording stop should leave a file on disk'
	assert out_path.stat().st_size > 0, 'recorded video must be non-empty'

	# Confirm the file is actually a decodable video with at least one frame.
	reader: Any = iio.get_reader(str(out_path))
	try:
		frame: Any = reader.get_next_data()
		assert frame is not None and frame.size > 0
	finally:
		reader.close()


async def test_a_recording_lasts_as_long_as_it_was_watched_and_ends_on_the_last_page(
	browser_session: BrowserSession, httpserver, tmp_path: Path
):
	"""The screencast sends a frame only when the page repaints. Each one used to become one video frame, so three
	seconds on a still page played for a fraction of a second, and the picture a navigation left behind was lost.
	Frames are now written at the set rate from the latest picture."""
	import numpy as np

	httpserver.expect_request('/magenta').respond_with_data('<body style="background:#f0f">', content_type='text/html')
	httpserver.expect_request('/teal').respond_with_data('<body style="background:#00807f">', content_type='text/html')
	watchdog = browser_session._recording_watchdog
	assert watchdog is not None
	await browser_session.event_bus.dispatch(NavigateToUrlEvent(url=httpserver.url_for('/magenta'), new_tab=False))
	await asyncio.sleep(1.0)
	out = await watchdog.start_recording(tmp_path / 'still.mp4', framerate=10)
	await asyncio.sleep(1.5)
	await browser_session.event_bus.dispatch(NavigateToUrlEvent(url=httpserver.url_for('/teal'), new_tab=False))
	await asyncio.sleep(1.5)
	await watchdog.stop_recording()

	frames = list(iio.mimread(str(out), memtest=False))
	assert 24 <= len(frames) <= 45, f'{len(frames)} frames for about 3 s at 10 fps'
	first, last = frames[0][100, 100].astype(int), frames[-1][100, 100].astype(int)
	assert np.abs(first - (255, 0, 255)).max() < 40, f'first frame is not the magenta page: {first}'
	assert np.abs(last - (0, 128, 127)).max() < 40, f'last frame is not the teal page: {last}'


async def test_a_recording_of_a_page_drawing_few_frames_a_second_lasts_as_long_as_the_session(
	browser_session: BrowserSession, httpserver, tmp_path: Path
):
	"""Found hands-on on a three.js example (WebGL drawn in software, ~1 frame a second): ten minutes recorded made a
	30 s video. The recorder wrote one frame per turn of its clock, and each turn first waited on the page (the
	screencast's acknowledgement, or a capture), ~2 s there. It now writes every frame its clock owes, from the latest
	picture."""
	httpserver.expect_request('/slow').respond_with_data(
		'<body><script>let n = 0; const busy = () => { const end = performance.now() + 400; while (performance.now() < end) {}'
		' document.body.style.background = n++ % 2 ? "#f0f" : "#0ff"; requestAnimationFrame(busy); };'
		' requestAnimationFrame(busy);</script></body>',
		content_type='text/html',
	)
	watchdog = browser_session._recording_watchdog
	assert watchdog is not None
	await browser_session.event_bus.dispatch(NavigateToUrlEvent(url=httpserver.url_for('/slow'), new_tab=False))
	await asyncio.sleep(1.0)
	out = await watchdog.start_recording(tmp_path / 'slow.mp4', framerate=10)
	await asyncio.sleep(5.0)
	await watchdog.stop_recording()
	frames = list(iio.mimread(str(out), memtest=False))
	assert 40 <= len(frames) <= 65, f'{len(frames)} frames for about 5 s at 10 fps'


async def test_start_recording_twice_raises(browser_session: BrowserSession, tmp_path: Path):
	watchdog = browser_session._recording_watchdog
	assert watchdog is not None

	await watchdog.start_recording(tmp_path / 'first.mp4')
	try:
		with pytest.raises(RuntimeError, match='already in progress'):
			await watchdog.start_recording(tmp_path / 'second.mp4')
	finally:
		await watchdog.stop_recording()


async def test_stop_without_start_returns_none(browser_session: BrowserSession):
	watchdog = browser_session._recording_watchdog
	assert watchdog is not None
	assert await watchdog.stop_recording() is None


async def test_on_browser_connected_degrades_gracefully_when_recording_fails(
	browser_session: BrowserSession, tmp_path: Path, monkeypatch
):
	"""If start_recording() raises (e.g. missing [video] deps), profile-driven recording
	must degrade to a warning instead of breaking BrowserSession startup (see PR #4710 review)."""
	from browser_use.browser.events import BrowserConnectedEvent
	from browser_use.browser.watchdogs import recording_watchdog as rw_mod

	watchdog = browser_session._recording_watchdog
	assert watchdog is not None

	async def fake_start_recording(self: Any, *_args: Any, **_kwargs: Any) -> Path:
		raise RuntimeError('simulated missing video deps')

	monkeypatch.setattr(rw_mod.RecordingWatchdog, 'start_recording', fake_start_recording)
	browser_session.browser_profile.record_video_dir = tmp_path

	# Must not raise — watchdog should catch the RuntimeError and just log a warning.
	await watchdog.on_BrowserConnectedEvent(BrowserConnectedEvent(cdp_url=browser_session.cdp_url or ''))
	assert not watchdog.is_recording


async def test_profile_record_video_dir_still_works(page_url: str, tmp_path: Path):
	"""The existing event-driven flow (profile.record_video_dir) must keep working."""
	session = BrowserSession(
		browser_profile=BrowserProfile(headless=True, record_video_dir=tmp_path),
	)
	await session.start()
	try:
		watchdog = session._recording_watchdog
		assert watchdog is not None
		# on_BrowserConnectedEvent should have auto-started recording via the watchdog
		assert watchdog.is_recording, 'profile.record_video_dir should have auto-started recording'
		await _drive_browser_briefly(session, page_url)
	finally:
		await session.kill()

	# After kill, BrowserStopEvent should have finalized the video file into tmp_path
	videos = list(tmp_path.glob('*.mp4'))
	assert videos, f'expected at least one recorded mp4 in {tmp_path}'
	assert videos[0].stat().st_size > 0


async def test_a_recording_ends_on_the_picture_an_animation_stopped_on(
	browser_session: BrowserSession, httpserver, tmp_path: Path
):
	"""Found live on the T-Rex runner: a look during a recording showed a frame from before the game ended. Chrome's
	screencast drops the repaint that ends an animation (it is skipped while frames are in flight, and a page that
	then stands still sends no other), and the recorder's full-size frames are in flight most of the time. So the
	video ended on the animation's last streamed frame, not where it stopped. A quiet stream is now topped up with
	one capture of the page as it is."""
	import imageio.v3 as iio
	import numpy as np

	httpserver.expect_request('/settles').respond_with_data(
		'<!doctype html><body style="margin:0;background:#fff"><canvas id="c" width="320" height="180"'
		' style="display:block;width:100vw;height:100vh"></canvas><script>'
		'const g = document.getElementById("c").getContext("2d"); let t0 = null;'
		'function frame(now) { t0 = t0 ?? now; const t = now - t0; g.fillStyle = t < 1500 ? `hsl(${t / 4}, 80%, 50%)` : "#0a0";'
		' g.fillRect(0, 0, 320, 180); if (t < 1500) requestAnimationFrame(frame); } requestAnimationFrame(frame);'
		'</script></body>',
		content_type='text/html',
	)
	watchdog = browser_session._recording_watchdog
	assert watchdog is not None
	out = await watchdog.start_recording(tmp_path / 'settles.mp4', framerate=10)
	await browser_session.event_bus.dispatch(NavigateToUrlEvent(url=httpserver.url_for('/settles'), new_tab=False))
	await asyncio.sleep(3.5)
	await watchdog.stop_recording()
	last = list(iio.imiter(str(out)))[-1]
	pixel = last[last.shape[0] // 2, last.shape[1] // 2].astype(int)
	assert np.abs(pixel - (0, 170, 0)).max() < 50, f'the video ends on {pixel}, not the green the animation stopped on'
