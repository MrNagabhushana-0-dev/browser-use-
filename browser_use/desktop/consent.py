"""Asking the person, on their own screen, to let the AI use some desktop apps (after Anthropic's request_access).

A small window opens on the desktop with the apps, the tier each would get and the AI's reason, and Allow / Deny. Only
the person can answer it:
- its app (WM_CLASS `retinat-consent`) can never be granted, so the AI's clicks and keys there are refused, and the
  AI's view of the screen covers it like any other app it may not use;
- the answer comes back over a loopback server whose address carries a random token. The window has no address bar
  and is hidden from the AI, so the token is never shown to it, and the server takes answers only from its own page.
"""

import asyncio
import html
import logging
import os
import secrets
import shutil
import tempfile

from aiohttp import web

from browser_use.desktop.service import CONSENT_CLASS, Tier, ceiling

logger = logging.getLogger(__name__)

RANK = {Tier.READ: 0, Tier.CLICK: 1, Tier.FULL: 2}

PAGE = """<!doctype html><meta charset="utf-8"><title>An AI asks to use apps</title>
<body style="margin:0;font:15px/1.4 system-ui,sans-serif;background:#fff8e6;color:#222">
<div style="padding:16px 18px">
<h2 style="margin:0 0 6px;font-size:17px">An AI on this computer asks to use these apps</h2>
<p style="margin:0 0 10px;color:#555">{reason}</p>
<form id="f">{rows}</form>
<p style="margin:10px 0 0;color:#666;font-size:13px">Until the session ends. The AI waits whenever you use the mouse or
keyboard, and it can't answer this window for you.</p>
</div>
<div style="position:fixed;right:18px;bottom:16px;display:flex;gap:10px">
<button id="deny" style="font:inherit;padding:8px 18px;width:110px">Deny</button>
<button id="allow" style="font:inherit;padding:8px 18px;width:110px;background:#1a7f37;color:#fff;border:0">Allow</button>
</div>
<script>
const answer = (allow) => fetch(location.pathname + '/answer', {{method: 'POST', headers: {{'content-type': 'application/json'}},
	body: JSON.stringify({{allow: allow ? [...document.querySelectorAll('input:checked')].map(i => i.value) : []}})}})
	.then(() => {{ document.body.textContent = allow ? 'Allowed.' : 'Denied.'; setTimeout(() => window.close(), 300); }});
document.getElementById('allow').onclick = () => answer(true);
document.getElementById('deny').onclick = () => answer(false);
</script></body>"""
ROW = '<label style="display:block;margin:4px 0"><input type="checkbox" value="{app}" checked> <b>{app}</b> — {what}</label>'
WHAT = {
	Tier.READ: 'look only',
	Tier.CLICK: 'click and scroll, no typing',
	Tier.FULL: 'click, type and use keys',
}


async def ask(display: str, wanted: dict[str, Tier], reason: str, timeout: float = 120.0) -> dict[str, Tier]:
	"""Show the request on `display` and wait for the person. Returns the apps they allowed (each capped at its kind's
	tier), or {} if they denied, closed the window or did not answer in time."""
	from browser_use.browser.watchdogs.local_browser_watchdog import LocalBrowserWatchdog
	from browser_use.mcp.effects import Refused

	asked = {app.lower(): tier for app, tier in wanted.items() if app.lower() != CONSENT_CLASS}
	assert asked, 'no apps to ask for'
	asked = {app: tier if RANK[tier] <= RANK[ceiling(app)] else ceiling(app) for app, tier in asked.items()}
	chrome = LocalBrowserWatchdog._find_installed_browser_path()
	if not chrome:
		raise Refused('there is no Chromium here to show the person the request; they can list apps in BROWSER_USE_DESKTOP_APPS')

	token = secrets.token_urlsafe(24)
	answered: asyncio.Future[list[str]] = asyncio.get_running_loop().create_future()
	rows = ''.join(ROW.format(app=html.escape(app), what=WHAT[tier]) for app, tier in sorted(asked.items()))
	page = PAGE.format(reason=html.escape(reason or 'No reason was given.'), rows=rows)

	own_origin: list[str] = []

	async def show(request: web.Request) -> web.Response:
		return web.Response(text=page, content_type='text/html')

	async def answer(request: web.Request) -> web.Response:
		if not own_origin or request.headers.get('Origin') != own_origin[0]:
			raise web.HTTPForbidden(text='answers come from the request window only')
		allow = (await request.json()).get('allow', [])
		if not answered.done():
			answered.set_result([a for a in allow if a in asked])
		return web.json_response({'ok': True})

	app = web.Application()
	app.router.add_get(f'/{token}', show)
	app.router.add_post(f'/{token}/answer', answer)
	runner = web.AppRunner(app, access_log=None)
	await runner.setup()
	site = web.TCPSite(runner, '127.0.0.1', 0)
	await site.start()
	port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
	own_origin.append(f'http://127.0.0.1:{port}')  # answers are taken from this page only

	profile = tempfile.mkdtemp(prefix='retinat-consent-')
	args = [chrome, f'--user-data-dir={profile}', '--no-first-run', '--no-default-browser-check', f'--class={CONSENT_CLASS}']
	args += ['--window-size=560,340', '--window-position=360,260', f'--app=http://127.0.0.1:{port}/{token}']
	if os.geteuid() == 0:
		args[1:1] = ['--no-sandbox', '--test-type']
	proc = await asyncio.create_subprocess_exec(
		*args, env={**os.environ, 'DISPLAY': display}, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
	)
	try:
		allowed = await asyncio.wait_for(answered, timeout)
	except TimeoutError:
		allowed = []
	finally:
		proc.terminate()
		try:
			await asyncio.wait_for(proc.wait(), 10)
		except TimeoutError:
			proc.kill()
		await runner.cleanup()
		shutil.rmtree(profile, ignore_errors=True)
	logger.info(f'🖥 Desktop access: asked {sorted(asked)}, allowed {sorted(allowed)}')
	return {app: asked[app] for app in allowed}


def describe(asked: dict[str, Tier], allowed: dict[str, Tier]) -> str:
	if not allowed:
		return f'The person did not allow {", ".join(sorted(asked))}. Ask them what they would like instead.'
	denied = sorted(set(asked) - set(allowed))
	given = ', '.join(f'{app} ({tier})' for app, tier in sorted(allowed.items()))
	return f'Allowed: {given}.' + (f' Not allowed: {", ".join(denied)}.' if denied else '')
