"""The event gate: when the model is woken, decided by a cheap always-on perceiver instead of the model's own polling.

The perceiver is the retina's per-frame change score (sight.deltas: mean absolute luma or colour difference of the
16x16 / 4x4 grids, ~16+ at a cut, ~2 a frame for a slow pan, <1 for a still). It runs on every frame of the attended
item as batches arrive. The gate over it is a four-state Schmitt trigger:

  IDLE -> CONFIRM_ONSET   when the smoothed score crosses tau_high (the onset is remembered)
  CONFIRM_ONSET -> ACTIVE after n_on frames at or above tau_low; back to IDLE on a dip below it (a blip, no episode)
  ACTIVE -> HANGOVER      when it falls below tau_low
  HANGOVER -> ACTIVE      on a rise back to tau_low or above; -> IDLE after n_off frames below (the episode ends)

The score is smoothed with an asymmetric EMA (fast attack, slow decay). Two thresholds, onset confirmation and a
hangover keep a borderline signal (a blinking caret, a slowly animated chart) from flapping the gate, while the
retroactive-commit reservoir (eyes/reservoir.py) keeps the onset that confirmation waits through: what is committed
starts at the reservoir's cursor, before the onset, so a conservative gate clips nothing. Gating alone is prior art
(StreamMind, arXiv 2503.06220); the claim is the pairing with the reservoir.

`single_threshold()` is the ablation: one threshold, no confirmation, no hangover.
"""

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

State = Literal['IDLE', 'CONFIRM_ONSET', 'ACTIVE', 'HANGOVER']


@dataclass
class Episode:
	onset: float  # wall (monotonic) of the frame that first crossed tau_high
	active: float  # when ACTIVE was entered (onset confirmed)
	end: float | None = None  # first frame of the hangover that closed it


@dataclass
class Gate:
	tau_high: float = 3.0
	tau_low: float = 1.5
	n_on: int = 3  # frames above tau_low to confirm an onset (0.3 s at 10 fps)
	n_off: int = 10  # frames below tau_low to close an episode (1 s at 10 fps)
	attack: float = 0.6  # EMA weight of a rising score
	decay: float = 0.15  # EMA weight of a falling score
	state: State = 'IDLE'
	level: float = 0.0
	episodes: list[Episode] = field(default_factory=list)
	transitions: list[tuple[float, str, str]] = field(default_factory=list)  # (wall, from, to)
	frames: int = 0
	first: float | None = None
	last: float | None = None
	_count: int = 0
	_onset: float = 0.0
	_hangover_from: float = 0.0

	@classmethod
	def single_threshold(cls, tau: float = 2.25, attack: float = 0.6, decay: float = 0.15) -> 'Gate':
		"""The ablation: ACTIVE at or above one threshold, IDLE below it, from the same smoothed score."""
		return cls(tau_high=tau, tau_low=tau, n_on=0, n_off=0, attack=attack, decay=decay)

	def _go(self, wall: float, to: State) -> None:
		self.transitions.append((wall, self.state, to))
		self.state = to

	def feed(self, wall: float, score: float) -> None:
		"""One frame's change score at `wall`."""
		assert score >= 0, score
		self.frames += 1
		self.first = wall if self.first is None else self.first
		self.last = wall
		w = self.attack if score > self.level else self.decay
		self.level += w * (score - self.level)
		x = self.level
		if self.state == 'IDLE':
			if x >= self.tau_high:
				self._onset = wall
				if self.n_on == 0:
					self._go(wall, 'ACTIVE')
					self.episodes.append(Episode(onset=wall, active=wall))
				else:
					self._count = 0
					self._go(wall, 'CONFIRM_ONSET')
		elif self.state == 'CONFIRM_ONSET':
			if x < self.tau_low:
				self._go(wall, 'IDLE')
			else:
				self._count += 1
				if self._count >= self.n_on:
					self._go(wall, 'ACTIVE')
					self.episodes.append(Episode(onset=self._onset, active=wall))
		elif self.state == 'ACTIVE':
			if x < self.tau_low:
				if self.n_off == 0:
					self.episodes[-1].end = wall
					self._go(wall, 'IDLE')
				else:
					self._count, self._hangover_from = 1, wall
					self._go(wall, 'HANGOVER')
		elif self.state == 'HANGOVER':
			if x >= self.tau_low:
				self._go(wall, 'ACTIVE')
			else:
				self._count += 1
				if self._count >= self.n_off:
					self.episodes[-1].end = self._hangover_from
					self._go(wall, 'IDLE')

	def stats(self) -> dict:
		"""Flapping telemetry: state transitions and episodes per hour of signal, and how long onsets waited to be
		confirmed (what a gate without a reservoir would clip)."""
		hours = max(1e-9, ((self.last or 0.0) - (self.first or 0.0)) / 3600)
		waits = [e.active - e.onset for e in self.episodes]
		# a flap is the gate opening or closing (ACTIVE/HANGOVER vs IDLE/CONFIRM_ONSET): what would wake the model or
		# end an event; the 4-state gate's inner steps (confirming, hanging over) are not flaps
		opened = {'ACTIVE', 'HANGOVER'}
		flips = sum(1 for _, a, b in self.transitions if (a in opened) != (b in opened))
		return {
			'frames': self.frames,
			'hours': round(hours, 5),
			'flips': flips,
			'flips_per_hour': round(flips / hours, 1),
			'transitions': len(self.transitions),
			'episodes': len(self.episodes),
			'onset_confirm_s_max': round(max(waits), 2) if waits else 0.0,
		}


class GateLog:
	"""Append-only signal and transition log, for replaying the ablation offline over the same signal."""

	def __init__(self, path: Path | None) -> None:
		self.path = path
		self._wall0 = time.time() - time.monotonic()  # monotonic -> epoch

	def write(self, kind: str, wall: float, **fields: object) -> None:
		if self.path is None:
			return
		self.path.parent.mkdir(parents=True, exist_ok=True)
		with self.path.open('a') as f:
			f.write(json.dumps({'kind': kind, 'wall': round(wall + self._wall0, 3), **fields}) + '\n')
