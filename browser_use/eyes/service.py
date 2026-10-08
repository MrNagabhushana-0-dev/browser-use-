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
import base64
import io
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from browser_use.eyes import asr, hearing, motion, sight
from browser_use.eyes.archive import FrameArchive
from browser_use.eyes.percept import (
	ItemPercept,
	Keyframe,
	Percept,
	assemble,
	estimate_image_tokens,
	page_text_note,
	render_strip,
)
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
# Discrete sounds this recent (beeps, knocks, notifications) keep a still picture worth watching.
BORED_SOUND_S = 4.0
# How far back a watch reaches for what the item did before the call (the caller's own latency, or a question
# asked long after). The retina's rings hold ~130 s of sound (MAX_HOPS) and ~150 s of frames at 10 fps.
BACKFILL_MAX_S = 120.0
# Seconds of speech or music below which onsets still count as discrete sounds (a heuristic blip is not talk).
VOICED_VETO_S = 2.0
# How long `next()` waits for the feed to show a different item after one gesture.
NEXT_CONFIRM_S = 2.0
# ...and how long the new item must stay attended to count as where the feed came to rest.
SETTLE_S = 0.6
JOURNAL_MAX_BYTES = 512_000
ARCHIVE_BACKLOG_LIMIT = 150  # unarchived keyframes; the page's ring holds 240
ARCHIVE_BACKOFF_S = 30.0  # after the page failed to hand over keyframes, archiving leaves it alone this long
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
		self._pending_text: list[str] = []  # text that appeared since the last journal write
		# Keyframes copied to disk as they are taken, so recall reaches past the page's ring.
		self.archive = FrameArchive(self.now_path.with_name('frames')) if archive and self.now_path is not None else None
		self._archiver: asyncio.Task | None = None
		self._watching = 0  # watches in progress: the archiver keeps out of their way
		self._archive_quiet_until = 0.0  # monotonic time before which archiving does not ask the page again
		self._meaning = None  # MeaningIndex over the archive, made on first search
		self._last_now = 0.0
		self._items_seen = 0
		self._reported: dict[int, float] = {}  # item id -> when a percept last covered it
		self._last_percept_end = 0.0
		self._sounds_noted: dict[int, str] = {}  # item id -> the distinct-sounds line last journaled for it
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

	def _unreported_since(self, start: float, vid: int) -> float:
		"""Where a watch of item `vid` starting at `start` picks up: the end of the last percept of it, or where it
		began, up to BACKFILL_MAX_S back. Starting at the call would leave the caller's latency as a blind gap: a beep
		or a toast between opening a page and the first watch would never be reported."""
		if not vid:
			return start
		walls = [f.wall for f in self.retina.frames if f.vid == vid] + [h.wall for h in self.retina.hops if h.vid == vid]
		if not walls:
			return start
		return min(start, max(min(walls), self._reported.get(vid, 0.0), self.retina.page_since, start - BACKFILL_MAX_S))

	def _text_since(self, start: float) -> float:
		"""Text that appeared after the last percept, on this page, up to BACKFILL_MAX_S back, is still news."""
		return min(start, max(self._last_percept_end, self.retina.page_since, start - BACKFILL_MAX_S))

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
		lately = [h for h in sound if h.wall >= now - BORED_SOUND_S]
		if lately and hearing.listen(lately, self.retina.state.get('sr')).onsets:
			return None  # discrete sounds still coming: the next beep is as unpredictable as the last
		return f'nothing new for {BORED_WINDOW_S:.0f}s (novelty {gain:.3f})'

	async def watch(self, *args: Any, **kwargs: Any) -> Percept:
		"""Watch what plays: see `_watch` for the parameters."""
		self._watching += 1
		try:
			return await self._watch(*args, **kwargs)
		finally:
			self._watching -= 1

	async def browse(self, *args: Any, **kwargs: Any) -> Percept:
		"""Browse a feed item by item: see `_browse` for the parameters."""
		self._watching += 1
		try:
			return await self._browse(*args, **kwargs)
		finally:
			self._watching -= 1

	async def _watch(
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
		since = self._unreported_since(start, start_vid)
		text_since = self._text_since(start)
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
		percept = await self.perceive(
			since=since, detail=detail, keyframes=keyframes, transcribe=transcribe, text_since=text_since, watch_start=start
		)
		if not percept.items and not self.retina.attended.get('vid'):
			await self._show_page(percept)
		percept.stop_reason = reason
		percept.started_at, percept.ended_at = start, time.monotonic()
		self._last_percept_end = percept.ended_at
		for item in percept.items:
			self._reported[item.vid] = percept.ended_at
		percept.text = percept.text.replace('{REASON}', reason)
		return percept

	async def perceive(
		self,
		since: float,
		detail: Detail = 'glance',
		keyframes: int | None = None,
		transcribe: bool | None = None,
		header: str | None = None,
		text_since: float | None = None,
		order: list[int] | None = None,
		watch_start: float | None = None,
	) -> Percept:
		"""Build a percept from everything the retina gathered since `since` (monotonic time), and the page text
		that appeared since `text_since` (default: `since`). `order` puts these items first, in this order."""
		frames, hops, events = self._since(since)
		if watch_start is not None and (frames or hops):
			# Backfill reaches before the watch, and the page boundary it is clamped to is learnt from a heartbeat that
			# can lag a navigation: what an earlier page showed then must not come back as this page's. An item id
			# carries its document (see retina.js nextItem), so samples from another document before the watch are
			# dropped; ones the watch itself saw (a navigation by the page, mid-watch) stay.
			latest = max([*frames, *hops], key=lambda x: x.wall).vid // 1000
			frames = [f for f in frames if f.vid // 1000 == latest or f.wall >= watch_start]
			hops = [h for h in hops if h.vid // 1000 == latest or h.wall >= watch_start]
		seen = [vid for vid in dict.fromkeys([f.vid for f in frames] + [h.vid for h in hops]) if vid]
		# Items in the given order (what was watched, in turn), then anything else seen, in order of first sight.
		order = [vid for vid in (order or []) if vid in seen] + [vid for vid in seen if vid not in (order or [])]
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
		trouble: str | None = None
		for vid in order:
			f = [x for x in frames if x.vid == vid]
			h = [x for x in hops if x.vid == vid]
			seen = sight.read(f, (info_by_vid.get(vid) or {}).get('duration'))
			heard = hearing.listen(h, self.retina.state.get('sr'))
			untranscribed = None
			if do_speech and h and any(x.pcm for x in h):
				regions, said = await asyncio.to_thread(_speech, h)
				if regions is not None:
					hearing.apply_speech_regions(heard, regions)
				heard.transcript = said or []
			elif transcribe and heard.heard:
				# Asked for words and none can come: say why, rather than look as if nothing was said.
				untranscribed = (
					'the speech extra (faster-whisper) is not installed'
					if not asr.available()
					else 'these eyes were opened with speech off, so no raw audio was kept for the speech model'
				)
			# Every shot deserves a keyframe if the budget can stretch that far (to twice `k`).
			first_pass = [sh for sh in seen.shots if not sh.after_loop]
			selection = sight.select_keyframes(f, min(2 * k, max(k, len(first_pass))))
			seqs = [f[i].seq for i in selection.indices]
			# Once the page has failed to hand over images in this percept, do not wait on it again per item.
			if trouble:
				jpegs: list[bytes | None] = [None] * len(seqs)
			else:
				jpegs, trouble = await self.retina.read_keyframes(seqs)
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
					motion=motion.track(f),
					deaf=deaf_spans(events, vid),
					untranscribed=untranscribed,
				)
			)
		page = self.retina.state.get('url', '')
		head = header or f'👁 {len(items)} item(s) watched on {page[:120]} · stopped: {{REASON}}'
		head += _text_lines(
			self.retina.events, since if text_since is None else text_since, self.retina.page_since, fresh_from=watch_start
		)
		if trouble:
			head += f'\n    no keyframe images: {trouble}'
		return assemble(items, head, detail)

	async def _show_page(self, percept: Percept) -> None:
		"""No video in the percept: attach the page as the compositor draws it, so a watch still shows the screen."""
		jpeg = await self._page_watcher().wait_latest()
		if not jpeg:
			return
		from PIL import Image

		with Image.open(io.BytesIO(jpeg)) as img:
			size = img.size
		percept.image, percept.image_size, percept.image_tokens = jpeg, size, estimate_image_tokens(*size)
		percept.text += f'\n    (no video: the page as drawn now, {size[0]}x{size[1]} ~{percept.image_tokens} tokens)'

	async def zoom(self, x: float, y: float, width: float, height: float, max_width: int = 1200) -> Percept:
		"""A region of the viewport (CSS px, as on the look images and for clicks) captured fresh at up to 4x, so small
		print is redrawn at that size, not upscaled from the ~640 px frame a look sends."""
		assert width > 0 and height > 0, 'the region needs a size'
		cdp = await self.browser_session.get_or_create_cdp_session(focus=False)
		metrics = await cdp.cdp_client.send.Page.getLayoutMetrics(session_id=cdp.session_id)
		vp = metrics['cssLayoutViewport']
		x, y = max(0.0, x), max(0.0, y)
		width, height = min(width, vp['clientWidth'] - x), min(height, vp['clientHeight'] - y)
		assert width > 0 and height > 0, 'the region is outside the viewport'
		scale = max(1.0, min(4.0, max_width / width))
		clip = {'x': vp['pageX'] + x, 'y': vp['pageY'] + y, 'width': width, 'height': height, 'scale': scale}
		shot = await cdp.cdp_client.send.Page.captureScreenshot(
			params={'format': 'jpeg', 'quality': 85, 'clip': clip},  # type: ignore[typeddict-item]
			session_id=cdp.session_id,
		)
		jpeg = base64.b64decode(shot['data'])
		from PIL import Image

		with Image.open(io.BytesIO(jpeg)) as img:
			size = img.size
		tokens = estimate_image_tokens(*size)
		text = (
			f'🔍 zoomed on ({x:.0f}, {y:.0f}) {width:.0f}x{height:.0f} px of the viewport at {scale:.1f}x '
			f'(redrawn, not upscaled); ~{tokens} tokens'
		)
		return Percept([], text, jpeg, size, tokens, len(text) // 4)

	async def find(self, text: str, limit: int = 5) -> Percept:
		"""Where visible text is on the page: each match's centre in viewport CSS px (to click), whether it is in view
		or how far to scroll, and a zoomed crop around the first match in view."""
		assert text.strip(), 'say what to find'
		if not self.retina.running:
			await self.open()
		found = await self.retina.evaluate(f'({_FIND_JS})({json.dumps(text.strip())}, {int(limit) * 4})') or {}
		matches, vw, vh = found.get('matches') or [], found.get('vw') or 1, found.get('vh') or 1
		if not matches:
			return Percept(
				[],
				f'🔎 "{text}": not found in the visible text of the page (text inside images, canvas, iframes and closed shadow roots is not searched)',
				None,
			)
		lines = [
			f'🔎 "{text}": {len(matches)} match(es) in the page text' + (f', the first {limit}' if len(matches) > limit else '')
		]
		in_view = None
		for i, m in enumerate(matches[:limit], 1):
			cx, cy = m['x'] + m['w'] / 2, m['y'] + m['h'] / 2
			if cy < 0:
				where = f'above the visible area: scroll up about {(-cy) / vh:.1f} screens'
			elif cy > vh:
				where = f'below the visible area: scroll down about {(cy - vh) / vh + 0.5:.1f} screens'
			elif cx < 0 or cx > vw:
				where = 'beside the visible area: scroll sideways'
			else:
				where = 'in view'
				in_view = in_view or m
			context = str(m.get('context', ''))
			lines.append(
				f'  {i}. at ({cx:.0f}, {cy:.0f}), {m["w"]:.0f}x{m["h"]:.0f} px, {where}: "{context}"' + page_text_note(context)
			)
		if in_view is None:
			return Percept([], '\n'.join(lines), None)
		# A crop around the first match in view, with room for its surroundings, magnified like zoom().
		w, h = max(240.0, in_view['w'] + 160), max(80.0, in_view['h'] + 60)
		x0 = min(max(0.0, in_view['x'] + in_view['w'] / 2 - w / 2), max(0.0, vw - w))
		y0 = min(max(0.0, in_view['y'] + in_view['h'] / 2 - h / 2), max(0.0, vh - h))
		crop = await self.zoom(x0, y0, w, h, max_width=720)
		crop.text = '\n'.join(lines) + '\n' + crop.text
		return crop

	async def look(self, detail: Detail = 'look', seconds: float = 2.0) -> Percept:
		"""What is on screen now. A playing video is watched briefly; otherwise the page itself is
		shown as the compositor draws it (canvas and WebGL included), in one frame."""
		if not self.retina.running:
			await self.open()
		await self.retina.wait_for_data(0.6)
		# A video is watched briefly; a canvas is shown with the page around it (watch follows a canvas over time).
		if self.retina.attended.get('vid') and self.retina.attended.get('kind') != 'canvas':
			return await self.watch(seconds=seconds, until='time', min_seconds=seconds, detail=detail, keyframes=2)
		jpeg = await self._page_watcher().wait_latest()
		url = self.retina.state.get('url', '')
		text = f'👁 no video playing on {url[:120]}; this is the page as drawn now (a compositor frame, not a screenshot call)'
		now = time.monotonic()
		text += _text_lines(self.retina.events, self._text_since(now), self.retina.page_since)
		self._last_percept_end = now
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
		if self.archive is None or time.monotonic() < self._archive_quiet_until:
			return 0
		pending = [f for f in self.retina.frames if f.has_keyframe and not self.archive.has(f.vid, f.seq)][-limit:]
		if not pending:
			return 0
		jpegs, trouble = await self.retina.read_keyframes([f.seq for f in pending])
		if trouble:
			# A hung page is not asked again every tick, each time holding another read open in it.
			self._archive_quiet_until = time.monotonic() + ARCHIVE_BACKOFF_S
			logger.info(f'eyes: archiving paused for {ARCHIVE_BACKOFF_S:.0f}s: {trouble}')
			return 0
		stored = 0
		for f, jpeg in zip(pending, jpegs):
			if jpeg:
				self.archive.add(f, jpeg)
				stored += 1
		return stored

	async def _archive_loop(self, every_s: float = 2.0) -> None:
		"""Copy keyframes to disk between watches. Pulling JPEGs out of the page uses the same page
		thread the retina times frames and sound on, and archiving can wait while measuring cannot:
		during a watch it only steps in, a few frames at a time, when the ring is close to overflowing."""
		while True:
			await asyncio.sleep(every_s)
			try:
				if self._watching:
					backlog = sum(
						1 for f in self.retina.frames if f.has_keyframe and self.archive and not self.archive.has(f.vid, f.seq)
					)
					if backlog < ARCHIVE_BACKLOG_LIMIT:
						continue
					await self.archive_now(limit=8)
					continue
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

	async def _settle(self, timeout: float) -> None:
		"""Wait until the attended item has stayed the same for SETTLE_S (or `timeout` passes)."""
		deadline = time.monotonic() + timeout
		current, since = self.retina.attended.get('vid', 0), time.monotonic()
		while time.monotonic() < deadline:
			await self.retina.wait_for_data(0.1)
			vid = self.retina.attended.get('vid', 0)
			if vid != current:
				current, since = vid, time.monotonic()
			elif time.monotonic() - since >= SETTLE_S:
				return

	async def _correct_overshoot(self, before_order: int | None, direction: str) -> str:
		"""If the flick carried the feed past the next item, flick back as a person would, until it lands.

		Judged from the videos' document order, which is how a feed lays out its items. When the
		order is unknown (no previous position, or a feed that recycles elements) nothing is
		assumed and nothing is done. A flick back that does not take (a scroll-snap feed snaps a
		weak fling back where it was) is tried again harder, as a thumb would; at most a few tries.
		"""
		after_order = self.retina.attended.get('order')
		if not isinstance(before_order, int) or not isinstance(after_order, int) or before_order < 0 or after_order < 0:
			return ''
		forward = 1 if direction == 'down' else -1
		target = before_order + forward
		step = (after_order - before_order) * forward
		if step <= 1:
			return ''
		w, h = await self.touch.viewport()
		# Finger directions: 'back' scrolls toward earlier items in the direction of travel, 'on' further along.
		back, on = ('down', 'up') if direction == 'down' else ('up', 'down')
		fractions = iter((0.45, 0.72, 0.72))
		fraction = next(fractions)
		tries, reversed_ = 0, False
		for _ in range(3):
			await self._settle(NEXT_CONFIRM_S)  # a late-taking flick lands before the next decision
			order = self.retina.attended.get('order')
			if not isinstance(order, int) or order == target:
				break
			# Decide from where the feed is now: a flick back can overcorrect past the target too.
			past = (order - target) * forward > 0
			reversed_ = reversed_ or not past
			current = self.retina.attended.get('vid', 0)
			tries += 1
			await self.touch.flick(back if past else on, fraction=fraction, around=(w / 2, h * 0.5))
			if not await self._wait_for_item_change(current, NEXT_CONFIRM_S):
				fraction = next(fractions, 0.72)  # it snapped back: flick harder next time
		await self._settle(NEXT_CONFIRM_S)  # judged where the feed comes to rest, not where it is passing through
		landed = self.retina.attended.get('order') == target
		harder = ' (harder after a flick that did not take)' if tries > 1 and landed and not reversed_ else ''
		again = ' (and forward again after the flick back overcorrected)' if landed and reversed_ else ''
		if landed:
			return f'overshot by {step - 1} item(s); flicked back{harder}{again}'
		return f'overshot by {step - 1} item(s); {tries} flick(s) back did not reach the next item'

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

	async def _browse(
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
		watched: list[int] = []  # the item each step watched, in order (a reel glimpsed while overshooting is not one)
		for i in range(items):
			p = await self.watch(seconds=max_seconds, until='bored', min_seconds=min_seconds, detail=detail, keyframes=keyframes)
			if (vid := self.retina.attended.get('vid', 0)) and vid not in watched:
				watched.append(vid)  # what the watch ended on (before it, a just-opened page may attend nothing yet)
			log.append(f'item {i + 1}: {p.stop_reason}')
			if i + 1 < items:
				moved = await self.next()
				log.append(
					f'  -> next by {moved.method} in {moved.seconds:.1f}s' + (f' ({moved.note})' if moved.note else '')
					if moved.moved
					else f'  -> feed did not move ({moved.note}); stopping'
				)
				if not moved.moved:
					break
		if hold:
			await self._hold()
		percept = await self.perceive(since=start, detail=detail, keyframes=keyframes, order=watched)
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

	def _distinct_sounds(self, vid: int) -> str | None:
		"""'heard N distinct sounds (at ...)' for an item whose sound was sparse discrete events, else None.

		Beeps over silence never change the sound class, so the class-change entries alone left a question asked
		after playback with nothing to answer from.
		"""
		return _sounds_line([h for h in self.retina.hops if h.vid == vid], self.retina.state.get('sr'))

	def _note_sounds_soon(self, vid: int) -> None:
		"""Journal an item's distinct sounds from a thread: leaving an item happens mid-swipe, when the event loop
		is timing the feed and ~30 ms of analysis on it is unwelcome."""
		hops, sr = [h for h in self.retina.hops if h.vid == vid], self.retina.state.get('sr')

		def done(task: asyncio.Task) -> None:
			heard = None if task.cancelled() or task.exception() else task.result()
			if heard and self._sounds_noted.get(vid) != heard and self.journal_path is not None:
				self._sounds_noted[vid] = heard
				self._append_journal([('sound', heard)], vid, None)

		try:
			asyncio.get_running_loop().create_task(asyncio.to_thread(_sounds_line, hops, sr)).add_done_callback(done)
		except RuntimeError:  # no running loop (called outside asyncio): skip rather than block
			pass

	def note_sounds(self) -> None:
		"""Journal the current item's distinct sounds now if they changed since last noted.

		For a reader asking between pauses (retinat_changes): the pause that would have noted them can come late
		or not be seen at all, and the count should not depend on catching that transition.
		"""
		vid = self.retina.attended.get('vid') or self._journaled.get('vid')
		if not vid or self.journal_path is None:
			return
		heard = self._distinct_sounds(vid)
		if heard and self._sounds_noted.get(vid) != heard:
			self._sounds_noted[vid] = heard
			self._append_journal([('sound', heard)], vid, self.retina.state.get('t'))

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
				if f.get('paused') and (heard := self._distinct_sounds(vid)) and self._sounds_noted.get(vid) != heard:
					self._sounds_noted[vid] = heard
					entries.append(('sound', heard))
				entries.append(('state', 'paused' if f.get('paused') else 'playing again'))
		if vid != last.get('vid') and last.get('vid') and not last.get('paused'):
			self._note_sounds_soon(last['vid'])  # leaving an item mid-play: say what it sounded like
		self._journaled = {**last, **{k: f.get(k) for k in ('url', 'vid', 'sound', 'paused')}}
		texts, self._pending_text = self._pending_text, []
		entries += [('text', f'text appeared: "{t}') for t in texts]  # each already closes its quote and carries its note
		if entries:
			self._append_journal(entries, vid, t)

	def _append_journal(self, entries: list[tuple[str, str]], vid: int, t: Any) -> None:
		assert self.journal_path is not None
		at = time.time()
		lines = [json.dumps({'at': at, 'kind': k, 'vid': vid, 't': t, 'text': text}) for k, text in entries]
		self.journal_path.parent.mkdir(parents=True, exist_ok=True)
		with self.journal_path.open('a') as out:
			out.write('\n'.join(lines) + '\n')
		if self.journal_path.stat().st_size > JOURNAL_MAX_BYTES:  # keep the newest half
			kept = self.journal_path.read_text().splitlines()
			self.journal_path.write_text('\n'.join(kept[len(kept) // 2 :]) + '\n')

	def _update_now(self, frames: list[FrameSample], hops: list[AudioHop], events: list[RetinaEvent]) -> None:
		# Text events are kept from every batch: the journal writes about once a second and must not drop them.
		self._pending_text += [
			str(e.data['text'])[:160] + '"' + page_text_note(str(e.data['text']), bool(e.data.get('faint')))
			for e in events
			if e.type == 'text' and e.data.get('text')
		]
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


def deaf_spans(events: list[RetinaEvent], vid: int) -> list[tuple[float, float, str]]:
	"""(t0, t1, 'muted'/'ended') stretches of item `vid`, in media time, when its capture track delivered no sound.

	A muted or ended track gives the ear silence whatever is playing, so what was "heard" there is unknown,
	not quiet. Read from the retina's state heartbeat (about once a second), so edges are good to ~1 s.
	"""
	spans: list[tuple[float, float, str]] = []
	open_at: tuple[float, str] | None = None
	last_t: float | None = None
	for e in events:
		if e.type != 'state' or e.data.get('vid') != vid or e.data.get('t') is None:
			continue
		t, track = float(e.data['t']), e.data.get('track')
		bad = track if track in ('muted', 'ended') else None
		if open_at and open_at[1] != bad:
			spans.append((open_at[0], t, open_at[1]))
			open_at = None
		if bad and not open_at:
			open_at = (t, bad)
		last_t = t
	if open_at and last_t is not None:
		spans.append((open_at[0], max(open_at[0], last_t), open_at[1]))
	return spans


def _sounds_line(hops: list[AudioHop], sr: Any) -> str | None:
	"""'heard N distinct sounds (at ...)' when the sound was sparse discrete events (beeps, knocks), else None."""
	if not hops:
		return None
	h = hearing.listen(hops, sr)
	if not h.heard or not 0 < len(h.onsets) <= 16:
		return None
	heard_s = sum(seg.duration for seg in h.segments if seg.kind != 'silence')
	voiced_s = sum(seg.duration for seg in h.segments if seg.kind in ('speech', 'music'))
	# Mostly talk or music: onsets there are syllables and notes, not events. Real talk or music lasts seconds; a
	# sub-second "speech" blip among beeps (the heuristic misreads a short tone) must not veto the count.
	if voiced_s >= VOICED_VETO_S and voiced_s > heard_s / 2:
		return None
	return f'heard {len(h.onsets)} distinct sounds (at ' + ', '.join(sight.fmt_t(t) for t in h.onsets) + ')'


# Visible text matches across the composed tree: open shadow roots are entered and slots followed to what they show,
# so slotted text counts once. The text is flattened with whitespace collapsed (inline neighbours join, as
# "Check<b>out</b>"; a block boundary is a space), every character remembering its node and offset. A match is measured
# node by node and the rectangles merged: one Range cannot cross a shadow boundary.
_FIND_JS = """(q, cap) => {
	const needle = q.replace(/\\s+/g, ' ').trim().toLowerCase();
	const nodes = [];
	const walk = (n) => {
		if (n.nodeType === 3) return void nodes.push(n);
		if (n.nodeType === 1) {
			if (/^(SCRIPT|STYLE|NOSCRIPT|TEMPLATE|HEAD)$/.test(n.tagName)) return;
			if (n.tagName === 'SLOT') {
				const shown = n.assignedNodes({ flatten: true });
				return void (shown.length ? shown : Array.from(n.childNodes)).forEach(walk);
			}
			if (n.shadowRoot) return void walk(n.shadowRoot);
		} else if (n.nodeType !== 11 && n.nodeType !== 9) return;
		for (const c of n.childNodes) walk(c);
	};
	walk(document.documentElement);
	// Between two text nodes there is a break when any element left or entered on the way (up to their common
	// ancestor) is not inline: "Check<b>out</b>" joins, "<p>a</p><p>b</p>" does not.
	const up = (n) => n.parentElement || (n.parentNode && n.parentNode.host) || null;
	const breaks = (a, b) => {
		const seen = new Set();
		for (let e = up(a); e; e = up(e)) seen.add(e);
		let common = null;
		const entered = [];
		for (let e = up(b); e; e = up(e)) {
			if (seen.has(e)) { common = e; break; }
			entered.push(e);
		}
		const left = [];
		for (let e = up(a); e && e !== common; e = up(e)) left.push(e);
		return [...left, ...entered].some((e) => !getComputedStyle(e).display.startsWith('inline'));
	};
	// A block break is a newline: a query (whitespace folded to spaces) never spans two blocks, and the context
	// shown around a match stops there, as the person reading the page would see it.
	let flat = '', shown = '';
	const from = [];
	let space = true;
	for (let k = 0; k < nodes.length; k++) {
		const n = nodes[k];
		if (k && flat && !flat.endsWith('\\n') && breaks(nodes[k - 1], n)) {
			if (space) { flat = flat.slice(0, -1); shown = shown.slice(0, -1); from.pop(); }
			flat += '\\n'; shown += '\\n'; from.push(null); space = true;
		}
		const d = n.data;
		for (let i = 0; i < d.length; i++) {
			const white = /\\s/.test(d[i]);
			if (white && space) continue;
			flat += white ? ' ' : d[i].toLowerCase();
			shown += white ? ' ' : d[i];
			from.push([k, i]);
			space = white;
		}
	}
	const out = [];
	for (let at = flat.indexOf(needle); at >= 0 && needle && out.length < cap; at = flat.indexOf(needle, at + 1)) {
		const spans = new Map();
		for (let c = at; c < at + needle.length; c++) {
			const f = from[c];
			if (!f) continue;
			const s = spans.get(f[0]);
			spans.set(f[0], s ? [s[0], f[1] + 1] : [f[1], f[1] + 1]);
		}
		let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
		for (const [k, [a, b]] of spans) {
			const el = nodes[k].parentElement;
			if (el) {
				const st = getComputedStyle(el);
				if (st.visibility === 'hidden' || +st.opacity === 0) continue;
			}
			const r = document.createRange();
			r.setStart(nodes[k], a);
			r.setEnd(nodes[k], b);
			const box = r.getBoundingClientRect();
			if (box.width < 1 || box.height < 1) continue;
			x0 = Math.min(x0, box.left); y0 = Math.min(y0, box.top);
			x1 = Math.max(x1, box.right); y1 = Math.max(y1, box.bottom);
		}
		if (x1 <= x0 || y1 <= y0) continue;
		let c0 = Math.max(0, at - 40), c1 = Math.min(shown.length, at + needle.length + 40);
		c0 = Math.max(c0, shown.lastIndexOf('\\n', at) + 1);
		const end = shown.indexOf('\\n', at + needle.length);
		if (end >= 0) c1 = Math.min(c1, end);
		const context = shown.slice(c0, c1).trim();
		out.push({ x: x0, y: y0, w: x1 - x0, h: y1 - y0, context });
	}
	return { matches: out, vw: innerWidth, vh: innerHeight };
}"""


def _text_lines(events, since: float, page_since: float, limit: int = 12, fresh_from: float | None = None) -> str:
	"""Text that appeared on the page (toasts, status lines, captions in the DOM), oldest first.

	Text from another document than the current one is the page just left: dropped, unless it arrived after
	`fresh_from` (a watch that itself saw the page navigate). The document comes from the retina's own tag,
	which does not lag a navigation as `page_since` can."""
	docs = [e.data['doc'] for e in events if e.type == 'state' and e.data.get('doc')]
	current = docs[-1] if docs else None
	seen: dict[str, tuple[float, bool]] = {}
	for e in events:
		doc = e.data.get('doc')
		if current and doc and doc != current and (fresh_from is None or e.wall < fresh_from):
			continue
		if e.type == 'text' and e.wall >= since and e.data.get('text'):
			seen.setdefault(' '.join(str(e.data['text']).split())[:240], (e.wall, bool(e.data.get('faint'))))
	if not seen:
		return ''
	lines = [
		f'    "{t}" ({w - page_since:.1f}s after the page loaded)' + page_text_note(t, faint)
		for t, (w, faint) in list(seen.items())[:limit]
	]
	more = f'\n    ... {len(seen) - limit} more' if len(seen) > limit else ''
	return '\n    text that appeared:\n' + '\n'.join(lines) + more


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
