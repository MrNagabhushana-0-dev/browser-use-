"""Recording a virtual display to a file that actually opens.

The failure this guards is the quiet one: ffmpeg killed instead of quit leaves an MP4 with no
index, which exists, has a plausible size, and cannot be played. So the assertion is on the
file being readable and the right shape, not on it being there.
"""

import asyncio
import re
import shutil
import subprocess

import pytest

from browser_use.vision.screenrec import RecorderUnavailable, RecordingFailed, _ffmpeg, record_display, virtual_display

pytestmark = pytest.mark.skipif(shutil.which('Xvfb') is None, reason='Xvfb is not installed')


def _probe(path) -> tuple[float, tuple[int, int]]:
	"""Duration in seconds and (width, height), read the way any player would."""
	result = subprocess.run([_ffmpeg(), '-hide_banner', '-i', str(path)], capture_output=True, text=True)
	duration = re.search(r'Duration: (\d+):(\d+):([\d.]+)', result.stderr)
	size = re.search(r'Video: .*?, (\d+)x(\d+)', result.stderr)
	assert duration and size, f'ffmpeg could not read {path}:\n{result.stderr[-400:]}'
	h, m, s = duration.groups()
	return int(h) * 3600 + int(m) * 60 + float(s), (int(size.group(1)), int(size.group(2)))


async def test_a_recording_is_finalized_into_a_playable_file(tmp_path):
	out = tmp_path / 'rec.mp4'
	async with virtual_display(640, 400) as display:
		async with record_display(display, out, 640, 400, fps=10):
			await asyncio.sleep(2.0)

	seconds, size = _probe(out)
	assert size == (640, 400)
	assert 1.0 < seconds < 4.0, f'asked for about 2s, got {seconds}s'


async def test_displays_are_released_when_the_block_exits():
	async with virtual_display(320, 200) as first:
		pass
	async with virtual_display(320, 200) as second:
		# The socket of the first is gone, so its number is free to be handed out again.
		assert second == first


async def test_displays_opened_at_the_same_moment_are_distinct():
	"""Two callers must never be handed the same display, or the first to exit kills the other's."""

	async def open_one():
		async with virtual_display(320, 200) as display:
			await asyncio.sleep(0.5)
			return display

	first, second = await asyncio.gather(open_one(), open_one())
	assert first != second


async def test_a_recording_that_cannot_start_raises_instead_of_producing_nothing(tmp_path):
	"""ffmpeg dying at start-up (here: no such display) used to leave no file and no error."""
	with pytest.raises(RecordingFailed):
		async with record_display(':250', tmp_path / 'never.mp4', 320, 200, fps=10):
			await asyncio.sleep(0.5)


async def test_a_missing_xvfb_is_a_clear_error(monkeypatch):
	monkeypatch.setenv('PATH', '')  # the real condition: Xvfb is simply not findable
	with pytest.raises(RecorderUnavailable):
		async with virtual_display():
			pass
