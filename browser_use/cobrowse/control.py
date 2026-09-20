"""Who is driving, when two of you share one browser.

Co-browsing without arbitration is worse than no co-browsing: the agent clicks while you
are mid-sentence in a text field, you scroll while it is measuring an element's position,
and both of you conclude the page is broken. The failure is silent and hard to reproduce,
which is the worst kind.

So control is explicit and one-at-a-time. The default holder is the agent, so nothing
about existing single-driver use changes; hand control to yourself and every way the agent
can act starts refusing, with a message that says who has it and why.

The activity log exists for the other half of the problem: if the agent has been driving
while you were away, you need to see what it did before you take the wheel back.
"""

import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

logger = logging.getLogger(__name__)

Holder = Literal['agent', 'human']

# Activity entries kept. Enough to answer "what did it just do", not an audit trail.
MAX_ACTIVITY = 200


@dataclass(frozen=True)
class ControlChange:
	"""One handover."""

	at: datetime
	holder: Holder
	note: str = ''

	def render(self) -> str:
		stamp = self.at.strftime('%H:%M:%S')
		return f'{stamp}  control → {self.holder}' + (f' ({self.note})' if self.note else '')


@dataclass
class Activity:
	"""Something the agent did, in words a person can scan."""

	at: datetime
	description: str

	def render(self) -> str:
		return f'{self.at.strftime("%H:%M:%S")}  {self.description}'


class ControlLock:
	"""Tracks who may act, and what the agent has been doing.

	Deliberately not a mutex. There is no waiting and no queue: an agent that blocks
	waiting for a human to finish is an agent that appears hung, and a human who has to
	wait for a lock will just close the tab. Refusal is the correct answer — the agent can
	report that it is paused and pick up when control comes back.
	"""

	def __init__(self, holder: Holder = 'agent') -> None:
		self.holder: Holder = holder
		self.reason: str = ''
		self.changes: deque[ControlChange] = deque(maxlen=50)
		self.activity: deque[Activity] = deque(maxlen=MAX_ACTIVITY)

	# -- handover ---------------------------------------------------------------------

	def grant_to_human(self, note: str = '') -> None:
		"""You take the wheel. The agent stops acting immediately."""
		self._set('human', note)

	def grant_to_agent(self, note: str = '') -> None:
		"""Hand it back. The agent may act again."""
		self._set('agent', note)

	def _set(self, holder: Holder, note: str) -> None:
		if self.holder == holder:
			return
		self.holder = holder
		self.reason = note
		change = ControlChange(at=datetime.now(timezone.utc), holder=holder, note=note)
		self.changes.append(change)
		logger.info(f'🤝 {change.render()}')

	# -- enforcement ------------------------------------------------------------------

	@property
	def agent_may_act(self) -> bool:
		return self.holder == 'agent'

	def refusal(self, what: str) -> str:
		"""The message the agent gets when it tries to act anyway.

		Says who has control, why, and that the block is temporary — otherwise a model
		reads a bare failure as "this page is broken" and starts trying workarounds.
		"""
		because = f' ({self.reason})' if self.reason else ''
		return (
			f'Cannot {what}: the person is driving right now{because}. '
			'Wait and report what you are blocked on; control will come back.'
		)

	# -- what the agent has been doing --------------------------------------------------

	def record(self, description: str) -> None:
		self.activity.append(Activity(at=datetime.now(timezone.utc), description=description))

	def recent(self, limit: int = 20) -> list[str]:
		entries = list(self.activity)[-limit:]
		return [entry.render() for entry in entries]

	def summary(self) -> str:
		"""A short account for whoever is about to take over."""
		lines = [f'Control is with the {self.holder}.']
		if self.reason:
			lines[0] += f' ({self.reason})'
		if self.activity:
			lines.append(f'Last {min(len(self.activity), 10)} agent actions:')
			lines.extend(f'  {line}' for line in self.recent(10))
		else:
			lines.append('The agent has not done anything yet.')
		return '\n'.join(lines)
