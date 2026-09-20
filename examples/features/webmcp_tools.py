"""
Call the tools a website declares, instead of clicking through its UI.

A WebMCP-aware page publishes typed, callable tools:

    navigator.modelContext.registerTool({
        name: 'add_to_cart',
        description: 'Add a product to the cart',
        inputSchema: {type: 'object', properties: {sku: {type: 'string'}}, required: ['sku']},
        async execute({sku}) { ... },
    })

browser-use installs that API into every page before the page's own scripts run, so
sites guarded by `if (navigator.modelContext)` actually register. Whatever they declare
is discovered per page, listed to the model in a `<webmcp_tools>` block, and invoked
with the `call_webmcp_tool` action — one step with typed arguments and a typed result,
in place of a find-element / click / type / re-read loop.

This example serves its own WebMCP page, so it runs offline and needs no API key for
the first half. Turn on the agent half with an LLM key if you want to watch a model
pick the declared tool over the form sitting right next to it.
"""

import asyncio
import os
import sys
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from dotenv import load_dotenv

load_dotenv()

from browser_use import BrowserSession

# A storefront that offers the same capability twice: as a form a vision agent would
# have to drive, and as a declared tool an agent can just call.
SHOP_HTML = """<!DOCTYPE html>
<html>
<head><title>Sockshop</title></head>
<body>
<h1>Sockshop</h1>
<form id="cart-form">
	<label>SKU <input id="sku" name="sku"></label>
	<label>Quantity <input id="qty" name="qty" type="number" value="1"></label>
	<button type="submit">Add to cart</button>
</form>
<pre id="cart">cart is empty</pre>

<script>
	const cart = [];
	function addToCart(sku, qty) {
		cart.push({sku, qty});
		document.getElementById('cart').textContent = JSON.stringify(cart, null, 2);
		return `added ${qty} x ${sku}`;
	}

	// Feature-detected, exactly as a real site would write it. Without the bridge
	// browser-use injects, this whole block is skipped and the agent is left with the form.
	if (navigator.modelContext) {
		navigator.modelContext.registerTool({
			name: 'add_to_cart',
			description: 'Add a product to the shopping cart by SKU',
			inputSchema: {
				type: 'object',
				properties: {sku: {type: 'string'}, qty: {type: 'number'}},
				required: ['sku'],
			},
			async execute({sku, qty}) {
				return {content: [{type: 'text', text: addToCart(sku, qty || 1)}]};
			},
		});

		navigator.modelContext.registerTool({
			name: 'read_cart',
			description: 'Return the current cart contents as JSON',
			inputSchema: {type: 'object', properties: {}},
			execute: () => JSON.stringify(cart),
		});
	}
</script>
</body>
</html>"""


def serve(directory: str) -> ThreadingHTTPServer:
	"""Serve the demo shop on a free localhost port."""
	handler = partial(SimpleHTTPRequestHandler, directory=directory)
	server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
	threading.Thread(target=server.serve_forever, daemon=True).start()
	return server


async def main() -> None:
	with TemporaryDirectory() as tmp:
		(Path(tmp) / 'index.html').write_text(SHOP_HTML)
		server = serve(tmp)
		url = f'http://127.0.0.1:{server.server_address[1]}/index.html'

		browser_session = BrowserSession(headless=True)
		await browser_session.start()
		try:
			await browser_session.navigate_to(url)

			# 1. What did this page declare? Discovery is automatic — every agent step
			#    already carries this listing, this call just shows you what it holds.
			page_tools = await browser_session.get_webmcp_tools()
			print(f'\n{len(page_tools.tools)} tool(s) declared by {page_tools.origin}:')
			for tool in page_tools.tools:
				print(f'  {tool.prompt_line()}')

			# 2. Call one directly. No element index, no click, no typing, no re-read.
			result = await browser_session.call_webmcp_tool('add_to_cart', {'sku': 'SOCK-42', 'qty': 3})
			print(f'\nadd_to_cart -> ok={result.ok} content={result.content!r}')

			# 3. The page's own state really changed; this was not a simulated call.
			cart = await browser_session.call_webmcp_tool('read_cart')
			print(f'read_cart   -> {cart.content}')

			# 4. This is verbatim what the model sees in <browser_state> each step.
			print('\n--- what the agent is told ---')
			print(page_tools.prompt_description())

			if os.getenv('OPENAI_API_KEY') or os.getenv('ANTHROPIC_API_KEY'):
				from browser_use import Agent, ChatOpenAI

				print('\n--- letting an agent choose for itself ---')
				agent = Agent(
					task=f'Go to {url} and add 2 of SKU SOCK-7 to the cart, then report the cart contents.',
					llm=ChatOpenAI(model='gpt-4.1-mini'),
					browser_session=browser_session,
				)
				await agent.run(max_steps=6)
			else:
				print('\nSet OPENAI_API_KEY to also watch an agent pick the declared tool over the form.')
		finally:
			await browser_session.kill()
			server.shutdown()


if __name__ == '__main__':
	asyncio.run(main())
