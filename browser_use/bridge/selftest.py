"""The install self-test: look, click, look, through relay -> extension -> a tab, judged on pixels alone.

After the extension is installed or updated (once per version), its worker opens one tab at the relay's own page
http://127.0.0.1:<port>/selftest and shares just that tab. The relay then looks (Page.captureScreenshot), clicks the
grey button's centre (Input.dispatchMouseEvent mousePressed + mouseReleased) and looks again. PASS when the button's
pixels turned green. The page turns it green only for a trusted click, so a PASS also means the AI's input reaches
pages the way the person's own does. The page reads the outcome from /selftest/state, which holds nothing else.
"""

import asyncio
import base64
import io
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from PIL import Image, ImageStat
from pydantic import BaseModel, ConfigDict

PATH = '/selftest'
STATE_PATH = '/selftest/state'
# The button and the patch of it that is measured, in CSS px from the viewport's top-left. Fixed to the top-left, so the
# "started debugging" bar appearing (it shortens the viewport) moves neither. The label sits below the patch.
BUTTON = (40, 150, 360, 160)  # left, top, width, height
PATCH = (76, 164, 288, 56)
GREY = '#9e9e9e'  # (158, 158, 158)
GREEN = '#2e7d32'  # (46, 125, 50)
LOOK1_WAIT_S = 8.0  # the page may still be loading when its tab is shared
LOOK2_WAIT_S = 3.0  # the click's repaint
SHOT = {'format': 'jpeg', 'quality': 85}  # as Retinat's own looks take them

Send = Callable[..., Awaitable[dict[str, Any]]]


class SelfTest(BaseModel):
	"""One run of the self-test, as the page, `setup` and `doctor` see it. Times are measured round trips in ms."""

	model_config = ConfigDict(extra='forbid')

	state: Literal['waiting', 'running', 'pass', 'fail'] = 'waiting'
	run: int = 0
	extension: str = ''  # the extension version that ran it
	attach_ms: float | None = None  # the first call: Page.getLayoutMetrics, which attaches the debugger
	look1_ms: float | None = None
	click_ms: float | None = None  # mousePressed + mouseReleased
	look2_ms: float | None = None
	green_after_ms: float | None = None  # from the end of the click to the look that saw green
	looks: int = 0  # screenshots taken (looks repeat while the page loads or repaints)
	shot: tuple[int, int] | None = None  # screenshot size in px
	scale: float | None = None  # screenshot px per CSS px
	click_at: tuple[float, float] | None = None  # CSS px
	before: tuple[int, int, int] | None = None  # mean RGB of the button patch, look 1
	after: tuple[int, int, int] | None = None  # ... look 2
	reason: str = ''


def is_grey(rgb: tuple[int, int, int]) -> bool:
	return max(rgb) - min(rgb) < 24 and 110 < sum(rgb) / 3 < 210


def is_green(rgb: tuple[int, int, int]) -> bool:
	r, g, b = rgb
	return g >= r + 40 and g >= b + 40


async def measure(send: Send, t: SelfTest, click_offset: float = 0.0) -> SelfTest:
	"""Run the self-test through `send(method, **params)` (a CDP call on the shared self-test tab), filling `t` as it
	goes so a failure part-way keeps what was measured. `click_offset` moves the click right by that many CSS px: a
	click off the button must FAIL."""
	assert t.state == 'running', t.state
	loop = asyncio.get_running_loop()
	started = loop.time()
	metrics = await send('Page.getLayoutMetrics')
	t.attach_ms = _ms(loop, started)
	css_width = (metrics.get('cssVisualViewport') or metrics.get('visualViewport') or {}).get('clientWidth') or 0
	if css_width <= 0:
		return _fail(t, f'Page.getLayoutMetrics gave no viewport width: {sorted(metrics)}')

	deadline = loop.time() + LOOK1_WAIT_S
	while True:
		image, t.look1_ms = await _look(send, loop)
		t.looks += 1
		t.shot, t.scale = image.size, round(image.width / css_width, 3)
		t.before = _patch(image, t.scale)
		if is_grey(t.before):
			break
		if loop.time() > deadline:
			return _fail(t, f'look 1 never showed the grey button (patch rgb {t.before}); is the page zoomed or still loading?')
		await asyncio.sleep(0.2)

	x, y = BUTTON[0] + BUTTON[2] / 2 + click_offset, BUTTON[1] + BUTTON[3] / 2
	t.click_at = (x, y)
	started = loop.time()
	for kind, buttons in (('mousePressed', 1), ('mouseReleased', 0)):
		await send('Input.dispatchMouseEvent', type=kind, x=x, y=y, button='left', buttons=buttons, clickCount=1)
	t.click_ms = _ms(loop, started)

	clicked = loop.time()
	while True:
		image, t.look2_ms = await _look(send, loop)
		t.looks += 1
		t.after = _patch(image, image.width / css_width)
		if is_green(t.after):
			t.green_after_ms = _ms(loop, clicked)
			t.state = 'pass'
			return t
		if loop.time() - clicked > LOOK2_WAIT_S:
			return _fail(t, f'the button did not turn green within {LOOK2_WAIT_S:.0f} s of the click (patch rgb {t.after})')
		await asyncio.sleep(0.1)


async def _look(send: Send, loop: asyncio.AbstractEventLoop) -> tuple[Image.Image, float]:
	started = loop.time()
	shot = await send('Page.captureScreenshot', **SHOT)
	took = _ms(loop, started)
	return Image.open(io.BytesIO(base64.b64decode(shot['data']))).convert('RGB'), took


