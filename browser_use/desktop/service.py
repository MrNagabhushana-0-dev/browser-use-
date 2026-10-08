"""Computer use: the AI uses apps on the person's X desktop with the mouse and keyboard, by their leave.

What the person keeps in hand:
- **It is off** unless turned on: `DesktopControl(enabled=True)` or BROWSER_USE_DESKTOP_CONTROL=1.
- **Only apps the person granted**, each at a tier, after Anthropic's desktop computer use. Browsers can only be
  looked at: the AI uses them through the bridge or a browser tool, which see the page rather than its pixels.
  Terminals and IDEs can be clicked but not typed into, right-clicked or dragged onto. Everything else is
  `full`. The tier is checked against the app the action actually reaches: the window under the pointer for a
  click, the app with the keyboard focus for typing.
- **The person comes first.** XInput2 tells their mouse and keyboard apart from the AI's injected input by
  device, not by timing. While they have used either in the last few seconds, the AI waits.
- **Refusals happen before anything is sent,** so their effect is `none` (browser_use/mcp/effects.py).

Each action reports where it landed and whether the screen changed, so a missed click shows up at once.
Linux/X11 only. Wayland has no global input injection or input-source reporting for ordinary clients.
"""

import asyncio
import io
import logging
import os
import random
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from browser_use.desktop.x11 import X11, AppInfo, PersonWatch
from browser_use.human.motion import bezier_path, keystroke_delays, move_duration_ms
from browser_use.mcp.effects import Refused

logger = logging.getLogger(__name__)

OPT_IN_ENV = 'BROWSER_USE_DESKTOP_CONTROL'
APPS_ENV = 'BROWSER_USE_DESKTOP_APPS'  # e.g. "gedit,libreoffice:full,xterm:click"
SIGNATURE_SIZE = (256, 160)


class Tier(StrEnum):
	READ = 'read'  # visible only
	CLICK = 'click'  # plain left clicks and scrolling
	FULL = 'full'


_RANK = {Tier.READ: 0, Tier.CLICK: 1, Tier.FULL: 2}

BROWSERS = frozenset(
	{
		'chromium',
		'chromium-browser',
		'google-chrome',
		'brave-browser',
		'microsoft-edge',
		'firefox',
		'vivaldi-stable',
		'vivaldi',
		'opera',
		'epiphany',
		'falkon',
		'librewolf',
		'navigator',
	}
)
TERMINALS_AND_IDES = frozenset(
	{
		'xterm',
		'uxterm',
		'gnome-terminal-server',
		'gnome-terminal',
		'konsole',
		'alacritty',
		'kitty',
		'terminator',
		'tilix',
		'xfce4-terminal',
		'urxvt',
		'st-256color',
		'org.wezfurlong.wezterm',
		'code',
		'code-oss',
		'vscodium',
		'cursor',
		'sublime_text',
		'emacs',
		'gvim',
		'zed',
	}
)


def ceiling(app: str) -> Tier:
	"""The highest tier an app of its kind can be granted."""
	if app in BROWSERS:
		return Tier.READ
	if app in TERMINALS_AND_IDES or app.startswith('jetbrains-'):
		return Tier.CLICK
	return Tier.FULL


def parse_grants(spec: str) -> dict[str, Tier]:
	"""'gedit, libreoffice:full, xterm:click' -> {app: tier}, each capped at its kind's ceiling."""
	grants: dict[str, Tier] = {}
	for part in filter(None, (p.strip() for p in spec.split(','))):
		app, _, tier = part.partition(':')
		app = app.strip().lower()
		wanted = Tier(tier.strip().lower()) if tier.strip() else Tier.FULL
		cap = ceiling(app)
		grants[app] = wanted if _RANK[wanted] <= _RANK[cap] else cap
	return grants


