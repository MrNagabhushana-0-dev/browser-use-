"""Hosts seeded eyesbench tasks for the blind end-to-end run. Truth goes to truth.json (agents never see it).

Each (task, seed, condition) gets its own random URL, so a URL says nothing about the answer, and no run
sees another's cache. The toast ID is not in the page source as text: it is decoded at show time.
"""

import hashlib
import json
import random
import sys
import time
from pathlib import Path

from werkzeug import Request, Response
from werkzeug.serving import run_simple

sys.path.insert(0, '/home/user/browser-use-')
from browser_use.eyes import bench  # noqa: E402

HERE = Path(__file__).parent
WORK = HERE / 'media'
WORK.mkdir(exist_ok=True)
SEEDS = (21, 22, 23)
CONDITIONS = ('retina', 'screenshots', 'dom')
SALT = (HERE / 'salt').read_text().strip() if (HERE / 'salt').exists() else ''

OBFUSCATED_TOAST = """<!doctype html><title>orders</title><body style="margin:0;font:16px sans-serif;background:#f4f4f4">
<main style="padding:40px"><h1>Your orders</h1><p>Recent activity appears here.</p></main>
<script>setTimeout(() => {{ const k = {key}; const t = document.createElement('div');
t.setAttribute('role', 'status'); t.textContent = 'Order #' + String.fromCharCode(...{codes}.map(c => c ^ k)) + ' confirmed';
t.style.cssText = 'position:fixed;left:50%;bottom:60px;transform:translateX(-50%);background:#ff7a00;color:#000;'
  + 'padding:28px 48px;font:bold 28px sans-serif;border-radius:12px';
document.body.appendChild(t); setTimeout(() => t.remove(), {dur_ms}); }}, {at_ms});</script></body>"""


def build():
	routes, runs = {}, []
	for seed in SEEDS:
		for make in (bench.flash_task, bench.beeps_task, bench.toast_task):
			task = make(seed, WORK)
			if task.name == 'toast':
				key = random.Random(seed * 7).randint(17, 99)
				codes = [ord(c) ^ key for c in str(task.truth['id'])]
				task.html = OBFUSCATED_TOAST.format(key=key, codes=codes, dur_ms=1500, at_ms=int(task.truth['at'] * 1000))
			for cond in CONDITIONS:
				token = hashlib.sha256(f'{SALT}-{task.name}-{seed}-{cond}'.encode()).hexdigest()[:8]
				routes[f'/p/{token}'] = ('page', task.page(f'/m/{token}.webm'))
				if task.media:
					routes[f'/m/{token}.webm'] = ('media', task.media)
				runs.append({'id': f'{task.name}-{seed}-{cond}', 'task': task.name, 'seed': seed, 'condition': cond,
					'url': f'http://127.0.0.1:8765/p/{token}', 'question': task.question,
					'truth': task.answer, 'detail': {k: v for k, v in task.truth.items()}})
	(HERE / 'truth.json').write_text(json.dumps(runs, indent=1))
	return routes


ROUTES = build()


@Request.application
def app(request: Request):
	with (HERE / 'access.log').open('a') as log:
		log.write(f"{time.time():.1f} {request.path} {request.headers.get('User-Agent', '-')[:60]}\n")
	hit = ROUTES.get(request.path)
	if not hit:
		return Response('not found', status=404)
	kind, body = hit
	if kind == 'media':
		return bench.media_response(request, body)
	return Response(body, content_type='text/html')


if __name__ == '__main__':
	run_simple('127.0.0.1', 8765, app, threaded=True)
