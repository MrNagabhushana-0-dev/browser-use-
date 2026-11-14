#!/usr/bin/env python3
"""A stdlib-only front for a stdio MCP server: answers the handshake from a cache while the real server starts.

Usage:
  $ python3 bin/mcp-front.py browser_use.retinat [server args...]

Claude Code gives a stdio server 30 s to answer `initialize`; on a freshly booted VM, `uv run` plus importing the `mcp`
package and browser_use off a cold disk can take longer. This process imports nothing but the standard library, so it
starts at once, spawns the real server (`bash bin/mcp-serve.sh <module> ...`, or `bash -c $MCP_FRONT_CHILD`), and
answers `initialize` and the first `tools/list` from the answers the real server gave last time. The real server still
gets every request; its duplicate responses are swallowed, and a changed tool list is announced with
`notifications/tools/list_changed`. Without a usable cache this is a plain pass-through.
"""

import hashlib
import json
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _cache_path(module: str) -> Path:
	base = os.environ.get('MCP_FRONT_CACHE_DIR') or str(Path.home() / '.cache' / 'browseruse' / 'mcp-front')
	return Path(base) / f'{module}-{hashlib.sha1(str(ROOT).encode()).hexdigest()[:8]}.json'


def _load_cache(path: Path) -> dict:
	try:
		data = json.loads(path.read_text(encoding='utf-8'))
		init = data.get('initialize')
		tools = data.get('tools')
		if not isinstance(init, dict) or not all(isinstance(v, dict) for v in init.values()):
			init = {}
		lists = data.get('lists')
		if not isinstance(lists, dict) or not all(isinstance(v, dict) for v in lists.values()):
			lists = {}
		return {'initialize': init, 'tools': tools if isinstance(tools, dict) else None, 'lists': lists}
	except Exception:
		return {'initialize': {}, 'tools': None, 'lists': {}}


# Asked by a client right after the handshake, with its own 30 s limit: answered from the cache while the real server
# starts. Found live: the real server answered them after 32 s, the client had given up, and it dropped the connection
# on the late answers ("a response for an unknown message ID").
OTHER_LISTS = ('prompts/list', 'resources/list', 'resources/templates/list')


def _key(msg_id) -> tuple:
	"""A request id with its JSON type kept: 1 and "1" are different ids."""
	return (type(msg_id).__name__, msg_id)


