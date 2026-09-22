"""A continuous perception stream: pixels in, a line of text per tick out.

A screenshot is the wrong unit for anything that moves. It costs on the order of 1,500
tokens, it arrives after the moment it describes, and it says nothing about motion — two
screenshots have to be diffed by the reader before they mean anything. Watching a video
or playing a game by screenshotting is therefore both expensive and late, which is why
the usual loop is screenshot, think, act, screenshot, and why it plays like a slideshow.

What a person gets from their visual system is not a sequence of pictures. It is a small,
continuously updated model of a few things that matter: what is here, where it is, which
way it is going, and what is about to hit what. That is a few dozen symbols, not a
million pixels, and it is already differentiated — velocity comes for free rather than
being inferred from a pair of stills.

So this keeps object identity across frames and emits one short line per tick:

	t=2.4 pan=left | #1 (0.21,0.62) v(+0.00,+0.05) | #4 (0.78,0.61) v(-0.09,+0.00) ttc=0.9s

Around forty tokens. The same moment as an image is about 1,500, and the image does not
carry the velocities or the time-to-contact — the reader would have to derive those from
two images, at 3,000 tokens, and still be a frame behind.

The tracker is deliberately classical: predict each known object forward by its velocity,
match this frame's blobs to those predictions by nearest neighbour inside a gate, age out
what stops appearing. Identity is what makes velocity meaningful; without it every frame
is a fresh set of anonymous rectangles and nothing can be said about where anything is
heading.
"""

import logging
import time
from dataclasses import dataclass, field

from browser_use.vision.label import FrameFeatures, frame_features
from browser_use.vision.perceive import Blob, Scene, luma_grid, perceive

logger = logging.getLogger(__name__)

# How far an object may be from where it was predicted and still count as the same thing.
# In normalised screen units, so 0.18 is about a fifth of the screen between ticks.
MATCH_GATE = 0.18

# Ticks an object may go unseen before it is forgotten. Two covers a sprite blinking or
# passing behind something; more starts inventing objects that have left.
MAX_MISSED = 2

# Objects reported per line, largest first. A player tracks a handful of things at once.
MAX_REPORTED = 4

# Objects closing faster than this are worth a time-to-contact estimate.
MIN_CLOSING_SPEED = 0.01


@dataclass
class Tracked:
	"""One object, followed across frames."""

	id: int
	x: float
	y: float
	w: float
	h: float
	dx: float = 0.0
	dy: float = 0.0
	age: int = 1
	missed: int = 0
	label: str = 'region'

	def predict(self) -> tuple[float, float]:
		return self.x + self.dx, self.y + self.dy

	def absorb(self, blob: Blob, smoothing: float = 0.6) -> None:
		"""Move onto the new observation, easing the velocity rather than snapping to it.

		Raw frame-to-frame deltas are noisy enough that an object can appear to reverse
		direction every tick; a little smoothing makes the heading mean something.
		"""
		dx, dy = blob.x - self.x, blob.y - self.y
		self.dx = round(smoothing * dx + (1 - smoothing) * self.dx, 3)
		self.dy = round(smoothing * dy + (1 - smoothing) * self.dy, 3)
		self.x, self.y, self.w, self.h = blob.x, blob.y, blob.w, blob.h
		self.age += 1
		self.missed = 0


