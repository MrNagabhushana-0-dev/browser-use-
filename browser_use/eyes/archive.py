"""Keyframes kept on disk, so recall reaches back hours instead of the page's last 240 frames.

The retina's ring of JPEG keyframes lives in the page and holds about two minutes. While the eyes
are open, an archiver copies each new keyframe here with what recall needs to choose among them:
item id, media time and the 16x16 signature. A new session (or a new `Eyes`) reads the same
directory, so a moment seen earlier can still be pulled. The directory is capped in bytes; the
oldest frames go first. Frames are of whatever the person watched, stored only on this machine.
"""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path

from browser_use.eyes.retina import FrameSample

DEFAULT_MAX_BYTES = 200 * 1024 * 1024  # ~10k keyframes at 320 px


class FrameArchive:
	"""An append-only, size-capped store of keyframes with an index for recall."""

	def __init__(self, root: Path, max_bytes: int = DEFAULT_MAX_BYTES) -> None:
		assert max_bytes > 0, 'max_bytes must be positive'
		self.root = Path(root)
		self.max_bytes = max_bytes
		self.index_path = self.root / 'index.jsonl'
		self._entries: list[dict] = []
		self._keys: set[tuple[int, int]] = set()
		self._bytes = 0
		self._load()

	def __len__(self) -> int:
		return len(self._entries)

	def _load(self) -> None:
		try:
			lines = self.index_path.read_text().splitlines()
		except FileNotFoundError:
			return
		for raw in lines:
			try:
				e = json.loads(raw)
			except ValueError:
				continue
			if (self.root / e['file']).exists():
				self._entries.append(e)
				self._keys.add((e['vid'], e['seq']))
				self._bytes += e.get('bytes', 0)

	def has(self, vid: int, seq: int) -> bool:
		return (vid, seq) in self._keys

	def add(self, sample: FrameSample, jpeg: bytes) -> None:
		"""Store one keyframe (idempotent), then drop the oldest until under the cap."""
		if self.has(sample.vid, sample.seq):
			return
		self.root.mkdir(parents=True, exist_ok=True)
		name = f'{sample.vid}-{sample.seq}.jpg'
		(self.root / name).write_bytes(jpeg)
		entry = {
			'vid': sample.vid,
			'seq': sample.seq,
			't': sample.t,
			'at': time.time(),
			'file': name,
			'bytes': len(jpeg),
			'luma': base64.b64encode(sample.luma).decode(),
			'rgb': list(sample.rgb),
		}
		with self.index_path.open('a') as out:
			out.write(json.dumps(entry) + '\n')
		self._entries.append(entry)
		self._keys.add((sample.vid, sample.seq))
		self._bytes += len(jpeg)
		if self._bytes > self.max_bytes:
			self._evict()

	def _evict(self) -> None:
		while self._entries and self._bytes > self.max_bytes:
			old = self._entries.pop(0)
			self._keys.discard((old['vid'], old['seq']))
			self._bytes -= old.get('bytes', 0)
			(self.root / old['file']).unlink(missing_ok=True)
		tmp = self.index_path.with_suffix('.tmp')
		tmp.write_text(''.join(json.dumps(e) + '\n' for e in self._entries))
		tmp.replace(self.index_path)

	def window(self, vid: int, t0: float, t1: float) -> list[FrameSample]:
		"""Archived keyframes of item `vid` with media time in [t0, t1], as samples recall can rank."""
		return [
			FrameSample(e['seq'], e['vid'], e['t'], 0.0, base64.b64decode(e['luma']), tuple(e['rgb']), True)  # type: ignore[arg-type]
			for e in self._entries
			if e['vid'] == vid and t0 <= e['t'] <= t1
		]

	def span(self, vid: int) -> tuple[float, float] | None:
		times = [e['t'] for e in self._entries if e['vid'] == vid]
		return (min(times), max(times)) if times else None

	def read(self, vid: int, seq: int) -> bytes | None:
		if not self.has(vid, seq):
			return None
		try:
			return (self.root / f'{vid}-{seq}.jpg').read_bytes()
		except FileNotFoundError:
			return None
