"""One `Page.captureScreenshot` at a time per browser connection, and no clipped captures where a crop will do.

Two captures in flight on one tab can hand each other's picture back. Found with the eyes' compositor sight of a
cross-origin film (captures of the film's box) while reflex rules captured the whole viewport: 20-24 of ~30 of the
eyes' pictures came back as the page, shrunk and tiled; 0 of ~30 with the captures taken one at a time. Every capture
in the library goes through `capture_screenshot`, which holds a lock per CDP client, so per browser connection (a lock
lives in one event loop, and so does a client).

A capture with a `clip` also shows in the tab's screencast: Chrome draws the clipped region to take it, and the
screencast sends those frames as if they were the page (found in a Retinat recording of the Commons film: frames of a
small picture on white, then of the film filling the frame). Measured with a screencast running while the right half
of a page was captured: 22-40 of 52-97 screencast frames were the clip, whatever its scale or `captureBeyondViewport`;
none without a clip. The recorder, the reflexes' screen feed and page scans all read the screencast. So a clip inside
the visible viewport, at a scale of 1 or less, is taken as a capture of the whole viewport cropped and scaled here.
A clip beyond the viewport, or one that enlarges (a zoom redrawn at up to 4x), still needs Chrome to draw it.
"""

import asyncio
import base64
import io
import weakref
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
	from cdp_use import CDPClient
	from cdp_use.cdp.page import CaptureScreenshotParameters

_locks: 'weakref.WeakKeyDictionary[CDPClient, asyncio.Lock]' = weakref.WeakKeyDictionary()

# A JPEG taken to be cropped is taken at least this good, so cropping and encoding again loses little.
CROP_SOURCE_QUALITY = 90

# Captures sent to Chrome through `capture_screenshot` in this process: what a caller can read to see what its work
# costs.
captures_taken = 0

# The capture each tab (per connection, per session) has in Chrome and not yet answered. One a caller gave up on is still
# being taken, Chrome cannot be told to drop it, and a capture sent over it can swap pictures with it, so the next one
# on that tab waits for it first.
_in_chrome: 'weakref.WeakKeyDictionary[CDPClient, dict[str | None, asyncio.Future[Any]]]' = weakref.WeakKeyDictionary()

# A capture waits for the compositor's next frame, and a tab that draws none never gives one. Found when a model played
# Poki games: the first look's capture never answered, and every capture after it waited behind it for the lock until
# the client gave up on each. No capture takes this long while frames come.
CAPTURE_WAIT_S = 20.0


class CaptureTimeout(TimeoutError):
	"""No frame came to capture in time: the tab draws none."""


def capture_lock(client: 'CDPClient') -> asyncio.Lock:
	"""The lock every capture through this client holds."""
	lock = _locks.get(client)
	if lock is None:
		lock = _locks[client] = asyncio.Lock()
	return lock


async def capture_screenshot(client: 'CDPClient', params: Any, session_id: str | None, timeout: float = CAPTURE_WAIT_S) -> Any:
	"""`Page.captureScreenshot` with `params` on `session_id`, never overlapping another capture through `client`, and
	without a clip where cropping a capture of the viewport gives the same picture (see the module docstring). Raises
	`CaptureTimeout` if no picture comes within `timeout` seconds, the wait for the lock included."""
	global captures_taken
	try:
		async with asyncio.timeout(timeout), capture_lock(client):
			in_chrome = _in_chrome.setdefault(client, {})
			earlier = in_chrome.get(session_id)
			if earlier is not None:
				await asyncio.wait({earlier})  # given up on by its caller, still being taken: nothing is sent over it
			captures_taken += 1
			taking = asyncio.ensure_future(_capture(client, params, session_id))
			in_chrome[session_id] = taking
			taking.add_done_callback(lambda done: _taken(client, session_id, done))
			return await asyncio.shield(taking)  # a timeout leaves it to be taken, and waited for by the next
	except TimeoutError as e:
		raise CaptureTimeout(f'no frame came to capture in {timeout:.0f}s: the tab draws none') from e


