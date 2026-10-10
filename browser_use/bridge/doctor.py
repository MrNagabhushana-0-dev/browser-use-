"""`python -m browser_use.bridge doctor`: which link of person -> extension -> relay -> AI is broken, and the fix.

After BrowserSkill's `bsk doctor`. Each check is ok, warn, fail or na (not reachable because an earlier link failed);
`fix` is written for the person at the keyboard, since they are the one who can load, reload or share.
"""

import re
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict

from browser_use.bridge.policy import PASSIVE_GLOBS
from browser_use.bridge.relay import DEFAULT_PORT, _product

Status = Literal['ok', 'warn', 'fail', 'na']
NAMES = ('relay', 'extension', 'browser', 'policy', 'tabs', 'wheel')
FLAT_SESSIONS = 125  # chrome.debugger flat child sessions: pages inside cross-site iframes
IDLE_SAFE = 116  # an open WebSocket keeps the MV3 service worker alive


class Check(BaseModel):
	model_config = ConfigDict(extra='forbid')

	name: str
	status: Status
	detail: str
	fix: str = ''


async def fetch_status(port: int = DEFAULT_PORT, timeout: float = 8.0) -> dict[str, Any]:
	"""The relay's own status, or what is (not) listening on its port instead."""
	base = f'http://127.0.0.1:{port}'
	async with httpx.AsyncClient(trust_env=False, timeout=timeout) as http:
		try:
			r = await http.get(f'{base}/bridge/status')
		except httpx.HTTPError:
			return {'url': base, 'reachable': False}
		try:
			mine = r.json() if r.status_code == 200 else {}
		except ValueError:
			mine = {}
		if isinstance(mine, dict) and mine.get('relay') == 'retinat-bridge':
			return mine
		try:
			other = (await http.get(f'{base}/json/version')).json().get('Browser', 'something else')
		except (httpx.HTTPError, ValueError):
			other = 'something else'
		return {'url': base, 'reachable': True, 'foreign': other}


async def diagnose(port: int = DEFAULT_PORT) -> list[Check]:
	return checks(await fetch_status(port))


def checks(status: dict[str, Any]) -> list[Check]:
	"""Judge a status (from `BridgeRelay.status()` or `fetch_status`). Pure: no I/O."""
	url = status.get('url', f'http://127.0.0.1:{DEFAULT_PORT}')
	port = url.rsplit(':', 1)[-1]
	if status.get('relay') != 'retinat-bridge':
		if status.get('reachable'):
			relay = Check(
				name='relay',
				status='fail',
				detail=f'{status.get("foreign")} is listening on {url}, not the Retinat bridge relay',
				fix='Stop it, or run the relay on another port (python -m browser_use.bridge --port N) and write the '
				'extension for that port (python -m browser_use.bridge extension DIR --port N).',
			)
		else:
			relay = Check(
				name='relay',
				status='fail',
				detail=f'nothing is listening on {url}',
				fix=f'Start it: python -m browser_use.bridge --port {port} (or retinat --bridge {port}, which starts its own).',
			)
		return [relay] + [Check(name=n, status='na', detail='needs the relay') for n in NAMES[1:]]

	clients = status.get('clients', 0)
	out = [Check(name='relay', status='ok', detail=f'on {url}, {clients} AI client{"" if clients == 1 else "s"} connected')]
	ext = status.get('extension')
	if not ext:
		out.append(
			Check(
				name='extension',
				status='fail',
				detail='the browser extension has not connected',
				fix='Open your browser with the Retinat bridge extension loaded: chrome://extensions (or edge://, brave://, '
				'vivaldi://, opera://extensions), Developer mode on, "Load unpacked", pick the extension folder. If it is '
				f'already there, make sure it is switched on and was written for port {port}.',
			)
		)
		return out + [Check(name=n, status='na', detail='needs the extension') for n in NAMES[2:]]

	out.append(_extension(ext, status.get('expected_version')))
	out.append(_browser(ext.get('userAgent', '')))
	out.append(_policy(status.get('policy'), status.get('expected_policy') or list(PASSIVE_GLOBS)))
	tabs = status.get('tabs') or []
	if tabs:
		named = '; '.join(f'{t["title"] or t["url"]}' for t in tabs[:3]) + ('; ...' if len(tabs) > 3 else '')
		out.append(Check(name='tabs', status='ok', detail=f'{len(tabs)} shared: {named}'))
	else:
		out.append(
			Check(
				name='tabs',
				status='warn',
				detail='no tab is shared, so the AI sees nothing',
				fix='On the tab you want help with, press the extension button and "Share this tab with the AI" (or Alt+Shift+A).',
			)
		)
	out.append(_wheel(status.get('holder', 'agent'), bool(status.get('stopped'))))
	assert [c.name for c in out] == list(NAMES)
	return out