class Landing(BaseModel):
	"""What one action reached and what it did to the screen."""

	model_config = ConfigDict(extra='forbid')

	app: str
	title: str = ''
	changed: bool
	note: str = ''

	def line(self, did: str) -> str:
		where = f' in {self.app}' + (f' ("{self.title[:60]}")' if self.title else '') if self.app else ''
		seen = 'the screen changed' if self.changed else 'nothing on screen changed'
		return f'{did}{where}; {seen}{" " + self.note if self.note else "."}'


class DesktopControl:
	"""Mouse and keyboard on an X display, within the person's grants and never over their own hands."""

	def __init__(
		self,
		display: str | None = None,
		enabled: bool | None = None,
		grants: dict[str, Tier] | None = None,
		resume_after_s: float = 8.0,
		seed: int | None = None,
	):
		allowed = enabled if enabled is not None else os.environ.get(OPT_IN_ENV, '').lower() in ('1', 'true', 'yes')
		if not allowed:
			raise Refused(
				f'Desktop control is off: it moves the mouse and types in apps on the whole desktop. Turn it on with '
				f'DesktopControl(enabled=True) or {OPT_IN_ENV}=1, only where the person has agreed to it.'
			)
		self.display = display or os.environ.get('DISPLAY') or ''
		assert self.display, 'no X display (set DISPLAY or pass display=)'
		self.grants = dict(grants) if grants is not None else parse_grants(os.environ.get(APPS_ENV, ''))
		for app, tier in self.grants.items():
			assert _RANK[tier] <= _RANK[ceiling(app)], f'{app} cannot be granted above {ceiling(app)}'
		self.resume_after_s = resume_after_s
		self.x = X11(self.display)
		self.person = PersonWatch(self.display)
		self.rng = random.Random(seed)
		self.scale = 1.0  # screen pixels per pixel of the last image the AI was shown

	def close(self) -> None:
		self.person.close()
		self.x.close()

	# -- seeing ----------------------------------------------------------------------------

	def _grab(self, box: tuple[int, int, int, int] | None = None):
		from PIL import ImageGrab

		return ImageGrab.grab(bbox=box, xdisplay=self.display).convert('RGB')

	async def screenshot(self, max_width: int = 1280) -> tuple[bytes, tuple[int, int]]:
		"""The screen as JPEG, at most `max_width` wide. Coordinates for actions are in this image's pixels."""
		from PIL import Image

		img = await asyncio.to_thread(self._grab)
		self.scale = max(1.0, img.width / max_width)
		if self.scale > 1.0:
			img = img.resize((round(img.width / self.scale), round(img.height / self.scale)), Image.Resampling.LANCZOS)
		out = io.BytesIO()
		img.save(out, format='JPEG', quality=80)
		return out.getvalue(), img.size

	async def zoom(self, x: float, y: float, width: float, height: float, out_width: int = 960) -> bytes:
		"""A region (in image pixels) at full screen resolution, enlarged to `out_width`: small print, icons."""
		from PIL import Image

		box = self._box(x, y, width, height)
		img = await asyncio.to_thread(self._grab, box)
		factor = out_width / max(1, img.width)
		if factor > 1:
			img = img.resize((out_width, round(img.height * factor)), Image.Resampling.LANCZOS)
		out = io.BytesIO()
		img.save(out, format='PNG')
		return out.getvalue()

	def _box(self, x: float, y: float, width: float, height: float) -> tuple[int, int, int, int]:
		assert width > 0 and height > 0, 'zoom needs a positive width and height'
		left, top = self._screen(x, y)
		right = min(self.x.width, round((x + width) * self.scale))
		bottom = min(self.x.height, round((y + height) * self.scale))
		assert right > left and bottom > top, 'the region is off the screen'
		return left, top, right, bottom

	def _screen(self, x: float, y: float) -> tuple[int, int]:
		sx, sy = round(x * self.scale), round(y * self.scale)
		if not (0 <= sx < self.x.width and 0 <= sy < self.x.height):
			raise Refused(f'({x}, {y}) is off the screen ({self.x.width / self.scale:.0f}x{self.x.height / self.scale:.0f})')
		return sx, sy

	def _signature(self) -> bytes:
		"""A 256x160 grey thumbnail: fine enough to see a few typed characters, coarse enough to be cheap."""
		return self._grab().convert('L').resize(SIGNATURE_SIZE).tobytes()

	def _changed_box(self, before: bytes, after: bytes) -> tuple[int, int, int, int] | None:
		"""Screen box around the cells that changed, or None if fewer than a caret's worth did."""
		w, h = SIGNATURE_SIZE
		cells = [i for i, (a, b) in enumerate(zip(before, after)) if abs(a - b) > 6]
		if len(cells) < 4:  # a blinking caret is about three cells
			return None
		xs, ys = [i % w for i in cells], [i // w for i in cells]
		fx, fy = self.x.width / w, self.x.height / h
		return round(min(xs) * fx), round(min(ys) * fy), round((max(xs) + 1) * fx), round((max(ys) + 1) * fy)

	# -- the person's grants and hands -----------------------------------------------------

	def status(self) -> str:
		focused = self.x.focused_app()
		quiet = self.person.quiet_for()
		hands = (
			'not seen using the mouse or keyboard'
			if quiet == float('inf')
			else f'last used the {self.person.last_kind} {quiet:.0f}s ago'
		)
		granted = ', '.join(f'{a} ({t})' for a, t in sorted(self.grants.items())) or 'none'
		return (
			f'Screen {self.x.width}x{self.x.height} on {self.display}. Focused: {focused.app if focused else "nothing"}. '
			f'Granted apps: {granted}. The person {hands}.'
		)

	def _check_hands(self) -> None:
		quiet = self.person.quiet_for()
		if quiet < self.resume_after_s:
			raise Refused(
				f'the person is using the computer (their {self.person.last_kind}, {quiet:.1f}s ago); the AI waits until '
				f'they have been idle for {self.resume_after_s:.0f}s'
			)

	def _check_app(self, app: AppInfo | None, needs: Tier, doing: str) -> AppInfo:
		if app is None or not app.app:
			raise Refused(f'{doing} would reach no application window; look again')
		tier = self.grants.get(app.app)
		if tier is None:
			hint = ' Browsers are used through the bridge or a browser tool instead.' if app.app in BROWSERS else ''
			raise Refused(
				f'{doing} would reach "{app.app}", which the person has not granted; ask them to add it to '
				f'{APPS_ENV} (with :click or :full).{hint}'
			)
		if _RANK[tier] < _RANK[needs]:
			why = {
				Tier.READ: 'it can only be looked at',
				Tier.CLICK: 'it may only be clicked, not typed into, right-clicked or dragged onto',
			}[tier]
			raise Refused(f'{doing} is not allowed in "{app.app}": it is granted at the {tier} tier, so {why}')
		return app

	# -- acting ----------------------------------------------------------------------------

	async def _glide(self, sx: int, sy: int) -> None:
		start = self.x.pointer()
		distance = ((sx - start[0]) ** 2 + (sy - start[1]) ** 2) ** 0.5
		path = bezier_path(start, (sx, sy), self.rng)
		step = move_duration_ms(distance, self.rng) / max(1, len(path)) / 1000
		for px, py in path:
			self.x.move(min(self.x.width - 1, max(0, round(px))), min(self.x.height - 1, max(0, round(py))))
			await asyncio.sleep(step)
		self.x.move(sx, sy)

	async def _landing(self, before: bytes, app: AppInfo | None, note: str = '') -> Landing:
		await asyncio.sleep(0.35)
		after = await asyncio.to_thread(self._signature)
		box = self._changed_box(before, after)
		if box is not None:
			s = self.scale
			where = f'around ({box[0] / s:.0f}, {box[1] / s:.0f})-({box[2] / s:.0f}, {box[3] / s:.0f})'
			note = f'{where}. {note}'.strip()
		return Landing(app=app.app if app else '', title=app.title if app else '', changed=box is not None, note=note)

	async def click(self, x: float, y: float, button: str = 'left', count: int = 1) -> str:
		assert button in ('left', 'middle', 'right'), button
		assert 1 <= count <= 3, 'count is 1, 2 or 3'
		sx, sy = self._screen(x, y)
		self._check_hands()
		needs = Tier.CLICK if button == 'left' else Tier.FULL
		app = self._check_app(self.x.app_at(sx, sy), needs, f'a {button} click at ({x:.0f}, {y:.0f})')
		before = await asyncio.to_thread(self._signature)
		await self._glide(sx, sy)
		code = {'left': 1, 'middle': 2, 'right': 3}[button]
		for _ in range(count):
			self.x.button(code, True)
			await asyncio.sleep(0.04 + self.rng.random() * 0.04)
			self.x.button(code, False)
			await asyncio.sleep(0.08)
		landed = await self._landing(before, app)
		times = {1: '', 2: ' twice', 3: ' three times'}[count]
		return landed.line(f'Clicked ({x:.0f}, {y:.0f}){times}')

	async def scroll(self, x: float, y: float, direction: str = 'down', amount: int = 3) -> str:
		assert direction in ('up', 'down', 'left', 'right'), direction
		assert 1 <= amount <= 30, 'amount is 1 to 30 notches'
		sx, sy = self._screen(x, y)
		self._check_hands()
		app = self._check_app(self.x.app_at(sx, sy), Tier.CLICK, f'scrolling at ({x:.0f}, {y:.0f})')
		before = await asyncio.to_thread(self._signature)
		await self._glide(sx, sy)
		code = {'up': 4, 'down': 5, 'left': 6, 'right': 7}[direction]
		for _ in range(amount):
			self.x.button(code, True)
			self.x.button(code, False)
			await asyncio.sleep(0.05)
		landed = await self._landing(before, app)
		return landed.line(f'Scrolled {direction} {amount} notches at ({x:.0f}, {y:.0f})')

	async def drag(self, x1: float, y1: float, x2: float, y2: float) -> str:
		start, end = self._screen(x1, y1), self._screen(x2, y2)
		self._check_hands()
		app = self._check_app(self.x.app_at(*start), Tier.FULL, f'dragging from ({x1:.0f}, {y1:.0f})')
		self._check_app(self.x.app_at(*end), Tier.FULL, f'dropping at ({x2:.0f}, {y2:.0f})')
		before = await asyncio.to_thread(self._signature)
		await self._glide(*start)
		self.x.button(1, True)
		try:
			await asyncio.sleep(0.1)
			await self._glide(*end)
			await asyncio.sleep(0.1)
		finally:
			self.x.button(1, False)  # never leave the button held, whatever happened on the way
		landed = await self._landing(before, app)
		return landed.line(f'Dragged ({x1:.0f}, {y1:.0f}) to ({x2:.0f}, {y2:.0f})')

	async def type_text(self, text: str, wpm: float = 300.0) -> str:
		assert text, 'nothing to type'
		self._check_hands()
		app = self._check_app(self.x.focused_app(), Tier.FULL, 'typing')
		before = await asyncio.to_thread(self._signature)
		for ch, delay in zip(text, keystroke_delays(text, self.rng, wpm=wpm)):
			self.x.type_char(ch)
			await asyncio.sleep(delay / 1000)
		landed = await self._landing(before, app)
		return landed.line(f'Typed {len(text)} characters')

	async def key(self, keys: str) -> str:
		"""A key or combination: 'Return', 'ctrl+s', 'alt+Tab', 'shift+F10'."""
		parts = [p for p in keys.replace(' ', '').split('+') if p]
		assert parts, 'no key given'
		*held, last = parts
		held_syms = [self.x.keysym(h) for h in held]
		last_sym = self.x.keysym(last)
		self._check_hands()
		app = self._check_app(self.x.focused_app(), Tier.FULL, f'pressing {keys}')
		before = await asyncio.to_thread(self._signature)
		self.x.press_keysyms(held_syms, last_sym)
		landed = await self._landing(before, app)
		return landed.line(f'Pressed {keys}')
