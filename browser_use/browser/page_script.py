"""Run agent-authored JavaScript against the live page and get structured data back.

The motivation is token economy. Reading a 200-row table through the normal loop means
serializing all 200 rows into browser_state and asking the model to read them back out —
the table crosses the context window at least twice, once as markup and once as prose.
A script does it in one step: ~200 characters of JavaScript go out, exactly the requested
fields come back. The same applies to bulk action: ticking 50 checkboxes is one call
rather than 50 click/observe rounds.

browser-use's own Online-Mind2Web report names this class of change — giving the agent
code over the page instead of step-by-step interaction — as their single largest accuracy
improvement, on the grounds that it "aligns much better with the LLM's training
distribution." Models write DOM-querying code far more reliably than they plan twenty
sequential clicks.

Everything here runs inside the page, so results are truncated in-page and never cross
the CDP socket at full size.
"""

# Small helpers injected into the script's scope. These exist purely to shorten what the
# model has to write: `$$('tr')` instead of `Array.from(document.querySelectorAll('tr'))`
# is the difference between a one-line script and a wrapped one, on every single call.
_HELPERS = r"""
	const $ = (sel, root) => (root || document).querySelector(sel);
	const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));
	const txt = (el) => (el && (el.innerText || el.textContent) || '').trim().replace(/\s+/g, ' ');
	const attr = (el, name) => (el && el.getAttribute ? el.getAttribute(name) : null);
"""


def build_page_script(script: str, max_chars: int) -> str:
	"""Wrap an agent-authored snippet so it can be evaluated safely and reported on.

	The snippet runs as an async function body, so it may use `await` and must `return`
	its result. Anything it throws is caught and reported as data rather than surfacing
	as a raw CDP exception, because an agent recovers from a message far better than
	from a stack trace.
	"""
	assert max_chars > 0, 'max_chars must be positive'

	# The snippet is injected as source, not as a string literal, so it needs no escaping.
	# That is safe here in a way it would not be for tool *arguments*: this text is the
	# model's own code, which is the entire point of the action, rather than page data
	# being smuggled into a code position.
	return (
		'(async () => {\n'
		'\tconst __MAX = ' + str(max_chars) + ';\n'
		# A DOM node, a window, or a circular structure would make JSON.stringify throw or
		# emit {}. Collapse nodes to a readable descriptor so a model that returns elements
		# by mistake still gets something it can act on instead of a wall of empty objects.
		'\tconst __replacer = (key, value) => {\n'
		'\t\tif (value instanceof Element) return { tag: value.tagName.toLowerCase(), text: (value.innerText || "").trim().slice(0, 200) };\n'
		'\t\tif (value instanceof Node) return String(value.nodeValue || "").slice(0, 200);\n'
		'\t\tif (typeof value === "function") return "[function]";\n'
		'\t\tif (typeof window !== "undefined" && value === window) return "[window]";\n'
		'\t\treturn value;\n'
		'\t};\n'
		'\ttry {\n' + _HELPERS + '\t\tconst __value = await (async () => {\n' + script + '\n\t\t})();\n'
		'\t\tlet __text;\n'
		'\t\ttry { __text = JSON.stringify(__value, __replacer); } catch (e) { __text = String(__value); }\n'
		'\t\tif (__text === undefined) __text = "null";\n'
		'\t\tconst __full = __text.length;\n'
		'\t\tconst __truncated = __full > __MAX;\n'
		'\t\tif (__truncated) __text = __text.slice(0, __MAX);\n'
		'\t\treturn JSON.stringify({ ok: true, value: __text, truncated: __truncated, full_length: __full });\n'
		'\t} catch (err) {\n'
		'\t\tconst __msg = (err && err.message) ? err.message : String(err);\n'
		'\t\treturn JSON.stringify({ ok: false, error: String(__msg).slice(0, 600) });\n'
		'\t}\n'
		'})()'
	)