def _extension(ext: dict[str, Any], expected: str | None) -> Check:
	version = ext.get('version') or '?'
	head = f'Retinat bridge {version} (Manifest V{ext.get("manifest", "?")})'
	if ext.get('answers_ms') is None:
		return Check(
			name='extension',
			status='fail',
			detail=f'{head} is connected but not answering',
			fix='Reload the extension on chrome://extensions (its service worker is stuck), then share the tab again.',
		)
	if expected and version != expected:
		return Check(
			name='extension',
			status='warn',
			detail=f'{head} answers in {ext["answers_ms"]:.0f} ms, but this relay ships {expected}',
			fix='Reload the extension on chrome://extensions, or load the folder from this install again. '
			'Branded Chrome keeps running the old copy until you reload it.',
		)
	return Check(name='extension', status='ok', detail=f'{head} answers in {ext["answers_ms"]:.0f} ms')


def _browser(user_agent: str) -> Check:
	product = _product(user_agent)
	match = re.search(r'Chrome/(\d+)', user_agent)
	major = int(match.group(1)) if match else 0
	base = f'{product} (Chromium {major or "unknown"})'
	limits = []
	if major and major < FLAT_SESSIONS:
		limits.append(f'pages inside cross-site iframes cannot be driven before Chromium {FLAT_SESSIONS}')
	if major and major < IDLE_SAFE:
		limits.append(f'the connection can drop while idle before Chromium {IDLE_SAFE}')
	if limits:
		return Check(
			name='browser',
			status='warn',
			detail=f'{base}: ' + '; '.join(limits),
			fix='Update the browser if you can; everything else works.',
		)
	if product.startswith('Vivaldi'):
		base += '. Vivaldi shows no "started debugging" bar; the pill at the bottom of a shared tab is the sign'
	return Check(name='browser', status='ok', detail=base)


def _policy(theirs: list[str] | None, ours: list[str]) -> Check:
	if theirs is None:
		return Check(
			name='policy',
			status='warn',
			detail='the extension did not report which methods only look (it predates this check)',
			fix='Reload the extension on chrome://extensions so it runs the copy that matches this relay.',
		)
	looser, stricter = sorted(set(theirs) - set(ours)), sorted(set(ours) - set(theirs))
	if looser:
		return Check(
			name='policy',
			status='fail',
			detail=f'the extension treats more methods as only looking than this relay does: {", ".join(looser[:5])}',
			fix='Load the extension folder from this install (or reload it); a different copy is running.',
		)
	if stricter:
		return Check(
			name='policy',
			status='warn',
			detail=f'the extension refuses some looking methods this relay allows: {", ".join(stricter[:5])}',
			fix='Reload the extension so both sides use the same list.',
		)
	return Check(name='policy', status='ok', detail=f'{len(ours)} looking-only method patterns, same on both sides')


def _wheel(holder: str, stopped: bool) -> Check:
	if stopped:
		return Check(
			name='wheel',
			status='warn',
			detail='Cancel was pressed on the "started debugging" bar, so the AI is stopped',
			fix='Share a tab again when you want the AI back.',
		)
	if holder == 'human':
		return Check(
			name='wheel',
			status='warn',
			detail='you have the wheel; the AI can only look',
			fix='Press "Hand back" on the pill in the shared tab, or Alt+Shift+Z.',
		)
	return Check(name='wheel', status='ok', detail='the AI may act in shared tabs')


def summary(found: list[Check]) -> str:
	"""The failing and warning checks with their fixes, for an error message an AI can relay to the person."""
	return ' '.join(f'{c.name}: {c.detail}. {c.fix}'.strip() for c in found if c.status in ('fail', 'warn'))


def _log_checks(found: list[Check]) -> str:
	mark = {'ok': '✓', 'warn': '!', 'fail': '✗', 'na': '·'}
	lines = []
	for c in found:
		lines.append(f'{mark[c.status]} {c.name:<9} {c.detail}')
		if c.fix:
			lines.append(f'  {"":<9} → {c.fix}')
	return '\n'.join(lines)
