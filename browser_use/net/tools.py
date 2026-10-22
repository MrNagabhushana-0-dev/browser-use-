"""The two tools that choose a browser's route (direct or Tor), as an MCP client lists them. Light on purpose: a
server's tool list is read from here before the rest of browser-use is loaded (see `retinat/front.py`)."""

import mcp.types as types


def network_tools(prefix: str) -> list[types.Tool]:
	"""The two tools an agent (or a UI toggle) uses to choose the route."""
	return [
		types.Tool(
			name=f'{prefix}_network',
			description=(
				'Choose how the browser reaches the web. mode "off" = direct; "auto" = direct, then retry once '
				'through Tor after a network failure or a "not available in your country" page; "always" = '
				'through Tor. exit_country is a two-letter code such as "de" or "jp" (omit for any). Use it '
				'for public pages a network censors or geo-fences. It does NOT get past bot walls or CAPTCHAs '
				'(Tor exits are challenged more, and walls are reported, never bypassed), and you must never '
				'log in or enter credentials over Tor. Changing route restarts the browser and closes its tabs.'
			),
			input_schema={
				'type': 'object',
				'properties': {
					'mode': {'type': 'string', 'enum': ['off', 'auto', 'always']},
					'exit_country': {'type': 'string', 'description': 'Two-letter country code, e.g. "de".'},
					'reason': {'type': 'string', 'description': 'Why, for the session log.'},
				},
				'required': ['mode'],
			},
		),
		types.Tool(
			name=f'{prefix}_network_status',
			description='The current route, the exit Tor reports (address and country), and recent route events.',
			input_schema={'type': 'object', 'properties': {}},
			annotations=types.ToolAnnotations(read_only_hint=True),
		),
	]
