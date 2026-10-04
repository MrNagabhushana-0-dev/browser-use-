"""Eyes: watch what is playing, move on like a person, and say what was seen and heard.

An agent that can only take screenshots experiences a video as a slideshow it has to ask
for one slide at a time, and it pays full image price for every slide. `Eyes` inverts that.
The retina watches continuously inside the page at no token cost; the agent asks for a
*percept* only when it wants to know something, and the percept is already compressed to the
moments that differ, with the sound laid out underneath.

    eyes = Eyes(browser_session)
    await eyes.open()
    p = await eyes.watch(until='bored', seconds=20)   # watch until nothing new is happening
    print(p.text)                                     # + p.image, one sheet
    await eyes.next()                                 # flick to the next reel, verified
    feed = await eyes.browse(items=5)                 # scroll a feed, one row per item

`watch(until=...)` is the closest a turn-based model gets to watching live:

- `'time'` returns after `seconds`.
- `'event'` returns as soon as something happens: a cut, a new item, a loop, the sound
  changing character (speech starting after silence, music dropping out).
- `'bored'` returns once the item stops showing anything new: it looped, or the marginal
  coverage of the last two seconds (see `sight.novelty`) fell below a threshold and the
  sound has not changed. This is the same test a person applies when they swipe away.
- `'item'` returns when the attended item changes (someone else scrolled).

`hold=True` pauses the video when the watch returns and resumes it at the start of the next
one, so nothing plays unseen while the agent is thinking.

Every action is verified by perception rather than assumed: `next()` flicks, then waits for
the retina to report a different item, and falls back from touch to the wheel to the
keyboard if the feed did not move.
"""

import asyncio
import io
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from browser_use.eyes import asr, hearing, sight
from browser_use.eyes.archive import FrameArchive
from browser_use.eyes.percept import ItemPercept, Keyframe, Percept, assemble, estimate_image_tokens, render_strip
from browser_use.eyes.retina import AudioHop, FrameSample, Retina, RetinaEvent
from browser_use.human.input import HumanInput
from browser_use.human.touch import HumanTouch

if TYPE_CHECKING:
	from browser_use.browser.session import BrowserSession

logger = logging.getLogger(__name__)

Until = Literal['time', 'event', 'bored', 'item']
Detail = Literal['glance', 'look', 'study']

# Below this marginal coverage for BORED_WINDOW_S, an item is showing nothing new.
BORED_NOVELTY = 0.04
BORED_WINDOW_S = 2.0
# How long `next()` waits for the feed to show a different item after one gesture.
NEXT_CONFIRM_S = 2.0
# ...and how long the new item must stay attended to count as where the feed came to rest.
SETTLE_S = 0.6
JOURNAL_MAX_BYTES = 512_000
# Keyframes per item on the sheet, by detail.
KEYFRAMES = {'glance': 4, 'look': 6, 'study': 8}


def default_now_path() -> Path:
	from browser_use.config import CONFIG

	return Path(os.environ.get('BROWSER_USE_EYES_NOW', str(CONFIG.BROWSER_USE_CONFIG_DIR / 'eyes' / 'now.json')))


@dataclass
class NextResult:
	moved: bool
	method: str  # 'swipe' | 'wheel' | 'key' | 'none'
	seconds: float
	tries: list[str]
	before: int
	after: int
	note: str = ''  # when it did not move: what the page itself reports


