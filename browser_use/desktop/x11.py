"""The X11 calls desktop control needs, through ctypes (libX11, libXtst, libXi): no xdotool, no python-xlib.

- Input goes in through XTest, so applications receive ordinary events from the core XTEST devices.
- The person's own input is told apart by XInput2 raw events, which name the device each event came from: the
  core XTEST devices are ours; a mouse, a keyboard, or any other device is the person's. This is exact,
  rather than a guess from timing.
- The focused application is read from the input focus and its top-level window's WM_CLASS, which is what
  app tiers are checked against.

One `X11` holds one display connection and is not thread-safe; `PersonWatch` reads events on its own connection.
"""

import ctypes
import logging
import threading
import time
from ctypes import POINTER, Structure, byref, c_char_p, c_int, c_long, c_ubyte, c_uint, c_ulong, c_void_p

from pydantic import BaseModel, ConfigDict

logger = logging.getLogger(__name__)

_X = ctypes.CDLL('libX11.so.6')
_XTST = ctypes.CDLL('libXtst.so.6')
_XI = ctypes.CDLL('libXi.so.6')

Window = c_ulong
KeySym = c_ulong
XIAllMasterDevices = 1
XI_RawKeyPress, XI_RawButtonPress, XI_RawMotion = 13, 15, 17
GenericEvent = 35
AnyPropertyType = 0


class _XClassHint(Structure):
	_fields_ = [('res_name', c_void_p), ('res_class', c_void_p)]  # Xlib-allocated: read, then XFree


class _XIDeviceInfo(Structure):
	_fields_ = [
		('deviceid', c_int),
		('name', c_char_p),
		('use', c_int),
		('attachment', c_int),
		('enabled', c_int),
		('num_classes', c_int),
		('classes', c_void_p),
	]


class _XIEventMask(Structure):
	_fields_ = [('deviceid', c_int), ('mask_len', c_int), ('mask', POINTER(c_ubyte))]


class _XGenericEventCookie(Structure):
	_fields_ = [
		('type', c_int),
		('serial', c_ulong),
		('send_event', c_int),
		('display', c_void_p),
		('extension', c_int),
		('evtype', c_int),
		('cookie', c_uint),
		('data', c_void_p),
	]


class _XEvent(ctypes.Union):
	_fields_ = [('type', c_int), ('xcookie', _XGenericEventCookie), ('pad', c_long * 24)]


class _XIRawEvent(Structure):
	_fields_ = [
		('type', c_int),
		('serial', c_ulong),
		('send_event', c_int),
		('display', c_void_p),
		('extension', c_int),
		('evtype', c_int),
		('time', c_ulong),
		('deviceid', c_int),
		('sourceid', c_int),
		('detail', c_int),
		('flags', c_int),
	]


def _sig(lib, name: str, restype, *argtypes) -> None:
	fn = getattr(lib, name)
	fn.restype = restype
	fn.argtypes = list(argtypes)


