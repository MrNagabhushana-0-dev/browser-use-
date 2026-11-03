"""memd: a small local memory daemon (Mem0 + fastembed + embedded Qdrant). Free, no API key, loopback only.

Runs inside the venv that has mem0ai and fastembed (default ~/agent-memory). The model is loaded once, so a lookup
costs milliseconds instead of the 3-4 s a fresh process spends loading it. Every request needs the bearer token in
~/agent-memory/token (created on first start, mode 600). Nothing here is committed to git: the store is local data.

usage: ~/agent-memory/bin/python scripts/memory/memd.py [--port 8787] [--home ~/agent-memory]
  POST /add     {"text": str, "user_id": str = "owner", "metadata": {}}   -> {"id": ...}
  POST /search  {"query": str, "user_id": str = "owner", "limit": int = 5} -> {"results": [{"memory", "score"}]}
  GET  /health                                                           -> {"ok": true, "count": n}
  POST /export  {}                                                       -> {"path": ...}  (JSONL under <home>/export)
"""

import argparse
import json
import os
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

os.environ.setdefault('MEM0_TELEMETRY', 'False')  # no telemetry: this runs on the owner's machine only

EMBED_MODEL = 'BAAI/bge-small-en-v1.5'
EMBED_DIMS = 384
COLLECTION = 'eyes_memory'
MAX_BODY = 1_000_000


def make_memory(home: Path):
	from mem0 import Memory

	os.environ.setdefault('MEM0_DIR', str(home / 'data'))
	config = {
		'embedder': {'provider': 'fastembed', 'config': {'model': EMBED_MODEL}},
		'vector_store': {
			'provider': 'qdrant',
			'config': {'path': str(home / 'data' / 'qdrant'), 'embedding_model_dims': EMBED_DIMS, 'collection_name': COLLECTION},
		},
		# only used when infer=True; this daemon stores text as given (infer=False), so no model key is needed
		'llm': {'provider': 'openai', 'config': {'model': 'unused', 'api_key': 'unused'}},
	}
	return Memory.from_config(config)


def read_token(home: Path) -> str:
	path = home / 'token'
	if not path.exists():
		path.write_text(secrets.token_hex(24))
		path.chmod(0o600)
	return path.read_text().strip()


def serve(home: Path, port: int) -> None:
	home.mkdir(parents=True, exist_ok=True)
	memory = make_memory(home)
	token = read_token(home)
	lock = threading.Lock()  # one embedded Qdrant client: requests take turns

	class Handler(BaseHTTPRequestHandler):
		def log_message(self, *args) -> None:  # quiet
			pass

		def _reply(self, code: int, body: dict) -> None:
			data = json.dumps(body).encode()
			self.send_response(code)
			self.send_header('Content-Type', 'application/json')
			self.send_header('Content-Length', str(len(data)))
			self.end_headers()
			self.wfile.write(data)

		def _authorized(self) -> bool:
			return secrets.compare_digest(self.headers.get('Authorization', ''), f'Bearer {token}')

		def _body(self) -> dict:
			n = int(self.headers.get('Content-Length') or 0)
			if n > MAX_BODY:
				raise ValueError('body too large')
			return json.loads(self.rfile.read(n) or b'{}')

		def do_GET(self) -> None:
			if not self._authorized():
				return self._reply(401, {'error': 'unauthorized'})
			if self.path != '/health':
				return self._reply(404, {'error': 'not found'})
			with lock:
				count = len(memory.get_all(filters={'user_id': 'owner'}, limit=10000).get('results', []))
			self._reply(200, {'ok': True, 'count': count})

		def do_POST(self) -> None:
			if not self._authorized():
				return self._reply(401, {'error': 'unauthorized'})
			try:
				body = self._body()
				user = str(body.get('user_id') or 'owner')
				with lock:
					if self.path == '/add':
						text = str(body.get('text') or '').strip()
						if not text:
							return self._reply(400, {'error': 'empty text'})
						meta = {**(body.get('metadata') or {}), 'ts': round(time.time())}
						out = memory.add(text, user_id=user, infer=False, metadata=meta)
						return self._reply(200, {'id': (out.get('results') or [{}])[0].get('id')})
					if self.path == '/search':
						query = str(body.get('query') or '').strip()
						if not query:
							return self._reply(200, {'results': []})
						found = memory.search(query, filters={'user_id': user}, limit=int(body.get('limit') or 5))
						hits = found.get('results', found) if isinstance(found, dict) else found
						return self._reply(200, {'results': [{'memory': h['memory'], 'score': h.get('score')} for h in hits]})
					if self.path == '/export':
						rows = memory.get_all(filters={'user_id': user}, limit=100000).get('results', [])
						out_dir = home / 'export'
						out_dir.mkdir(exist_ok=True)
						path = out_dir / f'{user}-{int(time.time())}.jsonl'
						path.write_text(
							'\n'.join(json.dumps({'memory': r['memory'], 'metadata': r.get('metadata')}) for r in rows)
						)
						return self._reply(200, {'path': str(path), 'count': len(rows)})
				self._reply(404, {'error': 'not found'})
			except (ValueError, KeyError, json.JSONDecodeError) as e:
				self._reply(400, {'error': str(e)})

	ThreadingHTTPServer(('127.0.0.1', port), Handler).serve_forever()


if __name__ == '__main__':
	parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
	parser.add_argument('--port', type=int, default=8787)
	parser.add_argument('--home', type=Path, default=Path.home() / 'agent-memory')
	args = parser.parse_args()
	serve(args.home, args.port)
