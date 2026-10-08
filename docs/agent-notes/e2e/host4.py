"""Hosts the silent-save pages for the blind retinat_requests run. Truth goes to $TRUTH4 (kept outside the repository).

Each page has a note box and a Save button. Save posts to the server, which answers 500 with an error code in its
JSON body; the page shows nothing at all, as a buggy app would. Only the network response carries the code, so an
agent can report it only by reading what the page fetched. Codes come from `secrets` at start, not from the seed.
Scoring rule, fixed before any run: the answer contains the exact code (case-insensitive); anything else is wrong.
"""

import hashlib
import json
import os
import secrets
import sys
from pathlib import Path

from werkzeug import Request, Response
from werkzeug.serving import run_simple

HERE = Path(__file__).parent
SEEDS = (51, 52, 53)
CONDITIONS = ('all-tools', 'no-requests')
PORT = 8767
SALT = (HERE / 'salt').read_text().strip() if (HERE / 'salt').exists() else 'silent-save'

PAGE = """<!doctype html><title>Notes</title>
<body style="margin:0;font:16px sans-serif;background:#fafafa">
<main style="max-width:640px;margin:40px auto">
<h1>My notes</h1>
<textarea id="note" style="width:100%;height:140px"></textarea>
<p><button id="save" style="font:inherit;padding:8px 20px">Save</button></p>
</main>
<script>
document.getElementById('save').onclick = () => {
	fetch('/api/save/{token}', {method: 'POST', headers: {'content-type': 'application/json'},
		body: JSON.stringify({note: document.getElementById('note').value})}).catch(() => {});
	// a buggy app: it never looks at the answer and says nothing either way
};
</script></body>"""


def build() -> tuple[dict[str, str], dict[str, str]]:
	pages, codes, runs = {}, {}, []
	for seed in SEEDS:
		for cond in CONDITIONS:
			token = hashlib.sha256(f'{SALT}-{seed}-{cond}'.encode()).hexdigest()[:8]
			code = 'Q-' + ''.join(secrets.choice('ACDEFHJKLMNPRTUVWXY3479') for _ in range(5))
			pages[f'/p/{token}'] = PAGE.replace('{token}', token)
			codes[token] = code
			runs.append(
				{
					'id': f'{seed}-{cond}',
					'seed': seed,
					'condition': cond,
					'url': f'http://127.0.0.1:{PORT}/p/{token}',
					'truth': code,
				}
			)
	Path(os.environ.get('TRUTH4', HERE / 'truth4.json')).write_text(json.dumps(runs, indent=1))
	return pages, codes


def correct(truth: str, answer: str) -> bool:
	return truth.upper() in answer.upper()


if __name__ == '__main__':
	pages, codes = build()

	@Request.application
	def app(request: Request) -> Response:
		print(request.method, request.path, file=sys.stderr, flush=True)
		if request.path in pages:
			return Response(pages[request.path], mimetype='text/html')
		if request.path.startswith('/api/save/') and request.method == 'POST':
			token = request.path.rsplit('/', 1)[-1]
			if token in codes:
				body = {'ok': False, 'error': f'storage quota exceeded for this account (code {codes[token]})'}
				return Response(json.dumps(body), status=500, mimetype='application/json')
		return Response('not found', status=404)

	run_simple('127.0.0.1', PORT, app)