_sig(_X, 'XOpenDisplay', c_void_p, c_char_p)
_sig(_X, 'XCloseDisplay', c_int, c_void_p)
_sig(_X, 'XFlush', c_int, c_void_p)
_sig(_X, 'XSync', c_int, c_void_p, c_int)
_sig(_X, 'XDefaultRootWindow', Window, c_void_p)
_sig(_X, 'XDefaultScreen', c_int, c_void_p)
_sig(_X, 'XDisplayWidth', c_int, c_void_p, c_int)
_sig(_X, 'XDisplayHeight', c_int, c_void_p, c_int)
_sig(_X, 'XStringToKeysym', KeySym, c_char_p)
_sig(_X, 'XKeysymToKeycode', c_ubyte, c_void_p, KeySym)
_sig(_X, 'XKeycodeToKeysym', KeySym, c_void_p, c_ubyte, c_int)
_sig(_X, 'XDisplayKeycodes', c_int, c_void_p, POINTER(c_int), POINTER(c_int))
_sig(_X, 'XGetKeyboardMapping', POINTER(KeySym), c_void_p, c_ubyte, c_int, POINTER(c_int))
_sig(_X, 'XChangeKeyboardMapping', c_int, c_void_p, c_int, c_int, POINTER(KeySym), c_int)
_sig(_X, 'XFree', c_int, c_void_p)
_sig(_X, 'XQueryPointer', c_int, c_void_p, Window, *(POINTER(Window),) * 2, *(POINTER(c_int),) * 4, POINTER(c_uint))
_sig(_X, 'XGetInputFocus', c_int, c_void_p, POINTER(Window), POINTER(c_int))
_sig(_X, 'XSetInputFocus', c_int, c_void_p, Window, c_int, c_ulong)
_sig(_X, 'XQueryTree', c_int, c_void_p, Window, POINTER(Window), POINTER(Window), POINTER(POINTER(Window)), POINTER(c_uint))
_sig(_X, 'XGetClassHint', c_int, c_void_p, Window, POINTER(_XClassHint))
_sig(_X, 'XFetchName', c_int, c_void_p, Window, POINTER(c_void_p))
_sig(_X, 'XInternAtom', c_ulong, c_void_p, c_char_p, c_int)
_sig(
	_X,
	'XGetWindowProperty',
	c_int,
	c_void_p,
	Window,
	c_ulong,
	c_long,
	c_long,
	c_int,
	c_ulong,
	POINTER(c_ulong),
	POINTER(c_int),
	POINTER(c_ulong),
	POINTER(c_ulong),
	POINTER(POINTER(c_ubyte)),
)
_sig(_X, 'XQueryExtension', c_int, c_void_p, c_char_p, POINTER(c_int), POINTER(c_int), POINTER(c_int))
_sig(_X, 'XNextEvent', c_int, c_void_p, POINTER(_XEvent))
_sig(_X, 'XPending', c_int, c_void_p)
_sig(_X, 'XGetEventData', c_int, c_void_p, POINTER(_XGenericEventCookie))
_sig(_X, 'XFreeEventData', None, c_void_p, POINTER(_XGenericEventCookie))
_sig(_X, 'XConnectionNumber', c_int, c_void_p)
_sig(_XTST, 'XTestFakeMotionEvent', c_int, c_void_p, c_int, c_int, c_int, c_ulong)
_sig(_XTST, 'XTestFakeButtonEvent', c_int, c_void_p, c_uint, c_int, c_ulong)
_sig(_XTST, 'XTestFakeKeyEvent', c_int, c_void_p, c_uint, c_int, c_ulong)
_sig(_XI, 'XIQueryVersion', c_int, c_void_p, POINTER(c_int), POINTER(c_int))
_sig(_XI, 'XIQueryDevice', POINTER(_XIDeviceInfo), c_void_p, c_int, POINTER(c_int))
_sig(_XI, 'XIFreeDeviceInfo', None, POINTER(_XIDeviceInfo))
_sig(_XI, 'XISelectEvents', c_int, c_void_p, Window, POINTER(_XIEventMask), c_int)

# Names people and models use for keys, to X keysym names.
KEY_NAMES = {
	'ctrl': 'Control_L',
	'control': 'Control_L',
	'alt': 'Alt_L',
	'option': 'Alt_L',
	'shift': 'Shift_L',
	'super': 'Super_L',
	'meta': 'Super_L',
	'cmd': 'Super_L',
	'win': 'Super_L',
	'enter': 'Return',
	'return': 'Return',
	'esc': 'Escape',
	'escape': 'Escape',
	'backspace': 'BackSpace',
	'delete': 'Delete',
	'del': 'Delete',
	'tab': 'Tab',
	'space': 'space',
	'up': 'Up',
	'down': 'Down',
	'left': 'Left',
	'right': 'Right',
	'pageup': 'Prior',
	'page_up': 'Prior',
	'pagedown': 'Next',
	'page_down': 'Next',
	'home': 'Home',
	'end': 'End',
	'insert': 'Insert',
	'menu': 'Menu',
	'printscreen': 'Print',
}
CHAR_KEYSYMS = {'\n': 0xFF0D, '\t': 0xFF09, '\b': 0xFF08}
SHIFT_L = 0xFFE1


class XError(RuntimeError):
	"""An X call failed or the display could not be opened."""


class AppInfo(BaseModel):
	"""The application owning a top-level window."""

	model_config = ConfigDict(extra='forbid')

	window: int
	app: str  # WM_CLASS class, lower-cased ('chromium', 'gnome-terminal-server', ...); '' when the window has none
	instance: str = ''  # WM_CLASS instance
	title: str = ''


def _take(ptr: int | None) -> str:
	"""Read a string Xlib allocated, then free it."""
	if not ptr:
		return ''
	try:
		return ctypes.string_at(ptr).decode(errors='replace')
	finally:
		_X.XFree(ptr)


