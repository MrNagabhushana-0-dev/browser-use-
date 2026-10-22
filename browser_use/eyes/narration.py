"""Live narration: what a watcher says it saw, written per result, with the time it was written.

A model asked to narrate each watch result before the next one tends to watch on and write it all at the end (four
measured runs did). In live mode the watch tool owes each result a narration and refuses to watch again until it is
given, so narration arrives per result by construction. Every narration, every result it answers and every recording
start and stop go to one JSON-lines log with wall-clock times: the run's sidecar, from which narration latency is
measured and the narration is burned into the recording (scripts/contract/narrated_recording.py).
"""

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class NarrationLog:
	"""The owed narration and the log it is written to."""

	path: Path
	live: bool = False
	results: int = 0  # watch results returned while live
	due: dict[str, Any] | None = None  # the last result, while it waits for its narration
	narrated: int = 0
	_opened: bool = field(default=False, repr=False)

	def _write(self, kind: str, **fields: Any) -> None:
		self.path.parent.mkdir(parents=True, exist_ok=True)
		with self.path.open('a') as f:
			f.write(json.dumps({'kind': kind, 'wall': time.time(), **fields}) + '\n')

	def start(self) -> None:
		"""Turn live mode on: from the next result on, each one is owed a narration."""
		if not self.live:
			self.live = True
			self._write('live')

	def owes(self) -> bool:
		return self.live and self.due is not None

	def refusal(self) -> str:
		assert self.due is not None
		return (
			f'Narrate watch result {self.due["n"]} (covering {self.due["covering"]}) first: pass what its pictures showed as '
			'narration="[m:ss] what is seen" lines (one per visual event) with this call, or with retinat_narrate if you '
			'are not watching again. Nothing was watched.'
		)

	def result(self, covering: str, lag: float | None = None) -> None:
		"""A watch result went back to the model: it owes one narration for it. `lag` is how long before this its last
		sample was taken (the percept's build time)."""
		if not self.live:
			return
		self.results += 1
		self.due = {'n': self.results, 'covering': covering, 'returned': time.time()}
		self._write('result', n=self.results, covering=covering, lag=lag)

	def take(self, text: str) -> int:
		"""Log the narration of the owed result; returns that result's number (0 when none was owed)."""
		text = text.strip()
		assert text, 'an empty narration narrates nothing'
		due, self.due = self.due, None
		n = due['n'] if due else 0
		self.narrated += 1 if due else 0
		self._write(
			'narration', n=n, covering=due['covering'] if due else None, text=text, returned=due['returned'] if due else None
		)
		return n

	def recording(self, action: str, path: str) -> None:
		self._write('record', action=action, path=path)
