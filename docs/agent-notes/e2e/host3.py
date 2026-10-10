"""Hosts the fine-print pages for the blind find/zoom run. Truth goes to truth3.json (agents never see it).

Codes come from `secrets` at start, not from the seed, so this file says nothing about them; truth is written to
$TRUTH3 (kept outside the repository during runs).

A long page with a six-character coupon code in 6 px grey type at the bottom. In the `dom` variant the code is
page text (so `retinat_find` can land on it); in `canvas` it is drawn on a canvas next to a readable label, so
only pixels carry it and `retinat_zoom` has to read it. Each (variant, seed, condition) gets its own random URL.
Scoring rule, fixed before any run: the exact code, case-insensitive; anything else is wrong.
"""

import hashlib
import json
import os
import random
import secrets
import sys
from pathlib import Path

from werkzeug import Request, Response
from werkzeug.serving import run_simple

HERE = Path(__file__).parent
SEEDS = (41, 42, 43)
VARIANTS = ('dom', 'canvas')
CONDITIONS = ('look-only', 'find-zoom')
ALPHABET = 'ACDEFHJKLMNPRTUVWXY3479'  # no 0/O, 1/I, 5/S, 2/Z, 6/G, 8/B: a misread is a misread, not a font quirk
PORT = 8766
SALT = (HERE / 'salt').read_text().strip() if (HERE / 'salt').exists() else 'fine-print'

FILLER = (
	'Our spring range is here, with new colours across the shop. Delivery takes two to four working days, and '
	'returns are free within thirty days of delivery. Sizes run true; if you are between two, pick the larger. '
)

PAGE = """<!doctype html><title>Spring offers</title>
<body style="margin:0;font:17px/1.6 Georgia,serif;background:#fff;color:#222">
<main style="max-width:860px;margin:0 auto;padding:40px">
<h1>Spring offers</h1>
{paragraphs}
<footer style="margin-top:60px;border-top:1px solid #eee;padding-top:16px">
{fine}
</footer></main>{script}</body>"""

DOM_FINE = '<p style="font:6px/1.2 sans-serif;color:#aaa">Terms apply. Coupon code: {code}. One per customer, while stocks last.</p>'
CANVAS_FINE = (
	'<p style="font:13px sans-serif;color:#888">Terms apply. Your coupon code is printed below.</p>'
	'<canvas id="c" width="260" height="14"></canvas>'
)
CANVAS_SCRIPT = """<script>const x = document.getElementById('c').getContext('2d');
x.font = '6px sans-serif'; x.fillStyle = '#aaa'; x.fillText(String.fromCharCode(...{codes}.map(c => c ^ {key})), 2, 9);</script>"""


def build() -> dict[str, str]:
	routes, runs = {}, []
	for seed in SEEDS:
		rng = random.Random(seed)
		for variant in VARIANTS:
			code = ''.join(secrets.choice(ALPHABET) for _ in range(6))
			paragraphs = '\n'.join(f'<p>{FILLER * rng.randint(2, 4)}</p>' for _ in range(9))
			if variant == 'dom':
				html = PAGE.format(paragraphs=paragraphs, fine=DOM_FINE.format(code=code), script='')
			else:
				key = secrets.randbelow(80) + 17
				script = CANVAS_SCRIPT.format(codes=[ord(c) ^ key for c in code], key=key)
				html = PAGE.format(paragraphs=paragraphs, fine=CANVAS_FINE, script=script)
			for cond in CONDITIONS:
				token = hashlib.sha256(f'{SALT}-{variant}-{seed}-{cond}'.encode()).hexdigest()[:8]
				routes[f'/p/{token}'] = html
				runs.append(
					{
						'id': f'{variant}-{seed}-{cond}',
						'variant': variant,
						'seed': seed,
						'condition': cond,
						'url': f'http://127.0.0.1:{PORT}/p/{token}',
						'truth': code,
					}
				)
	Path(os.environ.get('TRUTH3', HERE / 'truth3.json')).write_text(json.dumps(runs, indent=1))
	return routes


def correct(truth: str, answer: str) -> bool:
	return answer.strip().upper() == truth.upper()


if __name__ == '__main__':
	routes = build()

	@Request.application
	def app(request: Request) -> Response:
		html = routes.get(request.path)
		print(request.path, request.headers.get('User-Agent', '')[:80], file=sys.stderr, flush=True)
		return Response(html, mimetype='text/html') if html else Response('not found', status=404)

	run_simple('127.0.0.1', PORT, app)