def keysym_for_char(ch: str) -> int:
	"""The X keysym of one character: Latin-1 maps to itself, the rest to the Unicode keysym range."""
	assert len(ch) == 1, ch
	if ch in CHAR_KEYSYMS:
		return CHAR_KEYSYMS[ch]
	code = ord(ch)
	return code if 0x20 <= code <= 0x7E or 0xA0 <= code <= 0xFF else 0x01000000 + code


class X11:
	"""One connection to an X display, for input and for reading windows."""

	def __init__(self, display: str):
		self.display = display
		self._dpy = _X.XOpenDisplay(display.encode())
		if not self._dpy:
			raise XError(f'cannot open X display {display}')
		self.root = _X.XDefaultRootWindow(self._dpy)
		screen = _X.XDefaultScreen(self._dpy)
		self.width, self.height = _X.XDisplayWidth(self._dpy, screen), _X.XDisplayHeight(self._dpy, screen)
		self._lock = threading.Lock()

	def close(self) -> None:
		if self._dpy:
			_X.XCloseDisplay(self._dpy)
			self._dpy = None

	# -- pointer ---------------------------------------------------------------------------

	def pointer(self) -> tuple[int, int]:
		root, child = Window(), Window()
		rx, ry, wx, wy, mask = c_int(), c_int(), c_int(), c_int(), c_uint()
		with self._lock:
			_X.XQueryPointer(
				self._dpy, self.root, byref(root), byref(child), byref(rx), byref(ry), byref(wx), byref(wy), byref(mask)
			)
		return rx.value, ry.value

	def move(self, x: int, y: int) -> None:
		assert 0 <= x < self.width and 0 <= y < self.height, f'({x}, {y}) is off the {self.width}x{self.height} screen'
		with self._lock:
			_XTST.XTestFakeMotionEvent(self._dpy, -1, int(x), int(y), 0)
			_X.XFlush(self._dpy)

	def button(self, button: int, down: bool) -> None:
		assert button in (1, 2, 3, 4, 5, 6, 7), button
		with self._lock:
			_XTST.XTestFakeButtonEvent(self._dpy, button, 1 if down else 0, 0)
			_X.XFlush(self._dpy)

	def window_at(self, x: int, y: int) -> int:
		"""The top-level window under (x, y), 0 for the bare root."""
		with self._lock:
			return self._toplevel_at(x, y)

	def _toplevel_at(self, x: int, y: int) -> int:
		parent, root = Window(), Window()
		children = POINTER(Window)()
		n = c_uint()
		if not _X.XQueryTree(self._dpy, self.root, byref(root), byref(parent), byref(children), byref(n)):
			return 0
		try:
			stack = [children[i] for i in range(n.value)]  # bottom to top
		finally:
			if children:
				_X.XFree(children)
		for win in reversed(stack):
			geo = self._geometry(win)
			if geo and geo[4] and geo[0] <= x < geo[0] + geo[2] and geo[1] <= y < geo[1] + geo[3]:
				return win
		return 0

	def toplevels(self) -> list[tuple[tuple[int, int, int, int], AppInfo | None, bool]]:
		"""Viewable top-level windows, bottom to top: (box in root coordinates, app, override-redirect)."""
		with self._lock:
			parent, root = Window(), Window()
			children = POINTER(Window)()
			n = c_uint()
			if not _X.XQueryTree(self._dpy, self.root, byref(root), byref(parent), byref(children), byref(n)):
				return []
			try:
				stack = [children[i] for i in range(n.value)]
			finally:
				if children:
					_X.XFree(children)
			out = []
			for win in stack:
				attrs = _XWindowAttributes()
				if not _X.XGetWindowAttributes(self._dpy, win, byref(attrs)) or attrs.map_state != 2:
					continue
				box = (attrs.x, attrs.y, attrs.x + attrs.width, attrs.y + attrs.height)
				out.append((box, self._class_of(win), bool(attrs.override_redirect)))
			return out

	def buttons_down(self) -> int:
		"""The pointer's button mask (Button1Mask is 1 << 8): nonzero means a button is still held."""
		root, child = Window(), Window()
		rx, ry, wx, wy, mask = c_int(), c_int(), c_int(), c_int(), c_uint()
		with self._lock:
			_X.XQueryPointer(
				self._dpy, self.root, byref(root), byref(child), byref(rx), byref(ry), byref(wx), byref(wy), byref(mask)
			)
		return mask.value & (0x1F << 8)

	def _geometry(self, win: int) -> tuple[int, int, int, int, bool] | None:
		"""(x, y, width, height, mapped) of a window in root coordinates."""
		attrs = _XWindowAttributes()
		if not _X.XGetWindowAttributes(self._dpy, win, byref(attrs)):
			return None
		return attrs.x, attrs.y, attrs.width, attrs.height, attrs.map_state == 2  # IsViewable

	# -- keyboard --------------------------------------------------------------------------

	def _keycode(self, keysym: int) -> int:
		return int(_X.XKeysymToKeycode(self._dpy, keysym))

	def key(self, keycode: int, down: bool) -> None:
		with self._lock:
			_XTST.XTestFakeKeyEvent(self._dpy, keycode, 1 if down else 0, 0)
			_X.XFlush(self._dpy)

	def keysym(self, name: str) -> int:
		"""The keysym for a key name: 'ctrl', 'Return', 'F5', 'a', 'A', '5'."""
		lookup = KEY_NAMES.get(name.lower(), name)
		if len(lookup) == 1:
			return keysym_for_char(lookup)
		sym = int(_X.XStringToKeysym(lookup.encode()))
		if not sym and lookup[:1] in 'fF' and lookup[1:].isdigit():
			sym = int(_X.XStringToKeysym(f'F{lookup[1:]}'.encode()))
		if not sym:
			raise ValueError(f'no key called {name!r}')
		return sym

	def press_keysyms(self, held: list[int], key: int) -> None:
		"""Hold `held` (modifiers), press and release `key`, release `held` in reverse, whatever happens on the way."""
		codes: list[tuple[int, bool]] = []
		try:
			for sym in held:
				codes.append(self._ensure_keycode(sym))
			with self._lock:
				down: list[int] = []
				try:
					for code, _ in codes:
						_XTST.XTestFakeKeyEvent(self._dpy, code, 1, 0)
						down.append(code)
					self._tap_locked(key)
				finally:
					for code in reversed(down):
						_XTST.XTestFakeKeyEvent(self._dpy, code, 0, 0)
					_X.XFlush(self._dpy)
		finally:
			for code, temporary in codes:
				if temporary:
					self._unmap(code)

	def type_char(self, ch: str) -> None:
		with self._lock:
			self._tap_locked(keysym_for_char(ch))
			_X.XFlush(self._dpy)

	def _tap_locked(self, keysym: int) -> None:
		code = self._keycode(keysym)
		plain = int(_X.XKeycodeToKeysym(self._dpy, code, 0)) if code else 0
		shifted = int(_X.XKeycodeToKeysym(self._dpy, code, 1)) if code else 0
		temporary = False
		if keysym not in (plain, shifted):  # not on the keymap, or only behind AltGr or another level: remap
			code = self._remap_locked(keysym)
			temporary = True
		shift = not temporary and plain != keysym
		shift_code = self._keycode(SHIFT_L)
		if shift:
			_XTST.XTestFakeKeyEvent(self._dpy, shift_code, 1, 0)
		_XTST.XTestFakeKeyEvent(self._dpy, code, 1, 0)
		_XTST.XTestFakeKeyEvent(self._dpy, code, 0, 0)
		if shift:
			_XTST.XTestFakeKeyEvent(self._dpy, shift_code, 0, 0)
		if temporary:
			_X.XSync(self._dpy, 0)
			time.sleep(0.02)  # let clients read the key before the mapping goes away
			self._unmap_locked(code)

	def _ensure_keycode(self, keysym: int) -> tuple[int, bool]:
		code = self._keycode(keysym)
		if code:
			return code, False
		with self._lock:
			return self._remap_locked(keysym), True

	def _spare_keycode_locked(self) -> int:
		low, high = c_int(), c_int()
		_X.XDisplayKeycodes(self._dpy, byref(low), byref(high))
		per = c_int()
		count = high.value - low.value + 1
		syms = _X.XGetKeyboardMapping(self._dpy, low.value, count, byref(per))
		try:
			for i in range(count - 1, -1, -1):  # from the top: high keycodes are the ones keyboards leave empty
				if all(syms[i * per.value + j] == 0 for j in range(per.value)):
					return low.value + i
		finally:
			_X.XFree(syms)
		raise XError('no free keycode to type a character the keyboard map lacks')

	def _remap_locked(self, keysym: int) -> int:
		"""Map a spare keycode to `keysym` for one keystroke, as xdotool does for characters not on the keyboard."""
		code = self._spare_keycode_locked()
		pair = (KeySym * 2)(keysym, keysym)
		_X.XChangeKeyboardMapping(self._dpy, code, 2, pair, 1)
		_X.XSync(self._dpy, 0)
		time.sleep(0.02)  # clients refresh their keymap on MappingNotify
		return code

	def _unmap_locked(self, code: int) -> None:
		empty = (KeySym * 2)(0, 0)
		_X.XChangeKeyboardMapping(self._dpy, code, 2, empty, 1)
		_X.XSync(self._dpy, 0)

	def _unmap(self, code: int) -> None:
		with self._lock:
			self._unmap_locked(code)

	# -- windows ---------------------------------------------------------------------------

	def _toplevel(self, win: int) -> int:
		"""Climb from any window to its child of the root (the frame or the app window)."""
		while win and win != self.root:
			root, parent = Window(), Window()
			children = POINTER(Window)()
			n = c_uint()
			if not _X.XQueryTree(self._dpy, win, byref(root), byref(parent), byref(children), byref(n)):
				return 0
			if children:
				_X.XFree(children)
			if parent.value == self.root:
				return win
			win = parent.value
		return 0

	def _class_of(self, win: int) -> AppInfo | None:
		"""WM_CLASS and title of `win` or the first descendant that has one (window managers add frames)."""
		queue = [win]
		while queue:
			w = queue.pop(0)
			hint = _XClassHint()
			if _X.XGetClassHint(self._dpy, w, byref(hint)):
				app, instance = _take(hint.res_class).lower(), _take(hint.res_name)
				name = c_void_p()
				title = _take(name.value) if _X.XFetchName(self._dpy, w, byref(name)) else ''
				return AppInfo(window=w, app=app, instance=instance, title=title)
			root, parent = Window(), Window()
			children = POINTER(Window)()
			n = c_uint()
			if _X.XQueryTree(self._dpy, w, byref(root), byref(parent), byref(children), byref(n)) and children:
				queue.extend(children[i] for i in range(n.value))
				_X.XFree(children)
		return None

	def focused_app(self) -> AppInfo | None:
		"""The application typing would reach, or None if keys go nowhere. Under PointerRoot focus (no window manager,
		no app holding the focus) keys go to the window under the pointer, so that is the one reported."""
		with self._lock:
			focus, revert = Window(), c_int()
			_X.XGetInputFocus(self._dpy, byref(focus), byref(revert))
			if focus.value == 0:  # None: keystrokes are discarded
				return None
			if focus.value == 1:  # PointerRoot
				root, child = Window(), Window()
				rx, ry, wx, wy, mask = c_int(), c_int(), c_int(), c_int(), c_uint()
				_X.XQueryPointer(
					self._dpy, self.root, byref(root), byref(child), byref(rx), byref(ry), byref(wx), byref(wy), byref(mask)
				)
				top = self._toplevel_at(rx.value, ry.value)
			else:
				top = self._toplevel(focus.value)
			return self._class_of(top) if top else None

	def has_window_manager(self) -> bool:
		"""Whether an EWMH window manager runs (it then decides who gets the focus, not us)."""
		with self._lock:
			atom = _X.XInternAtom(self._dpy, b'_NET_SUPPORTING_WM_CHECK', 1)
			if not atom:
				return False
			kind, fmt, n, rest = c_ulong(), c_int(), c_ulong(), c_ulong()
			data = POINTER(c_ubyte)()
			ok = _X.XGetWindowProperty(
				self._dpy, self.root, atom, 0, 1, 0, AnyPropertyType, byref(kind), byref(fmt), byref(n), byref(rest), byref(data)
			)
			if data:
				_X.XFree(data)
			return ok == 0 and n.value > 0

	def give_focus(self, window: int) -> None:
		"""Give `window` the keyboard focus, as a click-to-focus window manager does."""
		with self._lock:
			_X.XSetInputFocus(self._dpy, window, 2, 0)  # RevertToParent, CurrentTime
			_X.XFlush(self._dpy)

	def app_at(self, x: int, y: int) -> AppInfo | None:
		"""The application whose window is on top at (x, y) (what a click there would reach), or None."""
		with self._lock:
			top = self._toplevel_at(x, y)
			return self._class_of(top) if top else None


