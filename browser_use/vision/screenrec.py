"""Record a virtual display to a video file, so a headful run can be watched afterwards.

Headless Chrome has no screen, and a screencast of a single tab has no browser around it. To
show an agent working the way a person would see it — the window, the page, the cursor moving
— the browser runs headful on a virtual X display (Xvfb) and ffmpeg records the display.

Two details that cost a failed recording if missed:

- ffmpeg finishes an MP4 by writing its index at the very end, on a clean quit. Killing it
  leaves a file most players will not open. So stopping writes `q` to ffmpeg's stdin and
  waits, and only escalates to a signal if it does not exit.
- The recorder has to outlive the thing being recorded by a moment and start before it, or
  the first and last seconds are missing. It is a context manager for that reason.
"""

import asyncio
import contextlib
import logging
import os
import shutil
from collections.abc import AsyncIterator
from pathlib import Path

logger = logging.getLogger(__name__)

# How long ffmpeg gets to write its index after being asked to quit.
_FINALIZE_TIMEOUT = 15.0
_DISPLAY_SOCKET_DIR = Path('/tmp/.X11-unix')


class RecorderUnavailable(RuntimeError):
	"""A required binary (Xvfb, ffmpeg) is missing."""


def _ffmpeg() -> str:
	if found := shutil.which('ffmpeg'):
		return found
	try:
		import imageio_ffmpeg

		return imageio_ffmpeg.get_ffmpeg_exe()
	except Exception as e:
		raise RecorderUnavailable(f'No ffmpeg binary found ({type(e).__name__}: {e})') from e


def _free_display() -> int:
	for number in range(99, 140):
		if not (_DISPLAY_SOCKET_DIR / f'X{number}').exists() and not Path(f'/tmp/.X{number}-lock').exists():
			return number
	raise RecorderUnavailable('No free X display number between :99 and :139')


@contextlib.asynccontextmanager
async def virtual_display(width: int = 1280, height: int = 800) -> AsyncIterator[str]:
	"""Run an Xvfb display for the duration of the block and yield its name, e.g. ':99'."""
	xvfb = shutil.which('Xvfb')
	if xvfb is None:
		raise RecorderUnavailable('Xvfb is not installed')
	number = _free_display()
	process = await asyncio.create_subprocess_exec(
		xvfb,
		f':{number}',
		'-screen',
		'0',
		f'{width}x{height}x24',
		'-nolisten',
		'tcp',
		stdout=asyncio.subprocess.DEVNULL,
		stderr=asyncio.subprocess.DEVNULL,
	)
	try:
		for _ in range(100):
			if (_DISPLAY_SOCKET_DIR / f'X{number}').exists():
				break
			if process.returncode is not None:
				raise RecorderUnavailable(f'Xvfb exited immediately with code {process.returncode}')
			await asyncio.sleep(0.05)
		else:
			raise RecorderUnavailable('Xvfb did not open its display socket')
		yield f':{number}'
	finally:
		if process.returncode is None:
			process.terminate()
			try:
				await asyncio.wait_for(process.wait(), timeout=5.0)
			except TimeoutError:
				process.kill()
				await process.wait()


@contextlib.asynccontextmanager
async def record_display(display: str, output: Path | str, width: int, height: int, fps: int = 15) -> AsyncIterator[Path]:
	"""Record `display` to an MP4 until the block exits, then finalize the file."""
	output = Path(output)
	output.parent.mkdir(parents=True, exist_ok=True)
	process = await asyncio.create_subprocess_exec(
		_ffmpeg(),
		'-y',
		'-loglevel',
		'error',
		'-f',
		'x11grab',
		# The X server's own cursor never moves for synthetic CDP input, so grabbing it would
		# paint a second, motionless pointer next to the one the overlay draws in the page.
		'-draw_mouse',
		'0',
		'-framerate',
		str(fps),
		'-video_size',
		f'{width}x{height}',
		'-i',
		display,
		'-c:v',
		'libx264',
		'-preset',
		'veryfast',
		'-crf',
		'30',
		'-pix_fmt',
		'yuv420p',
		'-movflags',
		'+faststart',
		str(output),
		stdin=asyncio.subprocess.PIPE,
		stdout=asyncio.subprocess.DEVNULL,
		stderr=asyncio.subprocess.PIPE,
		env={**os.environ, 'DISPLAY': display},
	)
	try:
		yield output
	finally:
		await _finalize(process)


async def _finalize(process: 'asyncio.subprocess.Process') -> None:
	if process.returncode is not None:
		return
	try:
		assert process.stdin is not None
		process.stdin.write(b'q')
		await process.stdin.drain()
		process.stdin.close()
	except (BrokenPipeError, ConnectionResetError):
		pass
	try:
		await asyncio.wait_for(process.wait(), timeout=_FINALIZE_TIMEOUT)
		return
	except TimeoutError:
		logger.warning('🎬 ffmpeg did not finish writing the recording in time; interrupting it')
	process.terminate()
	try:
		await asyncio.wait_for(process.wait(), timeout=5.0)
	except TimeoutError:
		process.kill()
		await process.wait()