class Front:
	def __init__(self, module: str, args: list[str]) -> None:
		self.path = _cache_path(module)
		self.cache = _load_cache(self.path)
		self.out_lock = threading.Lock()
		self.in_lock = threading.Lock()
		self.state_lock = threading.Lock()
		self.swallow: dict[tuple, tuple[str, str | None]] = {}  # id -> (kind, protocolVersion)
		self.store: dict[tuple, tuple[str, str | None]] = {}  # pass-through id -> (kind, protocolVersion)
		self.pending: set[tuple] = set()  # requests the child has not answered yet
		self.child_ready = False
		self.served_tools: list | None = None
		self.stopping = False
		child_cmd = os.environ.get('MCP_FRONT_CHILD')
		cmd = ['bash', '-c', child_cmd] if child_cmd else ['bash', str(ROOT / 'bin' / 'mcp-serve.sh'), module, *args]
		self.child = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, cwd=str(ROOT))

	# --- output helpers ---
	def _to_client(self, raw: bytes) -> None:
		with self.out_lock:
			try:
				sys.stdout.buffer.write(raw if raw.endswith(b'\n') else raw + b'\n')
				sys.stdout.buffer.flush()
			except (BrokenPipeError, ValueError, OSError):
				pass

	def _to_child(self, raw: bytes) -> bool:
		with self.in_lock:
			try:
				assert self.child.stdin is not None
				self.child.stdin.write(raw if raw.endswith(b'\n') else raw + b'\n')
				self.child.stdin.flush()
				return True
			except (BrokenPipeError, ValueError, OSError):
				return False

	def _save_cache(self) -> None:
		try:
			self.path.parent.mkdir(parents=True, exist_ok=True)
			tmp = self.path.with_name(f'{self.path.name}.{os.getpid()}.tmp')
			tmp.write_text(json.dumps(self.cache), encoding='utf-8')
			os.replace(tmp, self.path)
		except Exception:
			pass

	# --- client -> child ---
	def pump_client(self) -> None:
		stdin = sys.stdin.buffer
		for raw in stdin:
			try:
				self._on_client_line(raw)
			except Exception:
				self._to_child(raw)
		self._close_child()

	def _on_client_line(self, raw: bytes) -> None:
		try:
			msg = json.loads(raw)
		except ValueError:
			msg = None
		if isinstance(msg, dict) and 'id' in msg and isinstance(msg.get('method'), str):
			params = msg.get('params') if isinstance(msg.get('params'), dict) else {}
			key = _key(msg['id'])
			with self.state_lock:
				self.pending.add(key)
			if msg['method'] == 'initialize':
				version = params.get('protocolVersion')
				cached = self.cache['initialize'].get(version) if isinstance(version, str) else None
				with self.state_lock:
					if cached is not None:
						# Found live: a client probes with `server/discover` first, gives up on it while the real server is
						# still starting, and connects with this cached `initialize`; the real server's late answer to the
						# probe ("a response for an unknown message") then made the client drop the connection. What was
						# asked before a cached `initialize` and is still unanswered is never passed on.
						for early in self.pending - {key}:
							self.store.pop(early, None)
							self.swallow.setdefault(early, ('early', None))
						self.swallow[key] = ('initialize', version)
					else:
						self.store[key] = ('initialize', version if isinstance(version, str) else None)
				if cached is not None:
					self._to_client(json.dumps({'jsonrpc': '2.0', 'id': msg['id'], 'result': cached}).encode())
			elif msg['method'] in OTHER_LISTS and 'cursor' not in params:
				with self.state_lock:
					cached = None if self.child_ready else self.cache['lists'].get(msg['method'])
					(self.swallow if cached is not None else self.store)[key] = ('list', msg['method'])
				if cached is not None:
					self._to_client(json.dumps({'jsonrpc': '2.0', 'id': msg['id'], 'result': cached}).encode())
			elif msg['method'] == 'tools/list' and 'cursor' not in params:
				with self.state_lock:
					tools = self.cache['tools']
					if not self.child_ready and tools is not None:
						self.swallow[key] = ('tools', None)
						self.served_tools = tools.get('tools')
						serve = tools
					else:
						self.store[key] = ('tools', None)
						serve = None
				if serve is not None:
					self._to_client(json.dumps({'jsonrpc': '2.0', 'id': msg['id'], 'result': serve}).encode())
		elif isinstance(msg, dict) and msg.get('method') == 'notifications/cancelled':
			# the client gave up on a request: its late answer would be "a response for an unknown message"
			gave_up = (msg.get('params') or {}).get('requestId') if isinstance(msg.get('params'), dict) else None
			if gave_up is not None:
				with self.state_lock:
					if _key(gave_up) in self.pending:
						self.store.pop(_key(gave_up), None)
						self.swallow.setdefault(_key(gave_up), ('cancelled', None))
		self._to_child(raw)

	def _close_child(self) -> None:
		try:
			assert self.child.stdin is not None
			with self.in_lock:
				self.child.stdin.close()
		except Exception:
			pass
		try:
			self.child.wait(timeout=5)
		except subprocess.TimeoutExpired:
			self.terminate()

	def terminate(self) -> None:
		self.stopping = True
		try:
			if self.child.poll() is None:
				self.child.terminate()
				try:
					self.child.wait(timeout=5)
				except subprocess.TimeoutExpired:
					self.child.kill()
		except Exception:
			pass

	# --- child -> client ---
	def pump_child(self) -> None:
		assert self.child.stdout is not None
		for raw in self.child.stdout:
			try:
				self._on_child_line(raw)
			except Exception:
				self._to_client(raw)

	def _on_child_line(self, raw: bytes) -> None:
		try:
			msg = json.loads(raw)
		except ValueError:
			msg = None
		if isinstance(msg, dict) and 'id' in msg and 'method' not in msg:
			key = _key(msg['id'])
			result = msg.get('result') if isinstance(msg.get('result'), dict) else None
			with self.state_lock:
				swallowed = self.swallow.pop(key, None)
				stored = self.store.pop(key, None)
				kind, version = swallowed or stored or (None, None)
				self.pending.discard(key)
				self.child_ready = True
				if result is not None and kind == 'initialize' and version is not None:
					self.cache['initialize'][version] = result
					self._save_cache()
				elif result is not None and kind == 'list' and version is not None and 'nextCursor' not in result:
					self.cache['lists'][version] = result
					self._save_cache()
				elif result is not None and kind == 'tools' and 'nextCursor' not in result:
					self.cache['tools'] = result
					self._save_cache()
				changed = (
					kind == 'tools'
					and swallowed is not None
					and result is not None
					and 'nextCursor' not in result
					and result.get('tools') != self.served_tools
				)
			if swallowed:
				if changed:
					self._to_client(json.dumps({'jsonrpc': '2.0', 'method': 'notifications/tools/list_changed'}).encode())
				return
		self._to_client(raw)


def main() -> int:
	if len(sys.argv) < 2:
		print('usage: mcp-front.py <module> [server args...]', file=sys.stderr)
		return 2
	front = Front(sys.argv[1], sys.argv[2:])

	def on_signal(signum, frame):
		front.terminate()
		os._exit(128 + signum)

	signal.signal(signal.SIGTERM, on_signal)
	signal.signal(signal.SIGINT, on_signal)
	t_out = threading.Thread(target=front.pump_child, daemon=True)
	t_in = threading.Thread(target=front.pump_client, daemon=True)
	t_out.start()
	t_in.start()
	rc = front.child.wait()
	t_out.join(timeout=2)
	return rc


if __name__ == '__main__':
	sys.exit(main())
