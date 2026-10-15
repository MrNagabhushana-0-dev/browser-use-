"""A call's deadline, and the line a tool writes when it stops for time.

MCP clients (Claude Code among them) give a tool call 60 s and then report it as failed, whatever the server did: a
retinat_scan was lost that way twice. A long tool now gets a Deadline when its call begins and stops in time with what
it has, saying how far it got and how to go on. Library callers pass none and nothing changes for them.
"""

import json
import math
import time
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

MCP_CLIENT_LIMIT_S = 60.0
# What a call may spend: under the client's 60 s, with room for the answer (a sheet is a few hundred KB of base64)
CALL_BUDGET_S = 50.0


class Deadline(BaseModel):
	"""When a call must be done by, on the monotonic clock. `at` is infinite for no limit."""

	model_config = ConfigDict(extra='forbid', frozen=True)

	started: float = Field(default_factory=time.monotonic)
	at: float = math.inf

	@classmethod
	def after(cls, seconds: float) -> 'Deadline':
		assert seconds > 0, 'a deadline is some time from now'
		now = time.monotonic()
		return cls(started=now, at=now + seconds)

	@classmethod
	def unbounded(cls) -> 'Deadline':
		return cls()

	@property
	def bounded(self) -> bool:
		return math.isfinite(self.at)

	@property
	def budget_s(self) -> float:
		return self.at - self.started

	def left(self) -> float:
		return self.at - time.monotonic()

	def spent(self) -> float:
		return time.monotonic() - self.started

	def allows(self, cost_s: float) -> bool:
		"""Whether there is time for something taking `cost_s` seconds."""
		return self.left() >= cost_s

	def clamp(self, timeout: float, reserve: float = 0.0) -> float:
		"""A wait of at most `timeout` that also ends `reserve` seconds before the deadline (never negative)."""
		return max(0.0, min(timeout, self.left() - reserve))

	def minus(self, reserve_s: float) -> 'Deadline':
		"""The same deadline, `reserve_s` earlier: for work that has more to do after the part it hands this to."""
		return self.model_copy(update={'at': self.at - reserve_s})


def stop_line(deadline: Deadline | None, why: str, covered: str, resume: dict[str, Any] | None) -> str:
	"""How a tool says it stopped before it was done: why, what it covered, and the arguments that go on from there.

	Appended after the result, never first: a result whose first line reads as a failure is turned into an error."""
	head = '⏳ out of time' if why == 'time' else f'⏹ {why}'
	if why == 'time' and deadline is not None and deadline.bounded:
		head += (
			f": stopped at {deadline.spent():.1f} s of this call's {deadline.budget_s:.0f} s"
			f' (MCP clients drop a call at {MCP_CLIENT_LIMIT_S:.0f} s)'
		)
	line = f'{head} after {covered}.'
	if resume:
		line += ' To go on, call again with ' + ', '.join(f'{k}={json.dumps(v)}' for k, v in resume.items()) + '.'
	return line
