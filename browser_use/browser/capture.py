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


def capture_lock(client: 'CDPClient') -> asyncio.Lock:
	"""The lock every capture through this client holds."""
	lock = _locks.get(client)
	if lock is None:
		lock = _locks[client] = asyncio.Lock()
	return lock


async def capture_screenshot(client: 'CDPClient', params: Any, session_id: str | None) -> Any:
	"""`Page.captureScreenshot` with `params` on `session_id`, never overlapping another capture through `client`, and
	without a clip where cropping a capture of the viewport gives the same picture (see the module docstring)."""
	async with capture_lock(client):
		clip = params.get('clip') if isinstance(params, dict) else None
		if clip and float(clip.get('scale') or 1) <= 1 and not params.get('captureBeyondViewport'):
			shot = await _cropped(client, params, clip, session_id)
			if shot is not None:
				return shot
		return await client.send.Page.captureScreenshot(params=cast('CaptureScreenshotParameters', params), session_id=session_id)


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
		device = img.width / max(1.0, float(view['clientWidth']))  # image pixels per CSS px (device scale)
		size = (max(1, round(width * scale * device)), max(1, round(height * scale * device)))
		if img.format == 'JPEG' and scale < 1:
			# Decode at a fraction of the size when the clip is wanted smaller: ~6 ms instead of ~20 for a 1920x1080
			# frame cut to a 320 px picture (the JPEG decoder scales by 1/2, 1/4 or 1/8 as it goes)
			img.draft('RGB', (max(1, round(img.width * scale)), max(1, round(img.height * scale))))
		px = img.width / max(1.0, float(view['clientWidth']))  # pixels per CSS px as decoded
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
