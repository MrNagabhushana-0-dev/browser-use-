"""Deciding what to press, when you have never seen the game before.

Thirty arbitrary games cannot be hand-coded, and a fixed key pattern is not playing —
it is twitching in time. What generalises is the observation that a game is a function
from inputs to a changing picture, and that the controls which *are* the controls
produce more change than the ones that are not. Arrow keys in a driving game move the
world; the same keys in a point-and-click do nothing at all.

So the player treats the control set as a multi-armed bandit and the picture as the
reward. It tries things, measures how much the screen moved in the moment after each
one, and converges on the inputs that this particular game responds to — UCB1, so a
control that looked dead early still gets retried rather than being written off on one
sample. A driving game converges on held right-arrow within a few seconds; a tapper
converges on clicks; a game that wants the space bar finds the space bar.

Two things stop it degenerating. Holds and taps are separate arms, because accelerating
and nudging are different moves and a game usually wants one of them. And idling is an
arm too, so a game that punishes mashing can teach it to wait.
"""

import math
import random
from dataclasses import dataclass, field
from typing import Literal

# Where in the play surface a pointer action lands. Games put their controls in
# predictable places: steer left and right, jump in the middle.
Where = Literal['centre', 'left', 'right', 'top', 'bottom']


@dataclass
class Action:
	"""One thing a player can do."""

	name: str
	kind: Literal['hold_key', 'tap_key', 'click', 'hold_click', 'idle']
	key: str | None = None
	seconds: float = 0.0
	where: Where = 'centre'

	def __str__(self) -> str:
		return self.name


def default_actions() -> list[Action]:
	"""The control vocabulary of nearly every browser game worth playing.

	Deliberately small. A bandit over two hundred arms spends the whole session
	exploring; these fifteen cover driving, running, jumping, steering, tapping and
	waiting, which is most of the catalogue.
	"""
	return [
		Action('hold right', 'hold_key', key='ArrowRight', seconds=0.9),
		Action('hold left', 'hold_key', key='ArrowLeft', seconds=0.6),
		Action('hold up', 'hold_key', key='ArrowUp', seconds=0.7),
		Action('hold down', 'hold_key', key='ArrowDown', seconds=0.5),
		Action('tap right', 'tap_key', key='ArrowRight'),
		Action('tap left', 'tap_key', key='ArrowLeft'),
		Action('tap up', 'tap_key', key='ArrowUp'),
		Action('tap space', 'tap_key', key='Space'),
		Action('hold space', 'hold_key', key='Space', seconds=0.5),
		Action('tap W', 'tap_key', key='w'),
		Action('click centre', 'click', where='centre'),
		Action('click left', 'click', where='left'),
		Action('click right', 'click', where='right'),
		Action('hold click', 'hold_click', seconds=0.6, where='centre'),
		Action('wait', 'idle', seconds=0.4),
	]


@dataclass
class Arm:
	"""What has been learned about one action."""

	action: Action
	pulls: int = 0
	total: float = 0.0

	@property
	def mean(self) -> float:
		return self.total / self.pulls if self.pulls else 0.0


@dataclass
class BanditPlayer:
	"""Learns a game's controls from the only signal available: the picture."""

	actions: list[Action] = field(default_factory=default_actions)
	rng: random.Random = field(default_factory=random.Random)
	# Higher explores longer. 1.2 settles within ~20 pulls, which at ~2 actions a second
	# is about ten seconds of a ninety second session spent finding the controls.
	exploration: float = 1.2
	arms: list[Arm] = field(init=False)
	_step: int = 0

	def __post_init__(self) -> None:
		self.arms = [Arm(a) for a in self.actions]

	def choose(self) -> Action:
		"""UCB1: the best mean so far, plus a bonus for the arms we know least about."""
		self._step += 1
		unplayed = [arm for arm in self.arms if not arm.pulls]
		if unplayed:
			return self.rng.choice(unplayed).action

		def score(arm: Arm) -> float:
			return arm.mean + self.exploration * math.sqrt(math.log(self._step) / arm.pulls)

		return max(self.arms, key=score).action

	def reward(self, action: Action, value: float) -> None:
		"""Record how much the screen moved after an action. Value is 0-1."""
		for arm in self.arms:
			if arm.action is action:
				arm.pulls += 1
				arm.total += max(0.0, min(1.0, value))
				return

	def learned(self, top: int = 4) -> str:
		"""The controls this game turned out to respond to, best first."""
		ranked = sorted((a for a in self.arms if a.pulls), key=lambda a: a.mean, reverse=True)
		return ', '.join(f'{a.action.name} {a.mean:.2f}' for a in ranked[:top]) or 'nothing tried'
