"""Recording Watchdog for Browser Use Sessions."""

import asyncio
import time
from pathlib import Path
from typing import Any, ClassVar

from bubus import BaseEvent
from cdp_use.cdp.page.events import ScreencastFrameEvent
from pydantic import PrivateAttr
from uuid_extensions import uuid7str

from browser_use.browser.events import AgentFocusChangedEvent, BrowserConnectedEvent, BrowserStopEvent
from browser_use.browser.profile import ViewportSize
from browser_use.browser.video_recorder import VideoRecorderService
from browser_use.browser.watchdog_base import BaseWatchdog

# A screencast quiet for this long after a frame is topped up with one capture of the page as it is
QUIET_TOP_UP_S = 0.3


class RecordingWatchdog(BaseWatchdog):
	"""
	Manages video recording of a browser session using CDP screencasting.
	"""

	LISTENS_TO: ClassVar[list[type[BaseEvent]]] = [BrowserConnectedEvent, BrowserStopEvent, AgentFocusChangedEvent]
	EMITS: ClassVar[list[type[BaseEvent]]] = []

	_recorder: VideoRecorderService | None = PrivateAttr(default=None)
	_ticker: asyncio.Task | None = PrivateAttr(default=None)
	_current_session_id: str | None = PrivateAttr(default=None)
	_screencast_params: dict[str, Any] | None = PrivateAttr(default=None)
	# The newest screencast frame as sent (base64): a view that joins this stream starts from it, since a still page
	# sends no further frame
	latest_frame: str | None = None
	# The newest frame's acknowledgement, held back until the ticker has used it: the browser sends the next frame only
	# once the last is acknowledged, so the stream runs at the video's frame rate, not at every repaint of a 60 fps page
	# (each one encoded by the browser and parsed on the event loop: that held the reflexes' input up ~50 ms)
	_unacked: tuple[int, str | None] | None = PrivateAttr(default=None)
	# When the newest frame arrived, and whether the page was captured since: Chrome's screencast drops the repaint
	# that ends an animation (skipped while frames are in flight; a page that then stands still sends no other), so a
	# stream gone quiet is topped up with one capture of the page as it is. Found live: a recorded game's video, and
	# looks sharing its stream, ended on a frame from before the game stopped.
	_last_frame_at: float = PrivateAttr(default=0.0)
	_topped_up: bool = PrivateAttr(default=True)

	async def on_BrowserConnectedEvent(self, event: BrowserConnectedEvent) -> None:
		"""
		Starts video recording if it is configured in the browser profile.
		"""
		profile = self.browser_session.browser_profile
		if not profile.record_video_dir:
			return

		video_format = getattr(profile, 'record_video_format', 'mp4').strip('.')
		output_path = Path(profile.record_video_dir) / f'{uuid7str()}.{video_format}'
		try:
			await self.start_recording(output_path, size=profile.record_video_size, framerate=profile.record_video_framerate)
		except RuntimeError as e:
			# Preserve prior graceful degradation: a session configured with record_video_dir
			# should not fail startup when video deps are missing or viewport detection fails.
			self.logger.warning(f'Skipping video recording: {e}')

	async def start_recording(
		self,
		output_path: Path,
		size: ViewportSize | None = None,
		framerate: int | None = None,
	) -> Path:
		"""
		Begin recording the current session to `output_path`. Safe to call at any time
		after the browser has connected.

		Returns the resolved output path. Raises RuntimeError if recording is already active
		or if the viewport size could not be determined.
		"""
		if self._recorder is not None:
			raise RuntimeError(f'Recording already in progress (output: {self._recorder.output_path})')

		if size is None:
			self.logger.debug('record size not specified, detecting viewport size...')
			size = await self._get_current_viewport_size()
		if not size:
			raise RuntimeError('Cannot start video recording: viewport size could not be determined.')

		if framerate is None:
			framerate = self.browser_session.browser_profile.record_video_framerate

		output_path = Path(output_path)
		self.logger.debug(f'Initializing video recorder → {output_path}')
		recorder = VideoRecorderService(output_path=output_path, size=size, framerate=framerate)
		recorder.start()
		if not recorder._is_active:
			raise RuntimeError(
				'Failed to initialize video recorder — ensure optional deps are installed (`pip install "browser-use[video]"`).'
			)

		self._recorder = recorder
		self.browser_session.cdp_client.register.Page.screencastFrame(self.on_screencastFrame)
		self._screencast_params = {
			# JPEG, not PNG: the browser encodes and we decode every repaint, and a lossless frame of the whole viewport
			# costs tens of milliseconds each way for a video that h264 compresses lossily anyway
			'format': 'jpeg',
			'quality': 85,
			'maxWidth': size['width'],
			'maxHeight': size['height'],
			'everyNthFrame': 1,
		}
		await self._start_screencast()
		self._ticker = asyncio.create_task(self._tick(recorder), name='video_recorder_tick')
		return output_path

	async def _tick(self, recorder: VideoRecorderService) -> None:
		"""Write the latest picture at the frame rate, on a clock rather than per screencast frame, so the video lasts
		as long as the session did."""
		loop = asyncio.get_running_loop()
		period = 1.0 / max(1, recorder.framerate)
		due = loop.time()
		while True:
			await asyncio.to_thread(recorder.tick)  # decoding and encoding stay off the event loop
			if self._unacked is not None:
				(frame, session_id), self._unacked = self._unacked, None
				await self._ack(frame, session_id)
			elif not self._topped_up and time.monotonic() - self._last_frame_at > QUIET_TOP_UP_S:
				await self._top_up(recorder)
			due += period
			await asyncio.sleep(max(0.0, due - loop.time()))

	async def stop_recording(self) -> Path | None:
		"""
		Stop any in-progress recording and finalize the output file.

		Returns the path of the saved video, or None if no recording was active.
		"""
		if not self._recorder:
			return None

		recorder = self._recorder
		session_id = self._current_session_id
		if self._ticker is not None:
			self._ticker.cancel()
			self._ticker = None
		await asyncio.to_thread(recorder.tick)  # the last picture closes the video
		self._recorder = None
		self._current_session_id = None
		self.latest_frame = None
		self._screencast_params = None

		if session_id:
			try:
				await self.browser_session.cdp_client.send.Page.stopScreencast(session_id=session_id)
			except Exception as e:
				self.logger.debug(f'Failed to stop CDP screencast on {session_id}: {e}')

		output_path = recorder.output_path
		loop = asyncio.get_event_loop()
		await loop.run_in_executor(None, recorder.stop_and_save)
		return output_path

	@property
	def is_recording(self) -> bool:
		"""Whether a recording is currently in progress."""
		return self._recorder is not None

	async def on_AgentFocusChangedEvent(self, event: AgentFocusChangedEvent) -> None:
		"""
		Switches video recording to the new tab.
		"""
		if self._recorder:
			self.logger.debug(f'Agent focus changed to {event.target_id}, switching screencast...')
			await self._start_screencast()

	async def _start_screencast(self) -> None:
		"""Starts screencast on the currently focused tab."""
		if not self._recorder or not self._screencast_params:
			return

		try:
			# Get the current session (for the focused target)
			cdp_session = await self.browser_session.get_or_create_cdp_session()

			# If we are already recording this session, do nothing
			if self._current_session_id == cdp_session.session_id:
				return

			# Stop recording on the previous session
			if self._current_session_id:
				try:
					# Use the root client to stop screencast on the specific session
					await self.browser_session.cdp_client.send.Page.stopScreencast(session_id=self._current_session_id)
				except Exception as e:
					# It's possible the session is already closed
					self.logger.debug(f'Failed to stop screencast on old session {self._current_session_id}: {e}')

			self._current_session_id = cdp_session.session_id

			# Start recording on the new session
			await cdp_session.cdp_client.send.Page.startScreencast(
				params=self._screencast_params,  # type: ignore
				session_id=cdp_session.session_id,
			)
			self.logger.info(f'📹 Started/Switched video recording to target {cdp_session.target_id}')
		except Exception as e:
			self.logger.error(f'Failed to switch screencast via CDP: {e}')
			# If we fail to start on the new tab, we reset current session id
			self._current_session_id = None

	async def _get_current_viewport_size(self) -> ViewportSize | None:
		"""Gets the current viewport size directly from the browser via CDP."""
		try:
			cdp_session = await self.browser_session.get_or_create_cdp_session()
			metrics = await cdp_session.cdp_client.send.Page.getLayoutMetrics(session_id=cdp_session.session_id)

			# Use cssVisualViewport for the most accurate representation of the visible area
			viewport = metrics.get('cssVisualViewport', {})
			width = viewport.get('clientWidth')
			height = viewport.get('clientHeight')

			if width and height:
				self.logger.debug(f'Detected viewport size: {width}x{height}')
				return ViewportSize(width=int(width), height=int(height))
		except Exception as e:
			self.logger.warning(f'Failed to get viewport size from browser: {e}')

		return None

	def on_screencastFrame(self, event: ScreencastFrameEvent, session_id: str | None) -> None:
		"""
		Synchronous handler for incoming screencast frames.
		"""
		# Only process frames from the current session we intend to record
		# This handles race conditions where old session might still send frames before stop completes
		if self._current_session_id and session_id != self._current_session_id:
			return

		if not self._recorder:
			return
		self.latest_frame = event['data']
		self._recorder.add_frame(event['data'])
		self._unacked = (event['sessionId'], session_id)
		self._last_frame_at, self._topped_up = time.monotonic(), False

	async def _top_up(self, recorder: VideoRecorderService) -> None:
		"""Capture the page once, now that its stream is quiet, and make that the newest picture."""
		self._topped_up = True
		if not self._current_session_id:
			return
		try:
			shot = await self.browser_session.cdp_client.send.Page.captureScreenshot(
				params={'format': 'jpeg', 'quality': 85}, session_id=self._current_session_id
			)
		except Exception as e:
			self.logger.debug(f'Could not top up the recording with a capture: {e}')
			return
		if self._recorder is recorder and self._last_frame_at and not self._unacked:
			self.latest_frame = shot['data']
			recorder.add_frame(shot['data'])

	async def _ack(self, frame: int, session_id: str | None) -> None:
		"""Acknowledge a screencast frame, so the browser sends the next one."""
		try:
			await self.browser_session.cdp_client.send.Page.screencastFrameAck(params={'sessionId': frame}, session_id=session_id)
		except Exception as e:
			self.logger.debug(f'Failed to acknowledge screencast frame: {e}')

	async def on_BrowserStopEvent(self, event: BrowserStopEvent) -> None:
		"""
		Stops the video recording and finalizes the video file.
		"""
		if self._recorder:
			self.logger.debug('Stopping video recording and saving file...')
			await self.stop_recording()
