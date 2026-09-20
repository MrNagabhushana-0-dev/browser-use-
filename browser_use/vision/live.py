"""Watch a page over time instead of guessing when to screenshot it.

A single screenshot answers "what does this look like now", which is the wrong question
for anything that moves: a video, a feed loading, a transition, a spinner that resolves
into either a result or an error. Ask it too early and you capture a skeleton; too late
and you miss what happened in between. The usual workaround — screenshot in a loop — is
the most expensive thing an agent can do, because most frames are identical and every one
of them costs full image tokens.

So: stream frames continuously over CDP (cheap, they never leave the process), and spend
tokens only on the ones that differ. A ten second watch of a static page yields one
keyframe. Ten seconds of a video yields a handful that actually show different moments.

Frames are compared by an 8x8 RGB thumbnail, differenced channel-wise and normalised to
0-100. The obvious choice, average hash, is wrong for this job: it thresholds each pixel
against the frame's own mean, so a full-viewport colour change — a video cutting to a new
scene, a page swapping its theme — is invisible to it. A thumbnail difference sees that,
while still ignoring compression noise and a blinking caret.
"""

import asyncio
import base64
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from io import BytesIO
from typing import TYPE_CHECKING

if TYPE_CHECKING:
	from browser_use.browser.session import BrowserSession

logger = logging.getLogger(__name__)

# Frames buffered before the oldest is dropped. At ~10fps this is about 30 seconds.
DEFAULT_BUFFER = 300

# Thumbnail difference, 0-100, above which two frames count as "different". Around 4
# catches a scroll, a navigation or a scene cut; below 2 it fires on JPEG noise alone.
CHANGE_THRESHOLD = 4

# Thumbnail edge length. Small enough that layout noise averages away, large enough that a
# change confined to one region of the page still moves the number.
_THUMB = 8


def frame_signature(jpeg_bytes: bytes) -> bytes:
	"""An 8x8 RGB thumbnail of a frame, or b'' if it cannot be decoded."""
	try:
		from PIL import Image

		image = Image.open(BytesIO(jpeg_bytes)).convert('RGB').resize((_THUMB, _THUMB), Image.Resampling.BILINEAR)
	except Exception:
		return b''
	return image.tobytes()


def signature_distance(a: bytes, b: bytes) -> int:
	"""Mean absolute channel difference between two signatures, normalised to 0-100."""
	if not a or not b or len(a) != len(b):
		return 0
	total = sum(abs(x - y) for x, y in zip(a, b))
	return round(total / len(a) * 100 / 255)


@dataclass
class Frame:
	"""One captured moment."""

	at: float  # seconds since the watch began
	data: bytes = field(repr=False)
	signature: bytes = field(default=b'', repr=False)

	def to_base64(self) -> str:
		return base64.b64encode(self.data).decode()


@dataclass
class WatchResult:
	"""What happened during a watch, and the few frames worth looking at."""

	seconds: float
	frames_captured: int
	keyframes: list[Frame]
	# Change score per captured frame, so a caller can see *when* things moved even for
	# frames it chose not to keep.
	motion: list[int] = field(default_factory=list)

	@property
	def changed(self) -> bool:
		return len(self.keyframes) > 1

	def describe(self) -> str:
		if not self.frames_captured:
			return 'Captured nothing: the page produced no frames.'
		if not self.changed:
			return f'Watched {self.seconds:.1f}s over {self.frames_captured} frames: the page was static.'
		moments = ', '.join(f'{f.at:.1f}s' for f in self.keyframes)
		return (
			f'Watched {self.seconds:.1f}s over {self.frames_captured} frames and kept '
			f'{len(self.keyframes)} that differ, at {moments}.'
		)


