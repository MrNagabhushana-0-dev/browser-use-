"""Desktop eyes: the retina's perception (cuts, motion, keyframes, one sheet) over a whole X display. Opt-in only.

Desktop agents today see a screenshot per step (UFO2, Microsoft, 2025: screenshots fused with UI Automation and an
element detector). What happens on screen between two steps, a dialog that came and went or a progress bar that
moved, is not seen. These eyes sample the display continuously and summarise it the way the browser retina
summarises a video. Seeing the whole desktop is a larger step than seeing one browser tab, so they stay off unless
asked for: `DesktopEyes(enabled=True)` in code, or BROWSER_USE_DESKTOP_EYES=1 in the environment.

Linux/X11 only for now (Pillow grabs the display through XCB). No sound: the desktop has no single audio track.
"""

from __future__ import annotations

import asyncio
import io
import os
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
	from PIL import Image

from browser_use.eyes import hearing, motion, sight
from browser_use.eyes.page import signature
from browser_use.eyes.percept import ItemPercept, Keyframe, Percept, assemble, estimate_image_tokens
from browser_use.eyes.retina import FrameSample

OPT_IN_ENV = 'BROWSER_USE_DESKTOP_EYES'


class DesktopEyesOff(PermissionError):
	"""Desktop eyes were not asked for."""


class DesktopEyes:
	"""Watch an X display: `watch(seconds)` for a percept of what changed, `look()` for the screen now."""

	def __init__(
		self,
		display: str | None = None,
		enabled: bool | None = None,
		fps: float = 5.0,
		max_width: int = 960,
		mask: Callable[[Image.Image], Image.Image] | None = None,
	):
		allowed = enabled if enabled is not None else os.environ.get(OPT_IN_ENV, '').lower() in ('1', 'true', 'yes')
		if not allowed:
			raise DesktopEyesOff(
				f'Desktop eyes are off: they see the whole screen, not one browser tab. Turn them on with '
				f'DesktopEyes(enabled=True) or {OPT_IN_ENV}=1, only where the person has agreed to it.'
			)
		self.display = display or os.environ.get('DISPLAY') or None
		assert self.display, 'no X display to watch (set DISPLAY or pass display=)'
		assert 0 < fps <= 30, 'fps between 0 and 30'
		self.fps, self.max_width = fps, max_width
		self.mask = mask  # e.g. DesktopControl.hide_ungranted: what the AI may not see, covered in every frame

	def _grab(self) -> tuple[bytes, tuple[int, int]]:
		from PIL import Image, ImageGrab

		img = ImageGrab.grab(xdisplay=self.display).convert('RGB')
		if self.mask is not None:
			img = self.mask(img)
		size = img.size
		if img.width > self.max_width:
			img = img.resize((self.max_width, round(img.height * self.max_width / img.width)), Image.Resampling.BILINEAR)
		out = io.BytesIO()
		img.save(out, format='JPEG', quality=70)
		return out.getvalue(), size

	async def look(self) -> Percept:
		"""The screen now, as one image."""
		jpeg, size = await asyncio.to_thread(self._grab)
		frame = FrameSample(1, 1, 0.0, time.monotonic(), *signature(jpeg)[:2], True, signature(jpeg)[2])
		from PIL import Image

		with Image.open(io.BytesIO(jpeg)) as img:
			shown = img.size
		tokens = estimate_image_tokens(*shown)
		text = f'🖥 the screen ({self.display}, {size[0]}x{size[1]}) now: mostly {sight.colour_name(frame.rgb)}; ~{tokens} tokens'
		return Percept([], text, jpeg, shown, tokens, len(text) // 4)

	async def watch(self, seconds: float = 8.0, detail: str = 'glance', keyframes: int = 6) -> Percept:
		"""Sample the display for `seconds` and summarise it: shots and cuts, motion, keyframes on one sheet."""
		assert seconds > 0, 'seconds must be positive'
		frames: list[FrameSample] = []
		jpegs: dict[int, bytes] = {}
		size = (0, 0)
		start = time.monotonic()
		period = 1.0 / self.fps
		seq = 0
		while (now := time.monotonic()) - start < seconds:
			jpeg, size = await asyncio.to_thread(self._grab)
			seq += 1
			luma, rgb, grid4 = signature(jpeg)
			frames.append(FrameSample(seq, 1, now - start, now, luma, rgb, True, grid4))
			jpegs[seq] = jpeg
			await asyncio.sleep(max(0.0, period - (time.monotonic() - now)))
		seen = sight.read(frames)
		chosen = sight.select_keyframes(frames, keyframes)
		item = ItemPercept(
			index=1,
			vid=1,
			info={'kind': 'desktop', 'w': size[0], 'h': size[1]},
			frames=frames,
			hops=[],
			sight=seen,
			hearing=hearing.listen([]),
			keyframes=[Keyframe(frames[i].t, frames[i].seq, jpegs[frames[i].seq]) for i in chosen.indices],
			coverage=chosen.coverage,
			watched_s=frames[-1].t if frames else 0.0,
			motion=motion.track(frames),
		)
		return assemble([item], f'🖥 desktop watched {seconds:.0f}s on {self.display}', detail)
