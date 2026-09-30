"""A token meter and a visible cursor, drawn inside the page.

Two reasons to see what an agent is spending and doing, in the browser it is driving:

- The meter. Token cost is invisible by default — it is a number in a log after the fact. A
  live readout in the corner of the page makes the trade being made (a contact sheet instead
  of a stream of screenshots) something you can watch instead of take on trust.
- The cursor. The agent's pointer is synthesised over CDP, and Chrome does not move the
  operating system's cursor for synthetic events, so a screen recording of an agent clicking
  shows buttons pressing themselves. A cursor drawn in the page from the same `mousemove`
  events the page sees shows where the agent is pointing, with no extra protocol traffic.

Both live in one shadow root on a fixed, click-through element, so a page's CSS cannot restyle
them and they cannot intercept a click. The host carries `data-bu-overlay`, which is the
contract with everything that captures pixels: whatever is marked is hidden for the capture,
so the meter never ends up baked into a keyframe the model is about to read.

The meter's text is kept in sessionStorage so it survives a same-origin navigation. A
cross-origin navigation loses it until the next `show()`, because storage is per-origin.
"""

import json
import logging
from typing import TYPE_CHECKING, Any

from browser_use.vision.video import TokenLedger

if TYPE_CHECKING:
	from browser_use.browser.session import BrowserSession

logger = logging.getLogger(__name__)

# The attribute that marks an element as ours to hide while pixels are being captured.
OVERLAY_ATTRIBUTE = 'data-bu-overlay'

_OVERLAY_JS = """(() => {
	if (window.__buOverlay) return;
	const host = document.createElement('div');
	host.id = '__bu_overlay';
	host.setAttribute('data-bu-overlay', '');
	host.style.cssText = 'position:fixed;inset:0;pointer-events:none;z-index:2147483647;';
	const root = host.attachShadow({ mode: 'open' });
	root.innerHTML = `<style>
		#hud { position:absolute; top:10px; right:10px; display:none; min-width:210px; padding:9px 12px;
		       background:rgba(8,12,10,.82); color:#d7ffe3; border:1px solid rgba(120,255,170,.35);
		       border-radius:9px; font:12px/1.4 ui-monospace,SFMono-Regular,Menlo,monospace; white-space:pre;
		       box-shadow:0 4px 18px rgba(0,0,0,.35); }
		#cur { position:absolute; left:-60px; top:-60px; width:22px; height:22px; display:none;
		       filter:drop-shadow(0 1px 2px rgba(0,0,0,.6)); transform-origin:2px 2px; }
		#cur.down { transform:scale(.8); }
	</style>
	<div id="hud"></div>
	<svg id="cur" viewBox="0 0 22 22"><path d="M2 2 L2 17 L6.2 13.2 L9 19.5 L11.6 18.4 L8.9 12.2 L14.5 12 Z"
		fill="#fff" stroke="#111" stroke-width="1.3" stroke-linejoin="round"/></svg>`;
	const hud = root.getElementById('hud');
	const cur = root.getElementById('cur');
	const show = (lines) => {
		hud.textContent = lines.join('\\n');
		hud.style.display = lines.length ? 'block' : 'none';
		try { sessionStorage.setItem('__bu_hud', JSON.stringify(lines)); } catch (e) {}
	};
	const move = (e) => { cur.style.display = 'block'; cur.style.left = e.clientX + 'px'; cur.style.top = e.clientY + 'px'; };
	document.addEventListener('mousemove', move, true);
	document.addEventListener('mousedown', (e) => { move(e); cur.classList.add('down'); }, true);
	document.addEventListener('mouseup', () => cur.classList.remove('down'), true);
	window.__buOverlay = { show };
	const mount = () => { (document.documentElement || document).appendChild(host); };
	if (document.documentElement) mount(); else document.addEventListener('readystatechange', mount, { once: true });
	try { const saved = sessionStorage.getItem('__bu_hud'); if (saved) show(JSON.parse(saved)); } catch (e) {}
})();"""


class Overlay:
	"""Installs the meter and cursor into a session's pages and updates the meter."""

	def __init__(self, browser_session: 'BrowserSession') -> None:
		self.browser_session = browser_session

	async def _cdp(self) -> Any:
		return await self.browser_session.get_or_create_cdp_session(focus=False)

	async def install(self) -> None:
		"""Draw the overlay on the current page and on every page this tab loads next."""
		cdp = await self._cdp()
		await cdp.cdp_client.send.Page.addScriptToEvaluateOnNewDocument(params={'source': _OVERLAY_JS}, session_id=cdp.session_id)
		await cdp.cdp_client.send.Runtime.evaluate(params={'expression': _OVERLAY_JS}, session_id=cdp.session_id)

	async def show(self, lines: list[str]) -> None:
		"""Replace the meter's text. An empty list hides it."""
		cdp = await self._cdp()
		await cdp.cdp_client.send.Runtime.evaluate(
			params={'expression': f'window.__buOverlay && window.__buOverlay.show({json.dumps(lines)})'},
			session_id=cdp.session_id,
		)

	async def show_ledger(
		self, ledger: TokenLedger, seconds: float, width: int, height: int, title: str = 'vision tokens'
	) -> None:
		"""Show what was sent against what a screenshot a second would have cost."""
		await self.show(self.ledger_lines(ledger, seconds, width, height, title))

	@staticmethod
	def ledger_lines(ledger: TokenLedger, seconds: float, width: int, height: int, title: str = 'vision tokens') -> list[str]:
		naive = TokenLedger.naive_screenshot_tokens(seconds, width, height, every=1.0)
		saved = 100 * (1 - ledger.total / naive) if naive else 0.0
		return [
			f'browser-use  {title}',
			f'sent        ~{ledger.total:>9,} tok',
			f'1 shot/sec  ~{naive:>9,} tok',
			f'saved        {saved:>9.2f} %',
			'(estimates: w*h/750)',
		]