class _XWindowAttributes(Structure):
	_fields_ = [
		('x', c_int),
		('y', c_int),
		('width', c_int),
		('height', c_int),
		('border_width', c_int),
		('depth', c_int),
		('visual', c_void_p),
		('root', Window),
		('class_', c_int),
		('bit_gravity', c_int),
		('win_gravity', c_int),
		('backing_store', c_int),
		('backing_planes', c_ulong),
		('backing_pixel', c_ulong),
		('save_under', c_int),
		('colormap', c_ulong),
		('map_installed', c_int),
		('map_state', c_int),
		('all_event_masks', c_long),
		('your_event_mask', c_long),
		('do_not_propagate_mask', c_long),
		('override_redirect', c_int),
		('screen', c_void_p),
	]


_sig(_X, 'XGetWindowAttributes', c_int, c_void_p, Window, POINTER(_XWindowAttributes))


class PersonWatch:
	"""Notices the person's own mouse and keyboard while the AI also sends input.

	XInput2 raw events carry the source device. Our input comes from the core XTEST devices; anything else, a real
	mouse or keyboard (or another master's devices), is the person. Runs a reader thread on its own connection.
	"""

	def __init__(self, display: str):
		self.display = display
		self.last_input = 0.0  # time.monotonic() of the person's last input, 0 if none seen
		self.last_kind = ''
		self.escape_at = 0.0  # when the person last pressed Escape: their stop key
		self._dpy = _X.XOpenDisplay(display.encode())
		if not self._dpy:
			raise XError(f'cannot open X display {display}')
		opcode, event, error = c_int(), c_int(), c_int()
		if not _X.XQueryExtension(self._dpy, b'XInputExtension', byref(opcode), byref(event), byref(error)):
			raise XError('the X server has no XInput extension, so the person cannot be told apart')
		self._opcode = opcode.value
		major, minor = c_int(2), c_int(2)
		if _XI.XIQueryVersion(self._dpy, byref(major), byref(minor)) != 0 or (major.value, minor.value) < (2, 2):
			raise XError('the X server lacks XInput 2.2, so the person cannot be told apart')
		self.ours = self._xtest_devices()
		self._escape = int(_X.XKeysymToKeycode(self._dpy, 0xFF1B))
		mask = (c_ubyte * 4)()
		for ev in (XI_RawKeyPress, XI_RawButtonPress, XI_RawMotion):
			mask[ev >> 3] |= 1 << (ev & 7)
		selection = _XIEventMask(XIAllMasterDevices, 4, mask)
		_XI.XISelectEvents(self._dpy, _X.XDefaultRootWindow(self._dpy), byref(selection), 1)
		_X.XSync(self._dpy, 0)
		self._stop = threading.Event()
		self._thread = threading.Thread(target=self._run, name='person-watch', daemon=True)
		self._thread.start()

	def _xtest_devices(self) -> set[int]:
		n = c_int()
		devices = _XI.XIQueryDevice(self._dpy, 0, byref(n))  # XIAllDevices
		try:
			return {
				devices[i].deviceid
				for i in range(n.value)
				if (devices[i].name or b'').decode(errors='replace').startswith('Virtual core XTEST')
			}
		finally:
			_XI.XIFreeDeviceInfo(devices)

	def _run(self) -> None:
		import select

		fd = _X.XConnectionNumber(self._dpy)
		event = _XEvent()
		names = {XI_RawKeyPress: 'key', XI_RawButtonPress: 'button', XI_RawMotion: 'pointer'}
		while not self._stop.is_set():
			if not _X.XPending(self._dpy):
				select.select([fd], [], [], 0.2)
				continue
			_X.XNextEvent(self._dpy, byref(event))
			if event.type != GenericEvent or event.xcookie.extension != self._opcode:
				continue
			if not _X.XGetEventData(self._dpy, byref(event.xcookie)):
				continue
			try:
				raw = ctypes.cast(event.xcookie.data, POINTER(_XIRawEvent)).contents
				if raw.sourceid not in self.ours:
					self.last_input = time.monotonic()
					self.last_kind = names.get(raw.evtype, 'input')
					if raw.evtype == XI_RawKeyPress and raw.detail == self._escape:
						self.escape_at = self.last_input
			finally:
				_X.XFreeEventData(self._dpy, byref(event.xcookie))

	def quiet_for(self) -> float:
		"""Seconds since the person's last input (infinity if never)."""
		return time.monotonic() - self.last_input if self.last_input else float('inf')

	def close(self) -> None:
		self._stop.set()
		self._thread.join(timeout=2)
		if self._dpy:
			_X.XCloseDisplay(self._dpy)
			self._dpy = None