class LiveView:
	"""A running screencast of one target, with a rolling buffer of recent frames."""

	def __init__(self, browser_session: 'BrowserSession', buffer: int = DEFAULT_BUFFER) -> None:
		self.browser_session = browser_session
		self._frames: deque[Frame] = deque(maxlen=buffer)
		self._started_at: float = 0.0
		self._session_id: str | None = None
		self._running = False

	@property
	def logger(self):
		return self.browser_session.logger

	@property
	def frames(self) -> list[Frame]:
		return list(self._frames)

	async def start(self, target_id=None, max_width: int = 640, quality: int = 60, every_nth: int = 1) -> None:
		"""Begin streaming frames.

		Frames are capped in width and quality on the browser side: they exist to be
		compared and occasionally shown, not archived, and a full-resolution stream would
		spend real bandwidth on frames that are about to be discarded.
		"""
		if self._running:
			return
		cdp = await self.browser_session.get_or_create_cdp_session(target_id, focus=False)
		self._session_id = cdp.session_id
		self._frames.clear()
		self._started_at = time.monotonic()

		self.browser_session.cdp_client.register.Page.screencastFrame(self._on_frame)
		await cdp.cdp_client.send.Page.startScreencast(
			params={
				'format': 'jpeg',
				'quality': quality,
				'maxWidth': max_width,
				'everyNthFrame': every_nth,
			},
			session_id=cdp.session_id,
		)
		self._running = True
		self.logger.debug(f'🎥 Live view started on {cdp.target_id[-4:]}')

	async def stop(self) -> None:
		if not self._running:
			return
		self._running = False
		try:
			await self.browser_session.cdp_client.send.Page.stopScreencast(session_id=self._session_id)
		except Exception as e:
			self.logger.debug(f'🎥 Live view stop failed: {type(e).__name__}: {e}')

	def _on_frame(self, event, session_id: str | None) -> None:
		"""Synchronous CDP callback: buffer the frame and acknowledge it.

		The acknowledgement is not optional. Chrome sends the next frame only once the
		previous one is acked, so dropping it silently freezes the stream after one frame.
		"""
		if not self._running or (self._session_id and session_id != self._session_id):
			return
		try:
			data = base64.b64decode(event['data'])
		except Exception:
			return
		self._frames.append(Frame(at=time.monotonic() - self._started_at, data=data, signature=frame_signature(data)))

		from browser_use.utils import create_task_with_error_handling

		create_task_with_error_handling(
			self._ack(event, session_id),
			name='live_view_ack',
			logger_instance=self.logger,
			suppress_exceptions=True,
		)

	async def _ack(self, event, session_id: str | None) -> None:
		try:
			await self.browser_session.cdp_client.send.Page.screencastFrameAck(
				params={'sessionId': event['sessionId']}, session_id=session_id
			)
		except Exception as e:
			self.logger.debug(f'🎥 Frame ack failed: {type(e).__name__}: {e}')

	def keyframes(self, max_keyframes: int = 6, threshold: int = CHANGE_THRESHOLD) -> tuple[list[Frame], list[int]]:
		"""The frames worth looking at, and the per-frame change scores.

		Always keeps the first frame — "what it looked like when I started" is a real
		answer — then any frame that differs enough from the last one kept. If more
		survive than asked for, keep the biggest changes, restored to time order.
		"""
		frames = self.frames
		if not frames:
			return [], []

		motion: list[int] = [0]
		kept: list[tuple[int, Frame]] = [(0, frames[0])]
		last_kept = frames[0]
		for frame in frames[1:]:
			score = signature_distance(frame.signature, last_kept.signature)
			motion.append(score)
			if score >= threshold:
				kept.append((score, frame))
				last_kept = frame

		if len(kept) > max_keyframes:
			head = kept[0]
			rest = sorted(kept[1:], key=lambda pair: pair[0], reverse=True)[: max_keyframes - 1]
			kept = [head, *sorted(rest, key=lambda pair: pair[1].at)]
		return [frame for _, frame in kept], motion

	async def watch(
		self,
		seconds: float = 5.0,
		max_keyframes: int = 6,
		target_id=None,
		threshold: int = CHANGE_THRESHOLD,
	) -> WatchResult:
		"""Watch the page for a while and return only the moments that differ."""
		assert seconds > 0, 'watch() needs a positive duration'
		already_running = self._running
		if not already_running:
			await self.start(target_id=target_id)
		else:
			self._frames.clear()
			self._started_at = time.monotonic()

		await asyncio.sleep(seconds)
		captured = len(self._frames)
		keyframes, motion = self.keyframes(max_keyframes=max_keyframes, threshold=threshold)

		if not already_running:
			await self.stop()
		return WatchResult(seconds=seconds, frames_captured=captured, keyframes=keyframes, motion=motion)
