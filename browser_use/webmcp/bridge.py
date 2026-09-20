"""The JavaScript bridge injected into every page to implement WebMCP.

The point of injecting rather than sniffing: a page that does

    navigator.modelContext.registerTool({...})

throws on a browser that has no `navigator.modelContext`, so a WebMCP-aware site
guarded by a feature check registers *nothing* and an agent that merely probes for
`window.modelContext` finds an empty page forever. Installing the API before any page
script runs (`Page.addScriptToEvaluateOnNewDocument`) is what turns the site's own
declarations on.

The bridge lives in the main world because the `execute()` handlers it has to call are
page functions and closures — an isolated world could see the DOM but not them. The
consequence is that a page can inspect or clobber the bridge, which is acceptable:
the page is the one supplying the tools in the first place, and everything coming back
out is treated as untrusted by the Python side either way.

Truncation happens here, in the page, so an oversized tool result never crosses the
CDP WebSocket at all.
"""

# Property name the Python side reads. Non-enumerable so `Object.keys(window)` and
# fingerprinting scripts that enumerate globals do not trip over it.
BRIDGE_KEY = '__browserUseWebMCP__'

WEBMCP_BRIDGE_JS = r"""
(() => {
	'use strict';

	const KEY = '__browserUseWebMCP__';
	if (window[KEY]) return;

	// manifests: how many <link rel="model-context"> refs one document may make us load.
	// manifestMs: a deadline for the whole loading pass, not per request — the Python side
	// gives up after 3s, and without this the in-page promise chain kept fetching for 30s
	// per ref, once per agent step, all of it still in flight against the site.
	const LIMITS = { tools: 32, text: 512, schema: 4096, result: 16384, rpcMs: 30000, manifests: 8, manifestMs: 5000 };

	const clip = (value, limit) => {
		if (typeof value !== 'string') return '';
		return value.length > limit ? value.slice(0, limit - 1) + '…' : value;
	};

	// Stringify defensively: page objects may be circular, may have throwing getters,
	// or may be enormous. Any of those must degrade to a string, never throw.
	const safeJson = (value, limit) => {
		let out;
		try {
			out = JSON.stringify(value);
		} catch (err) {
			try { out = String(value); } catch (inner) { out = '[unserializable]'; }
		}
		if (typeof out !== 'string') out = String(out);
		return out.length > limit ? out.slice(0, limit) + '…[truncated]' : out;
	};

	// Round-trip the schema through JSON so we hand Python plain data, never a live
	// page object with getters. Oversized schemas fail the reparse and degrade to {}.
	const normalizeSchema = (schema) => {
		if (!schema || typeof schema !== 'object') return {};
		try { return JSON.parse(safeJson(schema, LIMITS.schema)); } catch (err) { return {}; }
	};

	const sameOrigin = (url) => {
		try { return new URL(url, location.href).origin === location.origin; } catch (err) { return false; }
	};

	// ---- tool registry (in-page handlers) --------------------------------------

	// name -> { descriptor, execute, thisArg, kind }
	// kind 'registered' comes from registerTool() and is additive; kind 'context'
	// comes from provideContext() and is replaced wholesale on each call.
	const tools = new Map();

	const describe = (tool, name) => ({
		name: name,
		description: clip(typeof tool.description === 'string' ? tool.description : '', LIMITS.text),
		inputSchema: normalizeSchema(tool.inputSchema || tool.input_schema || tool.parameters),
	});

	const prepare = (tool, kind) => {
		if (!tool || typeof tool !== 'object') throw new TypeError('WebMCP: a tool must be an object');
		const name = typeof tool.name === 'string' ? tool.name.trim() : '';
		if (!name) throw new TypeError('WebMCP: tool.name is required');
		const execute = tool.execute || tool.handler || tool.callback;
		if (typeof execute !== 'function') throw new TypeError('WebMCP: tool "' + name + '" needs an execute() function');
		return { descriptor: describe(tool, name), execute: execute, thisArg: tool, kind: kind };
	};

	const commit = (entry) => {
		const name = entry.descriptor.name;
		if (!tools.has(name) && tools.size >= LIMITS.tools) {
			throw new RangeError('WebMCP: at most ' + LIMITS.tools + ' tools can be registered');
		}
		tools.set(name, entry);
		return name;
	};

	const registerTool = (tool) => {
		const name = commit(prepare(tool, 'registered'));
		return { unregister: () => tools.delete(name) };
	};

	// Validate the whole batch before mutating anything, so a bad tool in the list
	// cannot leave the page with its previous context torn down and nothing to replace it.
	const provideContext = (context) => {
		const incoming = context && Array.isArray(context.tools) ? context.tools : [];
		const prepared = incoming.map((tool) => prepare(tool, 'context'));
		for (const [name, entry] of Array.from(tools)) {
			if (entry.kind === 'context') tools.delete(name);
		}
		for (const entry of prepared) commit(entry);
		return { toolCount: prepared.length };
	};

	// ---- manifests (<link rel="model-context">) --------------------------------

	// href -> parsed manifest. Cached for the document's lifetime so that re-running
	// discovery on every agent step costs one querySelectorAll, not a network round trip.
	const manifestCache = new Map();
	// name -> { name, description, inputSchema, endpoint } for manifest-declared tools.
	let manifestTools = new Map();
	// The discovery pass currently running, if any, so concurrent callers share one.
	let inFlightDiscovery = null;
	const runDiscovery = () => bridge._discover();
	let rpcId = 0;

	const manifestRefs = () => {
		const refs = [];
		const links = document.querySelectorAll('link[rel~="model-context"], link[rel~="modelcontext"]');
		for (const link of links) {
			if (link.href) refs.push({ kind: 'href', value: link.href });
		}
		const inline = document.querySelectorAll(
			'script[type="application/model-context+json"], script[type="application/mcp+json"]'
		);
		for (const el of inline) {
			refs.push({ kind: 'inline', value: el.textContent || '' });
		}
		return refs;
	};

	// A JSON-RPC response may arrive as plain JSON or as an SSE stream (MCP Streamable
	// HTTP). Handle both; for SSE take the last data: frame, which carries the result.
	const parsePayload = (text, contentType) => {
		if (contentType && contentType.indexOf('text/event-stream') !== -1) {
			let last = null;
			for (const line of text.split(/\r?\n/)) {
				if (line.indexOf('data:') === 0) {
					try { last = JSON.parse(line.slice(5).trim()); } catch (err) { /* skip partial frame */ }
				}
			}
			return last;
		}
		return JSON.parse(text);
	};

	const rpc = async (endpoint, method, params) => {
		if (!sameOrigin(endpoint)) throw new Error('WebMCP: refusing cross-origin endpoint ' + endpoint);
		const controller = new AbortController();
		const timer = setTimeout(() => controller.abort(), LIMITS.rpcMs);
		try {
			const res = await fetch(endpoint, {
				method: 'POST',
				credentials: 'same-origin',
				signal: controller.signal,
				headers: { 'content-type': 'application/json', accept: 'application/json, text/event-stream' },
				body: JSON.stringify({ jsonrpc: '2.0', id: ++rpcId, method: method, params: params || {} }),
			});
			const text = await res.text();
			if (!res.ok) throw new Error('HTTP ' + res.status + ' from ' + endpoint + ': ' + clip(text, 256));
			const payload = parsePayload(text, res.headers.get('content-type') || '');
			if (payload && payload.error) {
				throw new Error(payload.error.message || safeJson(payload.error, 256));
			}
			return payload ? payload.result : null;
		} finally {
			clearTimeout(timer);
		}
	};

	const loadManifests = async (errors) => {
		const found = [];
		const deadline = Date.now() + LIMITS.manifestMs;
		const refs = manifestRefs();
		if (refs.length > LIMITS.manifests) {
			errors.push('ignored ' + (refs.length - LIMITS.manifests) + ' manifest ref(s) over the limit');
		}
		for (const ref of refs.slice(0, LIMITS.manifests)) {
			if (Date.now() > deadline) { errors.push('manifest loading timed out'); break; }
			try {
				let doc;
				if (ref.kind === 'inline') {
					doc = JSON.parse(ref.value);
				} else if (!sameOrigin(ref.value)) {
					errors.push('ignored cross-origin manifest ' + ref.value);
					continue;
				} else if (manifestCache.has(ref.value)) {
					doc = manifestCache.get(ref.value);
				} else {
					const res = await fetch(ref.value, { credentials: 'same-origin', headers: { accept: 'application/json' } });
					if (!res.ok) { errors.push('manifest ' + ref.value + ' returned HTTP ' + res.status); continue; }
					doc = await res.json();
					manifestCache.set(ref.value, doc);
				}
				if (!doc || typeof doc !== 'object') { errors.push('manifest is not a JSON object'); continue; }

				const base = ref.kind === 'href' ? ref.value : location.href;
				const rawEndpoint = typeof doc.endpoint === 'string' ? doc.endpoint : '';
				const endpoint = rawEndpoint ? new URL(rawEndpoint, base).href : '';
				if (endpoint && !sameOrigin(endpoint)) {
					errors.push('ignored cross-origin endpoint ' + endpoint);
					continue;
				}

				// A manifest may inline its tool list, or name an endpoint we ask for one.
				let declared = Array.isArray(doc.tools) ? doc.tools : null;
				if (!declared && endpoint) {
					const listed = await rpc(endpoint, 'tools/list', {});
					declared = listed && Array.isArray(listed.tools) ? listed.tools : [];
				}
				for (const tool of declared || []) {
					if (!tool || typeof tool.name !== 'string' || !tool.name) continue;
					if (!endpoint) { errors.push('tool "' + tool.name + '" has no endpoint to call'); continue; }
					found.push({
						name: tool.name.trim(),
						description: clip(typeof tool.description === 'string' ? tool.description : '', LIMITS.text),
						inputSchema: normalizeSchema(tool.inputSchema || tool.input_schema || tool.parameters),
						endpoint: endpoint,
					});
				}
			} catch (err) {
				errors.push(clip('manifest error: ' + ((err && err.message) || String(err)), 256));
			}
		}
		return found;
	};

	// ---- result shaping ---------------------------------------------------------

	// MCP results are { content: [{type:'text', text}], isError? }, but page authors
	// return plain strings and plain objects too. Flatten all three to readable text.
	const resultToText = (result) => {
		if (result === null || result === undefined) return '';
		if (typeof result === 'string') return clip(result, LIMITS.result);
		if (typeof result !== 'object') return String(result);

		const content = result.content;
		if (Array.isArray(content)) {
			const parts = [];
			for (const item of content) {
				if (item && typeof item === 'object' && typeof item.text === 'string') parts.push(item.text);
				else parts.push(safeJson(item, 1024));
			}
			let text = parts.join('\n');
			if (result.structuredContent !== undefined) text += '\n' + safeJson(result.structuredContent, 2048);
			return clip(text, LIMITS.result);
		}
		return safeJson(result, LIMITS.result);
	};

	const callResult = (raw) => {
		const isError = !!(raw && typeof raw === 'object' && raw.isError);
		return JSON.stringify({ ok: !isError, content: resultToText(raw), error: isError ? 'the tool reported an error' : null });
	};

	const callFailure = (message) => JSON.stringify({ ok: false, content: '', error: clip(message, 512) });

	// ---- the bridge the Python side talks to ------------------------------------

	const bridge = {
		version: 1,

		discover() {
			// One pass at a time: discover() is called once per agent step, and a slow manifest
			// endpoint would otherwise stack a fresh fetch chain on every one of them.
			if (!inFlightDiscovery) {
				inFlightDiscovery = runDiscovery().finally(() => { inFlightDiscovery = null; });
			}
			return inFlightDiscovery;
		},

		async _discover() {
			const errors = [];
			const out = [];
			const seen = new Set();
			for (const entry of tools.values()) {
				out.push({
					name: entry.descriptor.name,
					description: entry.descriptor.description,
					inputSchema: entry.descriptor.inputSchema,
					source: 'js',
					endpoint: null,
				});
				seen.add(entry.descriptor.name);
			}
			// Built locally and swapped in whole. Clearing the live map and then awaiting meant
			// two overlapping discoveries wiped each other's entries, so a call arriving in
			// that window reported "no tool named X" for a tool the prompt had just listed.
			const nextManifestTools = new Map();
			for (const tool of await loadManifests(errors)) {
				// In-page handlers win: they run in the page's own session and need no network.
				if (seen.has(tool.name) || out.length >= LIMITS.tools) continue;
				seen.add(tool.name);
				nextManifestTools.set(tool.name, tool);
				out.push({
					name: tool.name,
					description: tool.description,
					inputSchema: tool.inputSchema,
					source: 'manifest',
					endpoint: tool.endpoint,
				});
			}
			manifestTools = nextManifestTools;
			return JSON.stringify({ url: location.href, origin: location.origin, tools: out, errors: errors });
		},

		async call(name, args) {
			const toolName = String(name == null ? '' : name);
			const params = args && typeof args === 'object' ? args : {};
			try {
				const entry = tools.get(toolName);
				if (entry) return callResult(await entry.execute.call(entry.thisArg, params));

				// A tool may have been declared since the last discovery pass.
				let manifestTool = manifestTools.get(toolName);
				if (!manifestTool) {
					await bridge.discover();
					manifestTool = manifestTools.get(toolName);
				}
				if (!manifestTool) {
					const known = Array.from(tools.keys()).concat(Array.from(manifestTools.keys()));
					return callFailure(
						'no WebMCP tool named "' + toolName + '" on ' + location.origin +
						(known.length ? '; available: ' + known.join(', ') : '; this page declares none')
					);
				}
				return callResult(await rpc(manifestTool.endpoint, 'tools/call', { name: toolName, arguments: params }));
			} catch (err) {
				return callFailure((err && err.message) || String(err));
			}
		},
	};

	Object.defineProperty(window, KEY, { value: bridge, enumerable: false, configurable: false, writable: false });

	// Never shadow a native implementation: if the browser ships WebMCP one day, the
	// real thing wins and the bridge just reads whatever it registers.
	const modelContext = {
		registerTool: registerTool,
		unregisterTool: (name) => tools.delete(String(name)),
		provideContext: provideContext,
		listTools: () => Array.from(tools.values()).map((entry) => Object.assign({}, entry.descriptor)),
		callTool: (name, args) => {
			const entry = tools.get(String(name));
			if (!entry) return Promise.reject(new Error('no WebMCP tool named "' + name + '"'));
			return Promise.resolve(entry.execute.call(entry.thisArg, args || {}));
		},
	};

	const install = (target, prop, value) => {
		if (!target || prop in target) return;
		try {
			Object.defineProperty(target, prop, { value: value, configurable: true, writable: true, enumerable: false });
		} catch (err) { /* a frozen target is the page's prerogative */ }
	};

	install(navigator, 'modelContext', modelContext);
	// Deliberately no window.agent alias: it is not part of any draft, so it is a free
	// fingerprint for anti-bot scripts, which is exactly what browser_use/human exists to
	// avoid handing out. navigator.modelContext is the spec's surface and the only one a
	// WebMCP-aware site looks for; window.modelContext stays for pages that check both.
	install(window, 'modelContext', modelContext);
	install(window, 'agent', { provideContext: provideContext, registerTool: registerTool });

	// Lets an already-loaded page (where we injected late) register after the fact.
	try { window.dispatchEvent(new Event('modelcontextready')); } catch (err) { /* no DOM yet */ }
})();
"""