async def _capture(client: 'CDPClient', params: Any, session_id: str | None) -> Any:
	clip = params.get('clip') if isinstance(params, dict) else None
	if clip and float(clip.get('scale') or 1) <= 1 and not params.get('captureBeyondViewport'):
		shot = await _cropped(client, params, clip, session_id)
		if shot is not None:
			return shot
	return await client.send.Page.captureScreenshot(params=cast('CaptureScreenshotParameters', params), session_id=session_id)


def _taken(client: 'CDPClient', session_id: str | None, done: 'asyncio.Future[Any]') -> None:
	in_chrome = _in_chrome.get(client)
	if in_chrome is not None and in_chrome.get(session_id) is done:
		del in_chrome[session_id]
	if not done.cancelled():
		done.exception()  # one nobody waited for any more failed: seen here, not reported as never retrieved


async def _cropped(client: 'CDPClient', params: dict, clip: dict, session_id: str | None) -> dict | None:
	"""The clip, cut from a capture of the visible viewport; None if it reaches beyond what is visible."""
	from PIL import Image

	metrics = await client.send.Page.getLayoutMetrics(session_id=session_id)
	view = metrics['cssVisualViewport']
	left, top = float(clip['x']) - float(view['pageX']), float(clip['y']) - float(view['pageY'])
	width, height = float(clip['width']), float(clip['height'])
	if left < -0.5 or top < -0.5 or left + width > view['clientWidth'] + 0.5 or top + height > view['clientHeight'] + 0.5:
		return None
	fmt = params.get('format') or 'png'
	whole = {k: v for k, v in params.items() if k != 'clip'}
	if fmt == 'jpeg':
		whole['quality'] = max(int(params.get('quality') or 80), CROP_SOURCE_QUALITY)
	shot = await client.send.Page.captureScreenshot(params=cast('CaptureScreenshotParameters', whole), session_id=session_id)
	with Image.open(io.BytesIO(base64.b64decode(shot['data']))) as img:
		scale = float(clip.get('scale') or 1)
		device = _device_scale(metrics, img.width)
		size = (max(1, round(width * scale * device)), max(1, round(height * scale * device)))
		full = img.width
		if img.format == 'JPEG' and scale < 1:
			# Decode at a fraction of the size when the clip is wanted smaller: ~6 ms instead of ~20 for a 1920x1080
			# frame cut to a 320 px picture (the JPEG decoder scales by 1/2, 1/4 or 1/8 as it goes)
			img.draft('RGB', (max(1, round(img.width * scale)), max(1, round(img.height * scale))))
		px = device * img.width / full  # pixels per CSS px as decoded
		box = (round(max(0.0, left) * px), round(max(0.0, top) * px), round((left + width) * px), round((top + height) * px))
		part = img.crop(box)
		if part.size != size:
			part = part.resize(size, Image.Resampling.LANCZOS)
		out = io.BytesIO()
		if fmt == 'jpeg':
			part.convert('RGB').save(out, 'JPEG', quality=int(params.get('quality') or 80))
		else:
			part.save(out, 'WEBP' if fmt == 'webp' else 'PNG')
	return {'data': base64.b64encode(out.getvalue()).decode()}


def _device_scale(metrics: Any, image_width: int) -> float:
	"""Image pixels per CSS px in a capture of the viewport. Not the image's width over the client width: the capture
	takes in the page's scrollbar and the client width leaves it out (1920 against 1905 px on Poki), which put every
	look on a page with a scrollbar ~1% out, ~15 px at its far edge. `visualViewport` is in device pixels."""
	css = float(metrics['cssVisualViewport']['clientWidth'])
	device = float((metrics.get('visualViewport') or {}).get('clientWidth') or 0)
	return device / css if device > 0 and css > 0 else image_width / max(1.0, css)