class Eyes:
	"""Perception and human input for one tab."""

	def __init__(
		self,
		browser_session: 'BrowserSession',
		*,
		audio: bool = True,
		speech: bool | None = None,
		fps: float = 10.0,
		now_path: Path | None | Literal[False] = None,
		archive: bool = True,
		seed: int | None = None,
	) -> None:
		self.browser_session = browser_session
		self.speech = asr.available() if speech is None else speech
		self.retina = Retina(browser_session, fps=fps, audio=audio, pcm=bool(self.speech and audio))
		self.touch = HumanTouch(browser_session, seed=seed)
		self.hand = HumanInput(browser_session, seed=seed)
		self.now_path = None if now_path is False else (now_path or default_now_path())
		# What changed, kept on disk between the model's turns (only changes, not every tick).
		self.journal_path = self.now_path.with_name('journal.jsonl') if self.now_path is not None else None
		self._journaled: dict[str, Any] = {}
		# Keyframes copied to disk as they are taken, so recall reaches past the page's ring.
		self.archive = FrameArchive(self.now_path.with_name('frames')) if archive and self.now_path is not None else None
		self._archiver: asyncio.Task | None = None
		self._meaning = None  # MeaningIndex over the archive, made on first search
		self._last_now = 0.0
		self._items_seen = 0
		self._pages = None

	# -- lifecycle -----------------------------------------------------------------------

	async def open(self, target_id: str | None = None) -> dict[str, Any]:
		state = await self.retina.start(target_id)
		if self.now_path is not None and self._update_now not in self.retina._listeners:
			self.retina.on_batch(self._update_now)
		if self.archive is not None and (self._archiver is None or self._archiver.done()):
			self._archiver = asyncio.create_task(self._archive_loop())
		return state

	async def close(self) -> None:
		if self._archiver is not None:
			self._archiver.cancel()
			try:
				await self._archiver
			except (asyncio.CancelledError, Exception):
				pass
			self._archiver = None
		if self._pages is not None:
			await self._pages.stop()
		await self.retina.stop()

	# -- watching ------------------------------------------------------------------------

	def _since(self, wall: float) -> tuple[list[FrameSample], list[AudioHop], list[RetinaEvent]]:
		return (
			[f for f in self.retina.frames if f.wall >= wall],
			[h for h in self.retina.hops if h.wall >= wall],
			[e for e in self.retina.events if e.wall >= wall],
		)

	def _salient(self, frames: list[FrameSample], hops: list[AudioHop], start_vid: int) -> str | None:
		"""Why a watch with until='event' should stop now, or None."""
		vid = self.retina.attended.get('vid', 0)
		if _moved_on(frames):
			return 'new item'
		mine = [f for f in frames if f.vid == vid]
		if len(mine) >= 3:
			s = sight.read(mine, self.retina.attended.get('duration'))
			if s.loops:
				return 'looped'
			if s.cuts:
				return f'cut at {sight.fmt_t(s.cuts[0])}'
		sound = [h for h in hops if h.vid == vid]
		if len(sound) >= 40:
			kinds = [seg.kind for seg in hearing.listen(sound, self.retina.state.get('sr')).segments if seg.duration >= 0.4]
			if len(set(kinds)) > 1:
				return f'sound changed ({" -> ".join(dict.fromkeys(kinds))})'
		return None

	def _bored(self, frames: list[FrameSample], hops: list[AudioHop], start_vid: int) -> str | None:
		vid = self.retina.attended.get('vid', 0)
		mine = [f for f in frames if f.vid == vid]
		if not mine:
			return None
		s = sight.read(mine, self.retina.attended.get('duration'))
		if s.loops:
			return 'looped (seen in full)'
		if self.retina.state.get('paused') and self.retina.state.get('vid') == vid:
			return 'video paused'
		now = mine[-1].wall
		recent = [f for f in mine if f.wall >= now - BORED_WINDOW_S]
		older = [f for f in mine if f.wall < now - BORED_WINDOW_S]
		if not older or mine[-1].wall - mine[0].wall < BORED_WINDOW_S * 1.5:
			return None
		gain = sight.novelty(older, recent)
		if gain >= BORED_NOVELTY:
			return None
		sound = [h for h in hops if h.vid == vid]
		recent_sound = [h for h in sound if h.wall >= now - BORED_WINDOW_S]
		older_sound = [h for h in sound if h.wall < now - BORED_WINDOW_S]
		if recent_sound:
			sr = self.retina.state.get('sr')
			after = {seg.kind for seg in hearing.listen(recent_sound, sr).segments if seg.duration >= 0.4}
			talking = 'speech' in after
			if self.speech and any(h.pcm for h in recent_sound):
				regions = asr.speech_regions(recent_sound)  # a few ms of CPU for 2 s of audio
				talking = bool(regions and sum(b - a for a, b in regions) >= 0.3)
			if talking:
				return None  # a talking head: the picture is steady, the information is in the words
			before = (
				{seg.kind for seg in hearing.listen(older_sound, sr).segments if seg.duration >= 0.4} if older_sound else set()
			)
			if after - before:
				return None  # the picture is steady but the sound is doing something new
		return f'nothing new for {BORED_WINDOW_S:.0f}s (novelty {gain:.3f})'

	async def watch(
		self,
		seconds: float = 8.0,
		until: Until = 'time',
		min_seconds: float = 1.5,
		detail: Detail = 'glance',
		keyframes: int | None = None,
		hold: bool = False,
		transcribe: bool | None = None,
	) -> Percept:
		"""Watch for up to `seconds`, stopping early per `until`, and return a percept."""
		assert seconds > 0, 'seconds must be positive'
		assert until in ('time', 'event', 'bored', 'item'), f'unknown until={until!r}'
		if not self.retina.running:
			await self.open()
		await self._resume_held()
		start = time.monotonic()
		start_vid = self.retina.attended.get('vid', 0)
		reason = f'watched {seconds:.0f}s'
		while True:
			elapsed = time.monotonic() - start
			if elapsed >= seconds:
				break
			await self.retina.wait_for_data(min(0.5, seconds - elapsed))
			if time.monotonic() - start < min_seconds:
				continue
			frames, hops, _ = self._since(start)
			why = None
			if until == 'item' and _moved_on(frames):
				why = 'new item'
			elif until == 'event':
				why = self._salient(frames, hops, start_vid)
			elif until == 'bored':
				why = self._bored(frames, hops, start_vid)
			if why:
				reason = why
				break
		if hold:
			await self._hold()
		percept = await self.perceive(since=start, detail=detail, keyframes=keyframes, transcribe=transcribe)
		percept.stop_reason = reason
		percept.started_at, percept.ended_at = start, time.monotonic()
		percept.text = percept.text.replace('{REASON}', reason)
		return percept

	async def perceive(
		self,
		since: float,
		detail: Detail = 'glance',
		keyframes: int | None = None,
		transcribe: bool | None = None,
		header: str | None = None,
	) -> Percept:
		"""Build a percept from everything the retina gathered since `since` (monotonic time)."""
		frames, hops, events = self._since(since)
		order: list[int] = []
		for vid in [f.vid for f in frames] + [h.vid for h in hops]:
			if vid and vid not in order:
				order.append(vid)
		info_by_vid: dict[int, dict] = {}
		for e in list(self.retina.events):
			if e.type == 'attend' and e.data.get('vid'):
				info_by_vid[e.data['vid']] = e.data
		muted_by_vid: dict[int, bool] = {}
		tainted: set[int] = set()
		for e in events:
			if e.type == 'state' and e.data.get('vid'):
				muted_by_vid[e.data['vid']] = bool(e.data.get('muted'))
			elif e.type == 'tainted':
				tainted.add(e.data.get('vid', 0))
		k = keyframes or KEYFRAMES[detail]
		do_speech = self.speech if transcribe is None else (transcribe and asr.available())
		items: list[ItemPercept] = []
		for vid in order:
			f = [x for x in frames if x.vid == vid]
			h = [x for x in hops if x.vid == vid]
			seen = sight.read(f, (info_by_vid.get(vid) or {}).get('duration'))
			heard = hearing.listen(h, self.retina.state.get('sr'))
			if do_speech and h and any(x.pcm for x in h):
				regions, said = await asyncio.to_thread(_speech, h)
				if regions is not None:
					hearing.apply_speech_regions(heard, regions)
				heard.transcript = said or []
			# Every shot deserves a keyframe if the budget can stretch that far (to twice `k`).
			first_pass = [sh for sh in seen.shots if not sh.after_loop]
			selection = sight.select_keyframes(f, min(2 * k, max(k, len(first_pass))))
			seqs = [f[i].seq for i in selection.indices]
			jpegs = await self.retina.keyframes(seqs) if seqs else []
			walls = [x.wall for x in f] + [x.wall for x in h]
			self._items_seen = max(self._items_seen, vid)
			items.append(
				ItemPercept(
					index=len(items) + 1,
					vid=vid,
					info=info_by_vid.get(vid, {}),
					frames=f,
					hops=h,
					sight=seen,
					hearing=heard,
					keyframes=[Keyframe(f[i].t, f[i].seq, j) for i, j in zip(selection.indices, jpegs)],
					coverage=selection.coverage,
					watched_s=(max(walls) - min(walls)) if walls else 0.0,
					muted=muted_by_vid.get(vid),
					tainted=vid in tainted,
				)
			)
		page = self.retina.state.get('url', '')
		head = header or f'👁 {len(items)} item(s) watched on {page[:120]} · stopped: {{REASON}}'
		return assemble(items, head, detail)

	async def look(self, detail: Detail = 'look', seconds: float = 2.0) -> Percept:
		"""What is on screen now. A playing video is watched briefly; otherwise the page itself is
		shown as the compositor draws it (canvas and WebGL included), in one frame."""
		if not self.retina.running:
			await self.open()
		await self.retina.wait_for_data(0.6)
		if self.retina.attended.get('vid'):
			return await self.watch(seconds=seconds, until='time', min_seconds=seconds, detail=detail, keyframes=2)
		jpeg = await self._page_watcher().wait_latest()
		url = self.retina.state.get('url', '')
		text = f'👁 no video playing on {url[:120]}; this is the page as drawn now (a compositor frame, not a screenshot call)'
		if not jpeg:
			return Percept([], text + '\n    (no frame arrived)', None)
		from PIL import Image

		with Image.open(io.BytesIO(jpeg)) as img:
			size = img.size
		tokens = estimate_image_tokens(*size)
		text += f'\n~{tokens + len(text) // 4} tokens (frame {size[0]}x{size[1]} ~{tokens}; estimates)'
		return Percept([], text, jpeg, size, tokens, len(text) // 4)

	async def archive_now(self, limit: int = 40) -> int:
		"""Copy keyframes not yet on disk from the page's ring to the archive. Returns how many."""
		if self.archive is None:
			return 0
		pending = [f for f in self.retina.frames if f.has_keyframe and not self.archive.has(f.vid, f.seq)][-limit:]
		if not pending:
			return 0
		jpegs = await self.retina.keyframes([f.seq for f in pending])
		stored = 0
		for f, jpeg in zip(pending, jpegs):
			if jpeg:
				self.archive.add(f, jpeg)
				stored += 1
		return stored

	async def _archive_loop(self, every_s: float = 2.0) -> None:
		while True:
			await asyncio.sleep(every_s)
			try:
				await self.archive_now()
			except asyncio.CancelledError:
				raise
			except Exception as e:  # the page navigated or closed; try again next tick
				logger.debug(f'eyes: archive tick failed: {type(e).__name__}: {e}')

	def held(self, item: int | None = None) -> tuple[float, float] | None:
		"""The media-time span (first, last) of one item's frames that still have a keyframe."""
		vid = item if item is not None else self.retina.attended.get('vid', 0)
		times = [f.t for f in self.retina.frames if f.vid == vid and f.has_keyframe]
		on_disk = self.archive.span(vid) if self.archive is not None else None
		if on_disk:
			times += list(on_disk)
		return (min(times), max(times)) if times else None

	async def recall(self, t0: float, t1: float, frames: int = 4, item: int | None = None) -> Percept:
		"""Frames from a moment already seen, by media time: the model pulls what it needs.

		`watch` and `browse` push a sheet the eyes chose without knowing the question; this asks
		for "t0 to t1" of the attended item (or `item`) and answers from what the retina kept,
		choosing the frames that best cover that window. It never seeks or replays the video.
		"""
		assert t1 >= t0 and frames >= 1, 'recall needs t1 >= t0 and at least one frame'
		vid = item if item is not None else self.retina.attended.get('vid', 0)
		window = [f for f in self.retina.frames if f.vid == vid and t0 <= f.t <= t1]
		if self.archive is not None:  # moments that have left the page's ring, or an earlier session
			in_memory = {f.seq for f in window}
			window += [f for f in self.archive.window(vid, t0, t1) if f.seq not in in_memory]
			window.sort(key=lambda f: f.t)
		span = self.held(vid)
		held = f'held: {sight.fmt_t(span[0])}-{sight.fmt_t(span[1])}' if span else 'held: nothing for this item'
		head = f'👁 recall {sight.fmt_t(t0)}-{sight.fmt_t(t1)} of item {vid}'
		chosen = sight.select_keyframes(window, frames).indices if window else []
		picked = sorted((window[i] for i in chosen), key=lambda f: f.t)
		from_disk = {f.seq: self.archive.read(vid, f.seq) for f in picked} if self.archive is not None else {}
		live = [f for f in picked if not from_disk.get(f.seq)]
		fetched = dict(zip([f.seq for f in live], await self.retina.keyframes([f.seq for f in live]))) if live else {}
		got = [(f.t, j) for f in picked if (j := from_disk.get(f.seq) or fetched.get(f.seq))]
		if not got:
			why = 'nothing held between those times' if not picked else 'those keyframes were evicted from the ring'
			return Percept([], f'{head}: {why} ({held})', None)
		strip = render_strip(got)
		assert strip is not None
		jpeg, w, h = strip
		tokens = estimate_image_tokens(w, h)
		evicted = len(picked) - len(got)
		text = (
			f'{head}: {len(got)} frame(s) at '
			+ ', '.join(sight.fmt_t(t) for t, _ in got)
			+ (f'; {evicted} evicted' if evicted else '')
			+ f' ({held})'
			+ f'\n~{tokens + 30} tokens (strip {w}x{h} ~{tokens}; estimates)'
		)
		return Percept([], text, jpeg, (w, h), tokens, len(text) // 4, frames=got)

	async def search(self, query: str, frames: int = 4, item: int | None = None) -> Percept:
		"""Frames from anything archived that best match a description in words.

		Uses an open image-text model locally (see `meaning.py`); the vectors live beside the
		archive, so only the matching frames reach the model. Needs the `eyes` extra and a one-off
		model download (~300 MB). Scores are cosine similarities: compare them, do not read them
		as probabilities.
		"""
		assert query.strip() and frames >= 1, 'search needs words and at least one frame'
		head = f'👁 search "{query[:80]}"'
		if self.archive is None:
			return Percept([], f'{head}: no archive (Eyes was created with archive=False or no now_path)', None)
		await self.archive_now(limit=500)
		if self._meaning is None:
			from browser_use.eyes.meaning import MeaningIndex

			self._meaning = MeaningIndex(self.archive)
		index = self._meaning
		added = await asyncio.to_thread(index.update)
		hits = await asyncio.to_thread(index.search, query, frames, item)
		got = [(t, jpeg, score, vid) for score, vid, seq, t in hits if (jpeg := self.archive.read(vid, seq))]
		if not got:
			return Percept([], f'{head}: nothing archived yet to search ({len(index)} frames indexed)', None)
		strip = render_strip([(t, jpeg) for t, jpeg, _, _ in got])
		assert strip is not None
		jpeg, w, h = strip
		tokens = estimate_image_tokens(w, h)
		listing = ', '.join(f'item {vid} at {sight.fmt_t(t)} ({score:.3f})' for t, _, score, vid in got)
		text = (
			f'{head}: best {len(got)} of {len(index)} archived frames'
			+ (f' ({added} newly indexed)' if added else '')
			+ f': {listing}\n~{tokens + 30} tokens (strip {w}x{h} ~{tokens}; estimates). '
			+ 'Use retinat_recall around a time for more frames of that moment.'
		)
		return Percept([], text, jpeg, (w, h), tokens, len(text) // 4, frames=[(t, j) for t, j, _, _ in got])

	def _page_watcher(self):
		from browser_use.eyes.page import PageWatcher

		if self._pages is None:
			self._pages = PageWatcher(self.browser_session, self.hand)
		return self._pages

	async def scan(self, max_screens: int = 25, keyframes: int = 6) -> Percept:
		"""Scroll the whole page like a reader while watching the rendered frames, and return the
		few that cover everything seen - canvas, WebGL and scroll-driven animation included."""
		from browser_use.eyes.page import scan_sheet

		watcher = self._page_watcher()
		result = await watcher.scan(max_screens=max_screens, keyframes=keyframes)
		sheet = scan_sheet(result)
		vh = max(1, result.viewport[1])
		lines = [
			f'👁 scanned {self.retina.state.get("url", "")[:120]}: {result.page_height / vh:.1f} screens tall, '
			f'scrolled {result.screens} times in {result.seconds:.1f}s, {len(result.frames)} frames seen',
			f'    sheet: {len(result.keyframes)} frames at '
			+ ', '.join(f'{k.scroll_y / vh:.1f}' for k in result.keyframes)
			+ f' screens down (cover {result.coverage:.0%} of what was seen)',
		]
		if result.moving:
			lines.append(
				'    moves on its own (animation/canvas) at '
				+ ', '.join(f'{y / vh:.1f}' for y in result.moving[:8])
				+ ' screens down'
			)
		image, size, tokens = (
			(sheet[0], (sheet[1], sheet[2]), estimate_image_tokens(sheet[1], sheet[2])) if sheet else (None, None, 0)
		)
		text = '\n'.join(lines)
		if size:
			text += f'\n~{tokens + len(text) // 4} tokens (sheet {size[0]}x{size[1]} ~{tokens}; estimates)'
		return Percept([], text, image, size, tokens, len(text) // 4)

	async def _hold(self) -> None:
		try:
			await self.retina.evaluate('window.__retina.hold()')
		except Exception as e:
			logger.debug(f'eyes: hold failed: {e}')

	async def _resume_held(self) -> None:
		try:
			await self.retina.evaluate('window.__retina.resume()')
		except Exception as e:
			logger.debug(f'eyes: resume failed: {e}')

	# -- acting --------------------------------------------------------------------------

	async def _wait_for_item_change(self, before: int, timeout: float) -> bool:
		"""True once a different item is attended *and has stayed attended* for SETTLE_S.

		A feed scrolling past items attends each of them in turn; judging at the first change
		would report where the scroll was passing through, not where it came to rest.
		"""
		deadline = time.monotonic() + timeout
		current, since = self.retina.attended.get('vid', 0), time.monotonic()
		while time.monotonic() < deadline + SETTLE_S:
			vid = self.retina.attended.get('vid', 0)
			if vid != current:
				current, since = vid, time.monotonic()
			if current not in (before, 0) and time.monotonic() - since >= SETTLE_S:
				return True
			if current in (before, 0) and time.monotonic() >= deadline:
				return False
			await self.retina.wait_for_data(0.1)
		return current not in (before, 0)

	async def _correct_overshoot(self, before_order: int | None, direction: str) -> str:
		"""If the flick carried the feed past the next item, flick back once, as a person would.

		Judged from the videos' document order, which is how a feed lays out its items. When the
		order is unknown (no previous position, or a feed that recycles elements) nothing is
		assumed and nothing is done.
		"""
		after_order = self.retina.attended.get('order')
		if not isinstance(before_order, int) or not isinstance(after_order, int) or before_order < 0 or after_order < 0:
			return ''
		step = after_order - before_order if direction == 'down' else before_order - after_order
		if step <= 1:
			return ''
		current = self.retina.attended.get('vid', 0)
		w, h = await self.touch.viewport()
		await self.touch.flick('down' if direction == 'down' else 'up', fraction=0.45, around=(w / 2, h * 0.5))
		if await self._wait_for_item_change(current, NEXT_CONFIRM_S):
			return f'overshot by {step - 1} item(s); flicked back'
		return f'overshot by {step - 1} item(s); the flick back did not move the feed'

	async def next(
		self, direction: Literal['down', 'up'] = 'down', methods: tuple[str, ...] = ('swipe', 'long-swipe', 'wheel', 'key')
	) -> NextResult:
		"""Move the feed to the next (or previous) item like a person, and confirm it moved."""
		if not self.retina.running:
			await self.open()
		await self._resume_held()
		before = self.retina.attended.get('vid', 0)
		before_order = self.retina.attended.get('order')
		started = time.monotonic()
		tried: list[str] = []
		rect = self.retina.attended.get('rect') or None
		for method in methods:
			tried.append(method)
			if method in ('swipe', 'long-swipe'):
				# A thumb flicks from the same place on the glass every time: the vertical middle
				# of the screen. Only the horizontal centre of the video is taken from the page;
				# its vertical position is from when it was first attended, often mid-scroll,
				# and aiming there clamps the stroke against the screen edge until it is too short
				# to carry the feed past half an item.
				w, h = await self.touch.viewport()
				x = w / 2
				if rect:
					rx, _ry, rw, _rh = rect
					if 0 < rx + rw / 2 < w:
						x = rx + rw / 2
				fraction = 0.55 if method == 'swipe' else 0.72
				await self.touch.flick('up' if direction == 'down' else 'down', fraction=fraction, around=(x, h * 0.5))
			elif method == 'wheel':
				w, h = await self.touch.viewport()
				await self.hand.move_to(w * 0.5, h * 0.5)
				await self.hand.wheel(h * 0.9 if direction == 'down' else -h * 0.9)
			elif method == 'key':
				await self.hand.press('ArrowDown' if direction == 'down' else 'ArrowUp')
			if await self._wait_for_item_change(before, NEXT_CONFIRM_S):
				after = self.retina.attended.get('vid', 0)
				note = await self._correct_overshoot(before_order, direction)
				return NextResult(
					True, method, time.monotonic() - started, tried, before, self.retina.attended.get('vid', after), note
				)
		# Before concluding the feed is stuck, ask the page directly: if the retina in the page
		# attends a different item than the pushed batches say, the channel lagged, not the feed.
		note = ''
		try:
			state = await self.retina.page_state()
			page_vid = state.get('vid', 0)
			if page_vid not in (before, 0):
				self.retina.attended = state.get('attended') or self.retina.attended
				return NextResult(
					True, tried[-1], time.monotonic() - started, tried, before, page_vid, 'confirmed by asking the page'
				)
			note = f'page reports item {page_vid} (same as before), batches received: {self.retina.batches}'
		except Exception as e:
			note = f'page state unavailable: {type(e).__name__}: {e}'
		return NextResult(False, 'none', time.monotonic() - started, tried, before, self.retina.attended.get('vid', 0), note)

	async def browse(
		self,
		items: int = 5,
		max_seconds: float = 15.0,
		min_seconds: float = 3.0,
		detail: Detail = 'glance',
		keyframes: int = 4,
		hold: bool = True,
	) -> Percept:
		"""Scroll a feed: watch each item until bored (or `max_seconds`), then flick on."""
		assert items >= 1, 'browse at least one item'
		if not self.retina.running:
			await self.open()
		start = time.monotonic()
		log: list[str] = []
		for i in range(items):
			p = await self.watch(seconds=max_seconds, until='bored', min_seconds=min_seconds, detail=detail, keyframes=keyframes)
			log.append(f'item {i + 1}: {p.stop_reason}')
			if i + 1 < items:
				moved = await self.next()
				log.append(
					f'  -> next by {moved.method} in {moved.seconds:.1f}s'
					if moved.moved
					else f'  -> feed did not move ({moved.note}); stopping'
				)
				if not moved.moved:
					break
		if hold:
			await self._hold()
		percept = await self.perceive(since=start, detail=detail, keyframes=keyframes)
		percept.text = percept.text.replace('{REASON}', f'browsed {len(log) - sum(1 for x in log if x.startswith("  "))} item(s)')
		percept.text += '\n' + '\n'.join(log)
		percept.stop_reason = 'browsed'
		return percept

	async def tap(self, x: float, y: float) -> None:
		await self.touch.tap(x, y)

	async def swipe(self, direction: Literal['up', 'down', 'left', 'right'] = 'up', fraction: float = 0.55) -> dict[str, float]:
		return await self.touch.flick(direction, fraction=fraction)

	async def listen(self, on: bool = True) -> dict[str, Any]:
		"""Make the attended video audible to the person. Hearing does not need this."""
		return await self.retina.set_listen(on)

	# -- ambient -------------------------------------------------------------------------

	def _now_fields(self, window_s: float = 3.0) -> dict[str, Any]:
		"""What is on screen and audible right now, as fields (the journal diffs these)."""
		att = self.retina.attended or {}
		state = self.retina.state
		fields: dict[str, Any] = {'url': state.get('url', ''), 'vid': att.get('vid', 0) or 0}
		if not fields['vid']:
			return fields
		vid = fields['vid']
		cutoff = time.monotonic() - window_s
		frames = [f for f in self.retina.frames if f.vid == vid and f.wall >= cutoff]
		hops = [h for h in self.retina.hops if h.vid == vid and h.wall >= cutoff]
		fields.update(
			caption=(att.get('text') or '').strip(),
			t=state.get('t'),
			duration=att.get('duration'),
			paused=bool(state.get('paused')),
		)
		if frames:
			s = sight.read(frames)
			fields['shot'] = sight.describe_shot(s.shots[-1])
			fields['cuts'] = len(s.cuts)
		if hops:
			h = hearing.listen(hops, state.get('sr'))
			if self.speech and any(x.pcm for x in hops):
				regions = asr.speech_regions(hops)
				if regions is not None:
					hearing.apply_speech_regions(h, regions)
			last = [seg for seg in h.segments if seg.duration >= 0.3]
			if last:
				fields['sound'] = last[-1].kind
				fields['sound_text'] = hearing.describe_segment(last[-1])
		return fields

	def now_line(self, window_s: float = 3.0, fields: dict[str, Any] | None = None) -> str:
		"""One line describing what is on screen and audible right now."""
		f = fields if fields is not None else self._now_fields(window_s)
		if not f.get('vid'):
			return f'👁 no video on screen ({f.get("url", "")[:100]})'
		parts = ['👁 watching a video']
		if f.get('caption'):
			parts[0] += f' "{f["caption"][:80]}"'
		if f.get('t') is not None:
			parts.append(f'at {sight.fmt_t(f["t"])}' + (f' of {sight.fmt_t(f["duration"])}' if f.get('duration') else ''))
		if f.get('paused'):
			parts.append('paused')
		if 'shot' in f:
			parts.append(f['shot'] + (f', {f["cuts"]} cut(s) in {window_s:.0f}s' if f.get('cuts') else ''))
		if 'sound_text' in f:
			parts.append('sound: ' + f['sound_text'])
		return ' · '.join(parts)

	def _journal(self, f: dict[str, Any]) -> None:
		"""Append what changed since the last entry: page, item, sound or play state. Never every tick."""
		if self.journal_path is None:
			return
		last, entries = self._journaled, []
		vid, t = f.get('vid', 0), f.get('t')
		if f.get('url') != last.get('url') and f.get('url'):
			entries.append(('page', f'opened {f["url"][:160]}'))
		if vid != last.get('vid'):
			if vid:
				what = f'"{f["caption"][:80]}"' if f.get('caption') else 'a video'
				length = f' ({sight.fmt_t(f["duration"])} long)' if f.get('duration') else ''
				entries.append(('item', f'now watching {what}{length}'))
			elif last.get('vid'):
				entries.append(('item', 'no video on screen'))
		elif vid:
			if f.get('sound') and f.get('sound') != last.get('sound'):
				entries.append(('sound', f'sound became {f["sound_text"]}'))
			if 'paused' in last and f.get('paused') != last.get('paused'):
				entries.append(('state', 'paused' if f.get('paused') else 'playing again'))
		self._journaled = {**last, **{k: f.get(k) for k in ('url', 'vid', 'sound', 'paused')}}
		if not entries:
			return
		at = time.time()
		lines = [json.dumps({'at': at, 'kind': k, 'vid': vid, 't': t, 'text': text}) for k, text in entries]
		self.journal_path.parent.mkdir(parents=True, exist_ok=True)
		with self.journal_path.open('a') as out:
			out.write('\n'.join(lines) + '\n')
		if self.journal_path.stat().st_size > JOURNAL_MAX_BYTES:  # keep the newest half
			kept = self.journal_path.read_text().splitlines()
			self.journal_path.write_text('\n'.join(kept[len(kept) // 2 :]) + '\n')

	def _update_now(self, frames: list[FrameSample], hops: list[AudioHop], events: list[RetinaEvent]) -> None:
		now = time.monotonic()
		if self.now_path is None or now - self._last_now < 1.0:
			return
		self._last_now = now
		try:
			fields = self._now_fields()
			line = self.now_line(fields=fields)
			self._journal(fields)
			self.now_path.parent.mkdir(parents=True, exist_ok=True)
			tmp = self.now_path.with_suffix('.tmp')
			tmp.write_text(json.dumps({'updated': time.time(), 'line': line, 'url': self.retina.state.get('url', '')}))
			tmp.replace(self.now_path)
		except Exception as e:
			logger.debug(f'eyes: could not write {self.now_path}: {type(e).__name__}: {e}')


def _moved_on(frames: list[FrameSample]) -> bool:
	"""True once frames from a second item arrive: we were watching one thing and now another.

	Judged from the frames themselves, not from which item was attended when the watch began,
	which right after a navigation can still be the previous page's.
	"""
	return len({f.vid for f in frames}) >= 2


def _speech(hops: list[AudioHop]) -> tuple[list[tuple[float, float]] | None, list[asr.Utterance] | None]:
	regions = asr.speech_regions(hops)
	said = asr.transcribe(hops) if regions else []
	return regions, said
