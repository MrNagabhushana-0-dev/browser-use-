"""INV-2, the retroactive-commit reservoir, at frame level (for a model whose context cannot be spliced).

The retina keeps every sample it takes in a bounded ring: frames ~10 a second, sound hops ~45 a second. None of it is
evidence for the model until a percept commits it. The reservoir is what makes "evidence cannot be missed" hold from
one call to the next: per item, a cursor at the last sample a percept took in. The next commit starts there however
long ago that was, so what played before the model was woken (by a gate, or by its own next call) is committed
retroactively instead of dropped. Where the ring already evicted samples past a cursor, or the backfill limit cut
them off, the loss is counted in seconds (erased_s), never silent.

Measured seed (E2, d3a6df1, protocol v2): resuming from the last sample taken instead of from when the percept was
done took film no window covered from 27.0 s to 0 in one run each way.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol


class _Sample(Protocol):
	vid: int
	wall: float


@dataclass
class Reservoir:
	"""Cursors over the retina's rings, one per item, and the evidence lost past them."""

	backfill_max_s: float
	cursor: dict[int, float] = field(default_factory=dict)  # item id -> wall (monotonic) of the last sample committed
	erased_s: float = 0.0  # all evidence lost past a cursor so far
	last_erased_s: float = 0.0  # what the latest start found lost

	def start(self, start: float, vid: int, rings: Sequence[Sequence[Any]], page_since: float) -> float:
		"""Where a commit for item `vid`, asked at `start`, begins: its cursor, or where the item began, never before the
		page or BACKFILL back. Counts as erased the item's samples between its cursor and that point (cut by the limit)
		and the span a full ring evicted past the cursor."""
		assert start > 0, start
		walls = [s.wall for ring in rings for s in ring if s.vid == vid] if vid else []
		self.last_erased_s = 0.0
		if not walls:
			return start
		floor = max(page_since, start - self.backfill_max_s)
		since = min(start, max(min(walls), self.cursor.get(vid, 0.0), floor))
		if vid in self.cursor:
			cut = [w for w in walls if self.cursor[vid] < w < floor]
			lost = (max(cut) - min(cut)) if len(cut) > 1 else 0.0
			for ring in rings:
				maxlen = getattr(ring, 'maxlen', None)
				if ring and maxlen and len(ring) >= maxlen and ring[0].wall > self.cursor[vid]:
					lost = max(lost, ring[0].wall - self.cursor[vid])  # a full ring dropped samples past the cursor
			self.last_erased_s = lost
			self.erased_s += lost
		assert since <= start
		return since

	def commit(self, samples: Sequence[_Sample], vids: Sequence[int], done_at: float) -> dict[int, float]:
		"""A percept took `samples` in for items `vids`: move each item's cursor to the newest sample it took (the next
		commit begins there), or to `done_at` for an item it took none of. Returns the new cursors."""
		newest: dict[int, float] = {}
		for s in samples:
			newest[s.vid] = max(newest.get(s.vid, 0.0), s.wall)
		for vid in vids:
			self.cursor[vid] = newest.get(vid, done_at)
		return {vid: self.cursor[vid] for vid in vids}
