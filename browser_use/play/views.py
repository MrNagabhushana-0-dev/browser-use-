"""What a play session is, and how it gets scored.

Scoring a game you cannot read is the hard part. A Poki game runs inside a cross-origin
iframe, so its score variable, its DOM and its canvas are all unreachable — the only
honest signal is pixels and the record of what was pressed. So the metrics here are
deliberately about *evidence of play* rather than a number the game would recognise:

- `active_ratio` — how much of the session the picture was actually moving. A game that
  never started, or that died in the first second and sat on a game-over screen, scores
  near zero however many keys were pressed at it.
- `response_rate` — how often an input was followed, within a couple of hundred
  milliseconds, by more motion than the surrounding baseline. This is the one that says
  the controls are connected: random keys at a video would move the picture just as much,
  but they would not *correlate* with the keys.
- `best_streak` — the longest unbroken stretch of motion, which for a runner or a driver
  is the closest pixel-only proxy there is for "how long it survived".
- `stalls` / `restarts` — how often the picture froze and had to be nudged.

None of these is a score in the game's own currency, and this module does not pretend
otherwise. They are falsifiable measurements of whether a machine was really playing.
"""

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field
from uuid_extensions import uuid7str

# Percentage of the frame that has to change for a moment to count as "the game is
# moving". Two percent is a sprite crossing a static background; below one percent is
# a blinking cursor or JPEG noise on an otherwise frozen picture.
ACTIVE_THRESHOLD = 2

# How long the picture may sit still before we treat it as a stall worth nudging.
STALL_SECONDS = 2.5


class InputEvent(BaseModel):
	"""One thing the player did, and when."""

	model_config = ConfigDict(extra='forbid')

	at: float
	kind: str
	detail: str = ''


class GameReport(BaseModel):
	"""The record of one game, played once."""

	model_config = ConfigDict(extra='forbid', validate_by_name=True)

	id: str = Field(default_factory=uuid7str)
	name: str
	url: str
	started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

	seconds_played: float = 0.0
	frames: int = 0
	# Per-frame change scores, so the shape of a session can be re-examined later.
	motion: list[int] = Field(default_factory=list)
	inputs: list[InputEvent] = Field(default_factory=list)

	loaded: bool = False
	surface: str = ''
	stalls: int = 0
	restarts: int = 0
	strategy: str = ''
	note: str = ''
	recording: str = ''
	# A handful of scene descriptions spread across the session — what the player saw,
	# as text. Six of these cost less than a single screenshot.
	scene_sample: list[str] = Field(default_factory=list)
	cuts: int = 0
	keyframes: list[str] = Field(default_factory=list)

	@property
	def active_ratio(self) -> float:
		"""Fraction of captured frames in which the picture was moving."""
		if not self.motion:
			return 0.0
		return sum(1 for m in self.motion if m >= ACTIVE_THRESHOLD) / len(self.motion)

	@property
	def fps(self) -> float:
		return self.frames / self.seconds_played if self.seconds_played else 0.0

	@property
	def best_streak(self) -> float:
		"""Longest unbroken run of moving frames, in seconds."""
		if not self.motion or not self.fps:
			return 0.0
		best = run = 0
		for m in self.motion:
			run = run + 1 if m >= ACTIVE_THRESHOLD else 0
			best = max(best, run)
		return round(best / self.fps, 1)

	@property
	def response_rate(self) -> float:
		"""How often an input was followed by more motion than the session's baseline.

		This is the measurement that distinguishes playing from pressing keys at a wall.
		For each input, compare the mean motion in the ~400ms after it against the
		session's own median. Random inputs against an unresponsive surface land near 0;
		inputs that drive something land well above it.
		"""
		if not self.inputs or not self.motion or not self.fps:
			return 0.0
		ordered = sorted(self.motion)
		median = ordered[len(ordered) // 2]
		window = max(2, int(self.fps * 0.4))

		hits = 0
		for event in self.inputs:
			start = int(event.at * self.fps)
			after = self.motion[start : start + window]
			if after and (sum(after) / len(after)) > median:
				hits += 1
		return hits / len(self.inputs)

	@property
	def played(self) -> bool:
		"""Whether this counts as having actually played the game.

		Deliberately strict: it has to have loaded, run for the full minimum, moved for
		most of that time, and responded to the controls.
		"""
		return self.loaded and self.seconds_played >= 90.0 and self.active_ratio >= 0.5 and self.response_rate >= 0.5

	def summary(self) -> str:
		verdict = 'PLAYED' if self.played else 'incomplete'
		return (
			f'{self.name:<28} {verdict:<11} {self.seconds_played:5.1f}s  '
			f'active {self.active_ratio * 100:5.1f}%  responded {self.response_rate * 100:5.1f}%  '
			f'streak {self.best_streak:5.1f}s  inputs {len(self.inputs):4d}  '
			f'stalls {self.stalls}  {self.note}'
		)


class Scoreboard(BaseModel):
	"""Every game played in one sitting."""

	model_config = ConfigDict(extra='forbid')

	reports: list[GameReport] = Field(default_factory=list)

	@property
	def played(self) -> list[GameReport]:
		return [r for r in self.reports if r.played]

	def render(self) -> str:
		lines = [r.summary() for r in self.reports]
		total = len(self.reports)
		good = len(self.played)
		seconds = sum(r.seconds_played for r in self.reports)
		lines.append('-' * 118)
		lines.append(
			f'{good}/{total} games genuinely played  ·  {seconds / 60:.1f} minutes of gameplay  ·  '
			f'{sum(len(r.inputs) for r in self.reports)} inputs sent'
		)
		return '\n'.join(lines)