def _patch(image: Image.Image, scale: float) -> tuple[int, int, int]:
	left, top, width, height = (round(v * scale) for v in PATCH)
	box = (left, top, min(left + width, image.width), min(top + height, image.height))
	if box[2] <= box[0] or box[3] <= box[1]:
		return (0, 0, 0)  # the viewport is smaller than the page's layout: no patch to read
	r, g, b = ImageStat.Stat(image.crop(box)).mean[:3]
	return round(r), round(g), round(b)


def _fail(t: SelfTest, reason: str) -> SelfTest:
	t.state = 'fail'
	t.reason = reason
	return t


def _ms(loop: asyncio.AbstractEventLoop, since: float) -> float:
	return round((loop.time() - since) * 1000, 1)


PAGE = (
	"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="color-scheme" content="light">
<title>Retinat bridge self-test</title>
<style>
	html, body { margin: 0; height: 100%; overflow: hidden; background: #fff; color: #1f2328;
		font: 16px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; }
	h1 { position: fixed; left: 40px; top: 26px; margin: 0; font-size: 22px; }
	#lead { position: fixed; left: 40px; top: 64px; margin: 0; max-width: 820px; color: #57606a; }
	#b { position: fixed; left: BUTTON_LEFTpx; top: BUTTON_TOPpx; width: BUTTON_WIDTHpx; height: BUTTON_HEIGHTpx; box-sizing: border-box;
		border: 0; border-radius: 12px; background: GREY; color: #fff; cursor: pointer; padding: 0 16px 18px;
		display: flex; align-items: flex-end; justify-content: center; font: 600 17px/1.3 system-ui, sans-serif; }
	#b.on { background: GREEN; }
	#out { position: fixed; left: 40px; top: 340px; max-width: 820px; }
	#verdict { font-size: 20px; font-weight: 650; margin: 0 0 6px; }
	#verdict.pass { color: GREEN; } #verdict.fail { color: #b3261e; }
	#numbers { color: #57606a; margin: 0 0 14px; font-variant-numeric: tabular-nums; }
	#next { margin: 0; font-weight: 600; }
</style>
</head>
<body>
<h1>Retinat bridge self-test</h1>
<p id="lead">The AI side looks at this tab, clicks the grey button, and looks again. Nothing happens in any other tab.</p>
<button id="b" type="button"><span>Waiting for the AI's click</span></button>
<div id="out"><p id="verdict">Waiting for the AI side to run the self-test&hellip;</p><p id="numbers"></p><p id="next"></p></div>
<script>
'use strict';
const b = document.getElementById('b');
const verdict = document.getElementById('verdict');
const numbers = document.getElementById('numbers');
const next = document.getElementById('next');
b.addEventListener('click', (e) => {
	if (!e.isTrusted) return void (numbers.textContent = 'A script tried to click the button; only a trusted click counts.');
	b.classList.add('on');
	b.firstElementChild.textContent = 'Clicked: a trusted click reached this page';
});
let seen = null;
const ms = (v) => (v == null ? '?' : Math.round(v) + ' ms');
function show(s) {
	seen = s;
	verdict.className = s.state === 'pass' || s.state === 'fail' ? s.state : '';
	numbers.textContent = '';
	next.textContent = '';
	if (s.state === 'waiting') verdict.textContent = 'Waiting for the AI side to run the self-test\\u2026';
	else if (s.state === 'running') verdict.textContent = 'Self-test running: look, click, look\\u2026';
	else {
		verdict.textContent = s.state === 'pass'
			? 'PASS: the AI saw this page, clicked the button, and saw it turn green.'
			: 'FAIL: ' + s.reason;
		numbers.textContent = 'look 1 ' + ms(s.look1_ms) + ' \\u00b7 click ' + ms(s.click_ms) + ' \\u00b7 look 2 ' + ms(s.look2_ms) +
			(s.before ? ' \\u00b7 button rgb ' + s.before.join(',') + ' \\u2192 ' + (s.after ? s.after.join(',') : '?') : '');
		next.textContent = s.state === 'pass'
			? 'Next: open the YouTube video you were given (captions off), press the Retinat bridge button in the toolbar, then "Share this tab with the AI". This tab is no longer shared; you can close it.'
			: 'To retry: run the setup command again. It opens this page itself; close this tab first if it is still open.';
	}
}
async function poll() {
	try {
		const r = await fetch('STATE_PATH', { cache: 'no-store' });
		if (r.ok) show(await r.json());
	} catch (e) {
		if (!seen) verdict.textContent = 'The relay is not running, so the self-test cannot run. Run the setup command again.';
		else if (seen.state === 'waiting' || seen.state === 'running') {
			verdict.textContent = 'The relay stopped before the self-test finished. Run the setup command again.';
		}
	}
	setTimeout(poll, seen && (seen.state === 'pass' || seen.state === 'fail') ? 1500 : 400);
}
poll();
</script>
</body>
</html>
""".replace('BUTTON_LEFT', str(BUTTON[0]))
	.replace('BUTTON_TOP', str(BUTTON[1]))
	.replace('BUTTON_WIDTH', str(BUTTON[2]))
	.replace('BUTTON_HEIGHT', str(BUTTON[3]))
	.replace('GREY', GREY)
	.replace('GREEN', GREEN)
	.replace('STATE_PATH', STATE_PATH)
)
