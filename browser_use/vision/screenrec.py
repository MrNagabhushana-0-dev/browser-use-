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
_DISPLAY_START_TIMEOUT = 10.0
# Long enough for ffmpeg to reject an unreachable display, which it does immediately.
# How long ffmpeg may take to capture its first frame before the recording is called failed
_FIRST_FRAME_TIMEOUT = 15.0


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


class RecordingFailed(RuntimeError):
	"""ffmpeg could not record, or finished without producing a file."""


async def _read_display_number(read_fd: int) -> int:
	"""The display number Xvfb chose, written to a pipe once the server is ready to accept clients."""
	loop = asyncio.get_running_loop()
	reader = asyncio.StreamReader()
	transport, _ = await loop.connect_read_pipe(
		lambda: asyncio.StreamReaderProtocol(reader), os.fdopen(read_fd, 'rb', buffering=0)
	)
	try:
		line = await asyncio.wait_for(reader.readline(), timeout=_DISPLAY_START_TIMEOUT)
	except TimeoutError as e:
		raise RecorderUnavailable('Xvfb did not report a display in time') from e
	finally:
		transport.close()
	if not line.strip():
		raise RecorderUnavailable('Xvfb exited before opening a display')
	return int(line)


@contextlib.asynccontextmanager
async def virtual_display(width: int = 1280, height: int = 800) -> AsyncIterator[str]:
	"""Run an Xvfb display for the duration of the block and yield its name, e.g. ':99'.

	Xvfb picks the display number itself (`-displayfd`) and reports it only once it is ready.
	Choosing a free number here and starting Xvfb on it is a race: two callers at the same
	moment pick the same number, the loser's server dies, and its caller quietly talks to the
	winner's display, which the winner tears down when it exits.
	"""
	xvfb = shutil.which('Xvfb')
	if xvfb is None:
		raise RecorderUnavailable('Xvfb is not installed')
	read_fd, write_fd = os.pipe()
	try:
		process = await asyncio.create_subprocess_exec(
			xvfb,
			'-displayfd',
			str(write_fd),
			'-screen',
			'0',
			f'{width}x{height}x24',
			'-nolisten',
			'tcp',
			stdout=asyncio.subprocess.DEVNULL,
			stderr=asyncio.subprocess.DEVNULL,
			pass_fds=(write_fd,),
		)
	except BaseException:
		os.close(read_fd)
		raise
	finally:
		os.close(write_fd)  # the child holds its own copy; ours must close for EOF to mean 'Xvfb died'
	try:
		number = await _read_display_number(read_fd)
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
		'-progress',
		'pipe:1',  # frame counts on stdout: the recording begins once the first frame is in
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
		stdout=asyncio.subprocess.PIPE,
		stderr=asyncio.subprocess.PIPE,
		env={**os.environ, 'DISPLAY': display},
	)
	# Not a fixed grace: under load ffmpeg can take longer than any guess to begin capturing, and a block left before
	# that came back with no frame at all (found in a full test run)
	captured = asyncio.Event()
	follow = asyncio.create_task(_follow_progress(process, captured))
	waits = [asyncio.create_task(captured.wait()), asyncio.create_task(process.wait())]
	await asyncio.wait(waits, timeout=_FIRST_FRAME_TIMEOUT, return_when=asyncio.FIRST_COMPLETED)
	for wait in waits:
		wait.cancel()
	if process.returncode is not None:
		follow.cancel()
		raise RecordingFailed(f'ffmpeg exited at start-up with code {process.returncode}: {await _stderr_tail(process)}')
	if not captured.is_set():
		await _finalize(process)
		follow.cancel()
		raise RecordingFailed(f'ffmpeg captured no frame of {display} within {_FIRST_FRAME_TIMEOUT:.0f}s')
	try:
		yield output
	except BaseException:
		# Whatever is unwinding is the more important error; still stop ffmpeg cleanly.
		await _finalize(process)
		raise
	finally:
		follow.cancel()
	await _finalize(process)
	if process.returncode != 0 or not output.exists() or output.stat().st_size == 0:
		raise RecordingFailed(f'ffmpeg finished with code {process.returncode} and no usable file: {await _stderr_tail(process)}')


async def _stderr_tail(process: 'asyncio.subprocess.Process', limit: int = 400) -> str:
	if process.stderr is None:
		return ''
	try:
		data = await asyncio.wait_for(process.stderr.read(), timeout=2.0)
	except TimeoutError:
		return ''
	return data.decode(errors='replace').strip()[-limit:]


async def _follow_progress(process: 'asyncio.subprocess.Process', captured: asyncio.Event) -> None:
	"""Read ffmpeg's -progress lines (and keep its stdout drained) and set `captured` at its first frame."""
	assert process.stdout is not None
	while line := await process.stdout.readline():
		key, _, value = line.decode(errors='replace').strip().partition('=')
		if key == 'frame' and value.isdigit() and int(value) >= 1:
			captured.set()


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