@dataclass
class SceneTracker:
	"""Keeps object identity across frames, which is what makes velocity possible."""

	_objects: list[Tracked] = field(default_factory=list)
	_next_id: int = 1

	@property
	def objects(self) -> list[Tracked]:
		return self._objects

	def update(self, blobs: list[Blob]) -> list[Tracked]:
		unmatched = list(blobs)
		for obj in self._objects:
			px, py = obj.predict()
			best, best_distance = None, MATCH_GATE**2
			for blob in unmatched:
				distance = (blob.x - px) ** 2 + (blob.y - py) ** 2
				if distance < best_distance:
					best, best_distance = blob, distance
			if best is not None:
				obj.absorb(best)
				unmatched.remove(best)
			else:
				obj.missed += 1

		self._objects = [o for o in self._objects if o.missed <= MAX_MISSED]
		for blob in unmatched:
			self._objects.append(Tracked(id=self._next_id, x=blob.x, y=blob.y, w=blob.w, h=blob.h))
			self._next_id += 1
		return self._objects

	def anchor(self) -> Tracked | None:
		"""The object most likely to be the thing you are controlling.

		Not a classifier — a consequence of how games are built. The avatar is on screen
		for the whole session while obstacles enter and leave, so the longest-lived object
		is almost always it. Wrong on a static HUD element, which is why size is a
		tiebreak: HUD pieces are small and never move.
		"""
		candidates = [o for o in self._objects if o.age >= 4]
		if not candidates:
			return None
		return max(candidates, key=lambda o: (o.age, o.w * o.h))


def time_to_contact(anchor: Tracked, other: Tracked) -> float | None:
	"""Seconds until two objects meet, in ticks, if they hold their courses.

	The quantity a player is actually computing when they decide to jump.
	"""
	rx, ry = other.x - anchor.x, other.y - anchor.y
	rdx, rdy = other.dx - anchor.dx, other.dy - anchor.dy
	closing = -(rx * rdx + ry * rdy)
	speed = rdx * rdx + rdy * rdy
	if speed <= 0 or closing <= MIN_CLOSING_SPEED:
		return None
	ticks = closing / speed
	return round(ticks, 1) if 0 < ticks < 30 else None


@dataclass
class PerceptionStream:
	"""Frames in, one short line of text per frame out."""

	tracker: SceneTracker = field(default_factory=SceneTracker)
	started: float = field(default_factory=time.monotonic)
	_previous_grid: list[list[int]] | None = None
	lines: list[str] = field(default_factory=list)

	def observe(self, jpeg_bytes: bytes, at: float | None = None) -> str | None:
		"""Take one frame. Returns the line describing it, or None for the first."""
		grid = luma_grid(jpeg_bytes)
		if grid is None:
			return None
		if self._previous_grid is None:
			self._previous_grid = grid
			return None

		scene = perceive(self._previous_grid, grid)
		self._previous_grid = grid
		# Decoded once and shared across every blob this frame — the JPEG decode is the
		# cost, labelling a region against the result is nearly free.
		features = frame_features(jpeg_bytes)
		line = self._render(scene, at if at is not None else time.monotonic() - self.started, features)
		self.lines.append(line)
		return line

	def _render(self, scene: Scene, at: float, features: FrameFeatures | None = None) -> str:
		if scene.cut:
			self.tracker = SceneTracker()
			return f't={at:.1f} scene cut — everything changed at once'
		if scene.static:
			return f't={at:.1f} still'

		objects = self.tracker.update(scene.blobs)
		if features is not None and features.usable:
			# Label on the frame we can actually see it in. A region that vanishes keeps its
			# last label rather than reverting to 'region', because identity outlives one
			# occluded frame and so should the name.
			for obj in objects:
				if obj.missed == 0:
					obj.label = features.label(obj.x, obj.y, obj.w, obj.h)
		anchor = self.tracker.anchor()
		parts = [f't={at:.1f}']
		if scene.pan:
			parts.append(f'pan={scene.pan}')

		ranked = sorted(objects, key=lambda o: o.w * o.h, reverse=True)[:MAX_REPORTED]
		for obj in ranked:
			tag = '*' if anchor is not None and obj.id == anchor.id else ''
			bit = f'#{obj.id}{tag} {obj.label} ({obj.x:.2f},{obj.y:.2f}) v({obj.dx:+.2f},{obj.dy:+.2f})'
			if anchor is not None and obj.id != anchor.id:
				if (ttc := time_to_contact(anchor, obj)) is not None:
					bit += f' ttc={ttc}'
			parts.append(bit)
		return ' | '.join(parts)

	def digest(self, most_recent: int = 12) -> str:
		"""The stream as a block, for a reader that wants the last few seconds at once."""
		return '\n'.join(self.lines[-most_recent:])
