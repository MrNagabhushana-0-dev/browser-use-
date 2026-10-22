"""MCP Server for browser-use - exposes browser automation capabilities via Model Context Protocol.

This server provides tools for:
- Running autonomous browser tasks with an AI agent
- Direct browser control (navigation, clicking, typing, etc.)
- Content extraction from web pages
- File system operations

Usage:
    uvx browser-use --mcp

Or as an MCP server in Claude Desktop or other MCP clients:
    {
        "mcpServers": {
            "browser-use": {
                "command": "uvx",
                "args": ["browser-use[cli]", "--mcp"],
                "env": {
                    "OPENAI_API_KEY": "sk-proj-1234567890",
                }
            }
        }
    }
"""

import os
import sys

# Set environment variables BEFORE any browser_use imports to prevent early logging
os.environ['BROWSER_USE_LOGGING_LEVEL'] = 'critical'
os.environ['BROWSER_USE_SETUP_LOGGING'] = 'false'

import asyncio
import base64
import json
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from browser_use.llm import ChatAWSBedrock

if TYPE_CHECKING:
	from browser_use.bridge import BridgeRelay

# Configure logging for MCP mode - redirect to stderr but preserve critical diagnostics
logging.basicConfig(
	stream=sys.stderr, level=logging.WARNING, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', force=True
)

try:
	import psutil

	PSUTIL_AVAILABLE = True
except ImportError:
	PSUTIL_AVAILABLE = False

# Import and configure logging to use stderr before other imports
from browser_use.logging_config import setup_logging


def _configure_mcp_server_logging():
	"""Configure logging for MCP server mode - redirect all logs to stderr to prevent JSON RPC interference."""
	# Set environment to suppress browser-use logging during server mode
	os.environ['BROWSER_USE_LOGGING_LEVEL'] = 'warning'
	os.environ['BROWSER_USE_SETUP_LOGGING'] = 'false'  # Prevent automatic logging setup

	# Configure logging to stderr for MCP mode - preserve warnings and above for troubleshooting
	setup_logging(stream=sys.stderr, log_level='warning', force_setup=True)

	# Also configure the root logger and all existing loggers to use stderr
	logging.root.handlers = []
	stderr_handler = logging.StreamHandler(sys.stderr)
	stderr_handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
	logging.root.addHandler(stderr_handler)
	logging.root.setLevel(logging.CRITICAL)

	# Configure all existing loggers to use stderr and CRITICAL level
	for name in list(logging.root.manager.loggerDict.keys()):
		logger_obj = logging.getLogger(name)
		logger_obj.handlers = []
		logger_obj.setLevel(logging.CRITICAL)
		logger_obj.addHandler(stderr_handler)
		logger_obj.propagate = False


# Configure MCP server logging before any browser_use imports to capture early log lines
_configure_mcp_server_logging()

# Additional suppression - disable all logging completely for MCP mode
logging.disable(logging.CRITICAL)

# Import browser_use modules
from browser_use import ActionModel, Agent
from browser_use.bridge.policy import NOT_ALLOWED
from browser_use.browser import BrowserProfile, BrowserSession
from browser_use.browser.profile import PROXY_CA_LEARNED
from browser_use.config import get_default_llm, get_default_profile, load_browser_use_config
from browser_use.filesystem.file_system import FileSystem
from browser_use.llm.openai.chat import ChatOpenAI
from browser_use.mcp.effects import Refused
from browser_use.mcp.tools import browser_tools

# The browser_* tools this server answers; any other browser_* name is refused before a browser is started for it.
BROWSER_TOOLS = frozenset(
	{
		'browser_call_page_tool',
		'browser_click',
		'browser_close',
		'browser_close_all',
		'browser_close_session',
		'browser_close_tab',
		'browser_extract_content',
		'browser_get_html',
		'browser_get_state',
		'browser_go_back',
		'browser_list_page_tools',
		'browser_list_sessions',
		'browser_list_tabs',
		'browser_navigate',
		'browser_network',
		'browser_network_status',
		'browser_run_script',
		'browser_screenshot',
		'browser_scroll',
		'browser_switch_tab',
		'browser_type',
	}
)
# A handler's text that is really a failure (see handle_call_tool).
TEXT_FAILURES = ('Error:', 'Error closing session', 'Refused:', 'Element with index ')
from browser_use.net import NetworkPolicyError, NetworkRouter, Outcome, classify_navigation
from browser_use.tools.service import Tools

logger = logging.getLogger(__name__)

# How long letting go of a tab's eyes may take when they move to another tab (found: a hung page held every look for
# 60 s, in the new tab too)
EYES_CLOSE_S = 3.0
# Namespace for tools that belong to the page rather than to browser-use, so a site's
# `search` can never shadow `browser_navigate`.
SITE_TOOL_PREFIX = 'site_'
AUTOPLAY_WITHOUT_GESTURE = '--autoplay-policy=no-user-gesture-required'


def _ensure_all_loggers_use_stderr():
	"""Ensure ALL loggers only output to stderr, not stdout."""
	# Get the stderr handler
	stderr_handler = None
	for handler in logging.root.handlers:
		if hasattr(handler, 'stream') and handler.stream == sys.stderr:  # type: ignore
			stderr_handler = handler
			break

	if not stderr_handler:
		stderr_handler = logging.StreamHandler(sys.stderr)
		stderr_handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))

	# Configure root logger
	logging.root.handlers = [stderr_handler]
	logging.root.setLevel(logging.CRITICAL)

	# Configure all existing loggers
	for name in list(logging.root.manager.loggerDict.keys()):
		logger_obj = logging.getLogger(name)
		logger_obj.handlers = [stderr_handler]
		logger_obj.setLevel(logging.CRITICAL)
		logger_obj.propagate = False


# Ensure stderr logging after all imports
_ensure_all_loggers_use_stderr()


# Try to import MCP SDK
try:
	import mcp.server.stdio
	import mcp.types as types
	from mcp.server import Server

	MCP_AVAILABLE = True

	# Configure MCP SDK logging to stderr as well
	mcp_logger = logging.getLogger('mcp')
	mcp_logger.handlers = []
	mcp_logger.addHandler(logging.root.handlers[0] if logging.root.handlers else logging.StreamHandler(sys.stderr))
	mcp_logger.setLevel(logging.ERROR)
	mcp_logger.propagate = False
except ImportError:
	MCP_AVAILABLE = False
	logger.error('MCP SDK not installed. Install with: pip install mcp')
	sys.exit(1)

from browser_use.telemetry import MCPServerTelemetryEvent, ProductTelemetry
from browser_use.utils import create_task_with_error_handling, get_browser_use_version


def get_parent_process_cmdline() -> str | None:
	"""Get the command line of all parent processes up the chain."""
	if not PSUTIL_AVAILABLE:
		return None

	try:
		cmdlines = []
		current_process = psutil.Process()
		parent = current_process.parent()

		while parent:
			try:
				cmdline = parent.cmdline()
				if cmdline:
					cmdlines.append(' '.join(cmdline))
			except (psutil.AccessDenied, psutil.NoSuchProcess):
				# Skip processes we can't access (like system processes)
				pass

			try:
				parent = parent.parent()
			except (psutil.AccessDenied, psutil.NoSuchProcess):
				# Can't go further up the chain
				break

		return ';'.join(cmdlines) if cmdlines else None
	except Exception:
		# If we can't get parent process info, just return None
		return None


class BrowserUseServer:
	"""MCP Server for browser-use capabilities."""

	def __init__(self, session_timeout_minutes: int = 10, network: NetworkRouter | None = None):
		# Ensure all logging goes to stderr (in case new loggers were created)
		_ensure_all_loggers_use_stderr()

		self.server = Server('browser-use', version=get_browser_use_version())
		self.config = load_browser_use_config()
		self.agent: Agent | None = None
		self.browser_session: BrowserSession | None = None
		self.tools: Tools | None = None
		self._read_only_tools: set[str] = set()  # their failures changed nothing
		self._schemas: dict[str, dict[str, Any]] = {}  # calls missing a required argument are refused
		self._remember_listed(browser_tools())  # a call can come before any list reaches this server (through the front)
		self.llm: ChatOpenAI | None = None
		self.file_system: FileSystem | None = None
		self._telemetry = ProductTelemetry()
		# Snapshot of the current page's tool surface, refreshed on navigation.
		self._site_tools: dict[str, Any] = {}
		# The URL the snapshot above describes, so a page change by any route invalidates it.
		self._site_tools_url: str = ''
		self._start_time = time.time()
		# Direct, or Tor with a chosen exit country: see browser_use/net/policy.py.
		self.network = network or NetworkRouter.from_env()
		# Set by servers that attach to a Chrome the person runs; its proxy isn't ours to change.
		self.cdp_url: str | None = None
		# The person's own browser through the bridge extension (BROWSER_USE_BRIDGE or retinat --bridge).
		self.bridge: 'BridgeRelay | None' = None

		# Session management
		self.active_sessions: dict[str, dict[str, Any]] = {}  # session_id -> session info
		self.session_timeout_minutes = session_timeout_minutes
		self._cleanup_task: Any = None

		# Setup handlers
		self._setup_handlers()

	def _setup_handlers(self):
		"""Setup MCP server handlers."""

		async def handle_list_tools(_context: Any, _params: types.PaginatedRequestParams) -> types.ListToolsResult:
			"""List all available browser-use tools."""
			tools = [
				*browser_tools(),
				# Whatever the page in front of us offers, as first-class tools. Asking a
				# client to call browser_list_page_tools first means most never will; the
				# point of the whole synthesis layer is that `search(query=...)` is simply
				# there once you are on a site that can search.
				*self._site_tool_entries(),
			]
			self._remember_listed(tools)
			return types.ListToolsResult(tools=tools)

		async def handle_list_resources(_context: Any, _params: types.PaginatedRequestParams) -> types.ListResourcesResult:
			"""List available resources (none for browser-use)."""
			return types.ListResourcesResult(resources=[])

		async def handle_list_prompts(_context: Any, _params: types.PaginatedRequestParams) -> types.ListPromptsResult:
			"""List available prompts (none for browser-use)."""
			return types.ListPromptsResult(prompts=[])

		async def handle_call_tool(_context: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
			"""Handle tool execution."""
			name = params.name
			arguments = params.arguments
			start_time = time.time()
			error_msg = None
			from browser_use.mcp import effects

			token = effects.begin()
			try:
				effects.check_arguments(self._schemas.get(name), arguments or {})
				result = await self._execute_tool(name, arguments or {})
				if isinstance(result, list):
					return types.CallToolResult(content=result)
				if result.startswith(TEXT_FAILURES):
					# Handlers below report most failures as text; they are checks made before anything was sent,
					# except a session that failed while closing, which may be half closed.
					error_msg = result.removeprefix('Error: ')
					after = result.startswith('Error closing session')
					error = RuntimeError(error_msg) if after else Refused(error_msg)
					return effects.failure(name, error, read_only=name in self._read_only_tools, instrumented=False)
				return types.CallToolResult(content=[types.TextContent(type='text', text=result)])
			except Exception as e:
				error_msg = str(e)
				logger.error(f'Tool execution failed: {e}', exc_info=True)
				# These tools don't mark when they start sending, so an acting one that fails may have acted (fail closed).
				read_only = name in self._read_only_tools
				return effects.failure(name, e, read_only=read_only, instrumented=False)
			finally:
				effects.end(token)
				# Capture telemetry for tool calls
				duration = time.time() - start_time
				self._telemetry.capture(
					MCPServerTelemetryEvent(
						version=get_browser_use_version(),
						action='tool_call',
						tool_name=name,
						duration_seconds=duration,
						error_message=error_msg,
					)
				)

		self.server.add_request_handler('tools/list', types.PaginatedRequestParams, handle_list_tools)
		self.server.add_request_handler('resources/list', types.PaginatedRequestParams, handle_list_resources)
		self.server.add_request_handler('prompts/list', types.PaginatedRequestParams, handle_list_prompts)
		self.server.add_request_handler('tools/call', types.CallToolRequestParams, handle_call_tool)

	def _remember_listed(self, tools: list[types.Tool]) -> None:
		"""What calls are checked against: the tools last listed, the page's own among them."""
		self._read_only_tools = {t.name for t in tools if t.annotations and t.annotations.read_only_hint}
		self._schemas = {t.name: t.input_schema for t in tools}

	async def _execute_tool(self, tool_name: str, arguments: dict[str, Any]) -> str | list[types.ContentBlock]:
		"""Execute a browser-use tool. Returns str for most tools, or a content list for tools with image output."""

		# Agent-based tools
		if tool_name == 'retry_with_browser_use_agent':
			return await self._retry_with_browser_use_agent(
				task=arguments['task'],
				max_steps=arguments.get('max_steps', 100),
				model=arguments.get('model'),
				allowed_domains=arguments.get('allowed_domains'),
				use_vision=arguments.get('use_vision', True),
			)

		# The route (direct or Tor) is a property of the server, not of a page: no session needed.
		if tool_name == 'browser_network':
			return await self._network_set(arguments)
		elif tool_name == 'browser_network_status':
			return await self.network.status()

		# Browser session management tools (don't require active session)
		elif tool_name == 'browser_list_sessions':
			return await self._list_sessions()

		elif tool_name == 'browser_close_session':
			return await self._close_session(arguments['session_id'])

		elif tool_name == 'browser_close_all':
			return await self._close_all_sessions()

		# Tools belonging to the page in front of us, advertised after navigation. Must sit
		# in the outer chain: the browser_* branch below only matches our own tool names.
		elif tool_name.startswith(SITE_TOOL_PREFIX):
			if not self.browser_session:
				await self._init_browser_session()
			return await self._call_site_tool(tool_name, arguments)

		# Watching and moving like a person: see browser_use/eyes.
		elif tool_name.startswith('eyes_'):
			if not self.browser_session:
				await self._init_browser_session()
			return await self._call_eyes_tool(tool_name, arguments)

		# Direct browser control tools (require active session)
		elif tool_name.startswith('browser_'):
			if tool_name not in BROWSER_TOOLS:
				raise Refused(f'Unknown tool: {tool_name}')  # before starting a browser for it
			# Ensure browser session exists
			if not self.browser_session:
				await self._init_browser_session()

			if tool_name == 'browser_navigate':
				return await self._navigate(arguments['url'], arguments.get('new_tab', False))

			elif tool_name == 'browser_click':
				return await self._click(
					index=arguments.get('index'),
					coordinate_x=arguments.get('coordinate_x'),
					coordinate_y=arguments.get('coordinate_y'),
					new_tab=arguments.get('new_tab', False),
				)

			elif tool_name == 'browser_type':
				return await self._type_text(arguments['index'], arguments['text'])

			elif tool_name == 'browser_get_state':
				state_json, screenshot_b64 = await self._get_browser_state(arguments.get('include_screenshot', False))
				content: list[types.ContentBlock] = [types.TextContent(type='text', text=state_json)]
				if screenshot_b64:
					content.append(types.ImageContent(type='image', data=screenshot_b64, mime_type='image/png'))
				return content

			elif tool_name == 'browser_get_html':
				return await self._get_html(arguments.get('selector'))

			elif tool_name == 'browser_run_script':
				return await self._run_script(arguments['script'])

			elif tool_name == 'browser_list_page_tools':
				return await self._list_page_tools()

			elif tool_name == 'browser_call_page_tool':
				return await self._call_page_tool(arguments['name'], arguments.get('arguments', '{}'))

			elif tool_name == 'browser_screenshot':
				meta_json, screenshot_b64 = await self._screenshot(arguments.get('full_page', False))
				content: list[types.ContentBlock] = [types.TextContent(type='text', text=meta_json)]
				if screenshot_b64:
					content.append(types.ImageContent(type='image', data=screenshot_b64, mime_type='image/png'))
				return content

			elif tool_name == 'browser_extract_content':
				return await self._extract_content(arguments['query'], arguments.get('extract_links', False))

			elif tool_name == 'browser_scroll':
				return await self._scroll(arguments.get('direction', 'down'))

			elif tool_name == 'browser_go_back':
				return await self._go_back()

			elif tool_name == 'browser_close':
				return await self._close_browser()

			elif tool_name == 'browser_list_tabs':
				return await self._list_tabs()

			elif tool_name == 'browser_switch_tab':
				return await self._switch_tab(arguments['tab_id'])

			elif tool_name == 'browser_close_tab':
				return await self._close_tab(arguments['tab_id'])

		raise Refused(f'Unknown tool: {tool_name}')

	# -- eyes ----------------------------------------------------------------------------

	async def _eyes(self):
		"""The Eyes for the tab in front of us, (re)attached when the tab changes."""
		from browser_use.eyes import Eyes

		assert self.browser_session is not None
		focus = self.browser_session.agent_focus_target_id
		eyes = getattr(self, '_eyes_instance', None)
		if eyes is not None and (eyes.browser_session is not self.browser_session or (focus and eyes.retina.target_id != focus)):
			try:
				# a page that hung (a script that never yields) answers nothing: letting go of it must not wait on it
				await asyncio.wait_for(eyes.close(), EYES_CLOSE_S)
			except Exception:
				pass
			eyes = None
		if eyes is None:
			eyes = Eyes(self.browser_session)
			await eyes.open(focus)
			self._eyes_instance = eyes
		return eyes

	@staticmethod
	def _percept_content(percept) -> list[types.ContentBlock]:
		content: list[types.ContentBlock] = [types.TextContent(type='text', text=percept.text)]
		if percept.image:
			content.append(
				types.ImageContent(type='image', data=base64.b64encode(percept.image).decode(), mime_type='image/jpeg')
			)
		return content

	async def _call_eyes_tool(self, tool_name: str, arguments: dict[str, Any]) -> str | list[types.ContentBlock]:
		eyes = await self._eyes()
		detail = arguments.get('detail', 'glance')
		if tool_name == 'eyes_watch':
			percept = await eyes.watch(
				seconds=float(arguments.get('seconds', 12)),
				until=arguments.get('until', 'bored'),
				detail=detail,
				hold=bool(arguments.get('hold', True)),
			)
			return self._percept_content(percept)
		if tool_name == 'eyes_browse':
			percept = await eyes.browse(
				items=int(arguments.get('items', 5)),
				max_seconds=float(arguments.get('max_seconds', 12)),
				min_seconds=float(arguments.get('min_seconds', 3)),
				detail=detail,
			)
			return self._percept_content(percept)
		if tool_name == 'eyes_next':
			result = await eyes.next(direction=arguments.get('direction', 'down'))
			if not result.moved:
				return f'The feed did not move (tried {", ".join(result.tries)} over {result.seconds:.1f}s). {eyes.now_line()}'
			return f'Moved by {result.method} in {result.seconds:.1f}s. {eyes.now_line()}'
		if tool_name == 'eyes_tap':
			await eyes.tap(float(arguments['x']), float(arguments['y']))
			return f'Tapped ({arguments["x"]}, {arguments["y"]}). {eyes.now_line()}'
		if tool_name == 'eyes_swipe':
			info = await eyes.swipe(arguments.get('direction', 'up'), float(arguments.get('fraction', 0.55)))
			return f'Swiped {arguments.get("direction", "up")} {info["distance_px"]:.0f}px in {info["duration_ms"]:.0f}ms. {eyes.now_line()}'
		if tool_name == 'eyes_now':
			await eyes.retina.wait_for_data(1.0)
			return eyes.now_line()
		raise Refused(f'Unknown tool: {tool_name}')

	async def _init_browser_session(self, allowed_domains: list[str] | None = None, start_timeout: float | None = None, **kwargs):
		"""Initialize browser session using config. start_timeout: seconds the browser start may take (see
		BrowserSession.start)."""
		if self.browser_session:
			return

		# Ensure all logging goes to stderr before browser initialization
		_ensure_all_loggers_use_stderr()

		logger.debug('Initializing browser session...')

		# Get profile config
		profile_config = get_default_profile(self.config)

		# Merge profile config with defaults and overrides
		profile_data = {
			'downloads_path': str(Path.home() / 'Downloads' / 'browser-use-mcp'),
			'wait_between_actions': 0.5,
			'keep_alive': True,
			'user_data_dir': '~/.config/browseruse/profiles/default',
			'device_scale_factor': 1.0,
			'disable_security': False,
			'headless': False,
			**profile_config,  # Config values override defaults
		}

		# An agent never makes a user gesture, so under Chrome's default policy the eyes' AudioContext stays
		# suspended and every video is silent to them. Same switch as Chrome's own kiosk/automation setups.
		profile_data['args'] = [*(profile_data.get('args') or []), AUTOPLAY_WITHOUT_GESTURE]

		# Tool parameter overrides (highest priority)
		if allowed_domains is not None:
			profile_data['allowed_domains'] = allowed_domains

		if self.bridge is not None and 'cdp_url' not in kwargs:
			from browser_use.bridge import bridge_session_kwargs

			kwargs = {**bridge_session_kwargs(self.bridge.cdp_url), **kwargs}

		# Merge any additional kwargs that are valid BrowserProfile fields
		for key, value in kwargs.items():
			profile_data[key] = value

		# Through Tor the profile gets the SOCKS proxy, leak-guard flags and a throwaway profile. An
		# attached Chrome keeps its own connection: there is nothing of ours to route.
		if not profile_data.get('cdp_url'):
			profile_data.update(await self.network.session_kwargs())

		# Create browser profile
		profile = BrowserProfile(**profile_data)

		# Create browser session
		session = self.browser_session = BrowserSession(browser_profile=profile)
		try:
			await session.start(timeout=start_timeout)
		except BaseException:
			# keep no half-started session: every later call would fail on it instead of retrying the launch
			if self.browser_session is session:
				self.browser_session = None
			try:
				await session.kill()
			except Exception:
				pass
			raise
		if self.browser_session is not session:
			# dropped while it started (a route change): it is not the browser wanted now
			await session.kill()
			raise RuntimeError('The browser was replaced while it started. Nothing was done; call again.')

		# Track the session for management
		self._track_session(session)

		# Create tools for direct actions
		self.tools = Tools()

		# Initialize LLM from config
		llm_config = get_default_llm(self.config)
		base_url = llm_config.get('base_url', None)
		kwargs = {}
		if base_url:
			kwargs['base_url'] = base_url
		if api_key := llm_config.get('api_key'):
			self.llm = ChatOpenAI(
				model=llm_config.get('model', 'gpt-o4-mini'),
				api_key=api_key,
				temperature=llm_config.get('temperature', 0.7),
				**kwargs,
			)

		# Initialize FileSystem for extraction actions
		file_system_path = profile_config.get('file_system_path', '~/.browser-use-mcp')
		self.file_system = FileSystem(base_dir=Path(file_system_path).expanduser())

		logger.debug('Browser session initialized')

	async def _retry_with_browser_use_agent(
		self,
		task: str,
		max_steps: int = 100,
		model: str | None = None,
		allowed_domains: list[str] | None = None,
		use_vision: bool = True,
	) -> str:
		"""Run an autonomous agent task."""
		logger.debug(f'Running agent task: {task}')

		# Get LLM config
		llm_config = get_default_llm(self.config)

		# Get LLM provider
		model_provider = llm_config.get('model_provider') or os.getenv('MODEL_PROVIDER')

		# Get Bedrock-specific config
		if model_provider and model_provider.lower() == 'bedrock':
			llm_model = llm_config.get('model') or os.getenv('MODEL') or 'us.anthropic.claude-sonnet-4-6'
			aws_region = llm_config.get('region') or os.getenv('REGION')
			if not aws_region:
				aws_region = 'us-east-1'
			aws_sso_auth = llm_config.get('aws_sso_auth', False)
			llm = ChatAWSBedrock(
				model=llm_model,  # or any Bedrock model
				aws_region=aws_region,
				aws_sso_auth=aws_sso_auth,
			)
		else:
			api_key = llm_config.get('api_key') or os.getenv('OPENAI_API_KEY')
			if not api_key:
				return 'Error: OPENAI_API_KEY not set in config or environment'

			# Use explicit model from tool call, otherwise fall back to configured default
			llm_model = model or llm_config.get('model', 'gpt-4o')

			base_url = llm_config.get('base_url', None)
			kwargs = {}
			if base_url:
				kwargs['base_url'] = base_url
			llm = ChatOpenAI(
				model=llm_model,
				api_key=api_key,
				temperature=llm_config.get('temperature', 0.7),
				**kwargs,
			)

		# Get profile config and merge with tool parameters
		profile_config = get_default_profile(self.config)

		# Override allowed_domains only when the client supplied a non-empty list.
		# Treating an empty list as an override would silently disable any
		# admin-configured allowlist on the default profile, since
		# SecurityWatchdog interprets allowed_domains=[] as "no restrictions".
		if allowed_domains:
			profile_config['allowed_domains'] = allowed_domains

		# Create browser profile using config
		profile = BrowserProfile(**profile_config)

		# Create and run agent
		agent = Agent(
			task=task,
			llm=llm,
			browser_profile=profile,
			use_vision=use_vision,
		)

		try:
			history = await agent.run(max_steps=max_steps)

			# Format results
			results = []
			results.append(f'Task completed in {len(history.history)} steps')
			results.append(f'Success: {history.is_successful()}')

			# Get final result if available
			final_result = history.final_result()
			if final_result:
				results.append(f'\nFinal result:\n{final_result}')

			# Include any errors
			errors = history.errors()
			if errors:
				results.append(f'\nErrors encountered:\n{json.dumps(errors, indent=2)}')

			# Include URLs visited
			urls = history.urls()
			if urls:
				# Filter out None values and convert to strings
				valid_urls = [str(url) for url in urls if url is not None]
				if valid_urls:
					results.append(f'\nURLs visited: {", ".join(valid_urls)}')

			return '\n'.join(results)

		except Exception as e:
			logger.error(f'Agent task failed: {e}', exc_info=True)
			return f'Agent task failed: {str(e)}'
		finally:
			# Clean up
			await agent.close()

	def _network_tool_entries(self, prefix: str) -> list[types.Tool]:
		"""The two tools an agent (or a UI toggle) uses to choose the route."""
		from browser_use.net.tools import network_tools

		return network_tools(prefix)

	async def _network_set(self, arguments: dict[str, Any]) -> str:
		"""Change the route; if it changed, drop the browser so the next call starts on the new one."""
		if self.cdp_url and arguments.get('mode') != 'off':
			return (
				'Error: this server is attached to a Chrome you run (--cdp-url), which keeps its own connection. '
				'Set a proxy in that browser instead, or start the server without --cdp-url.'
			)
		before = self.network.route
		try:
			summary = await self.network.set_network(
				arguments['mode'], arguments.get('exit_country'), arguments.get('reason', '')
			)
		except NetworkPolicyError as e:
			return f'Error: {e}'
		if self.network.route != before:
			await self._drop_browser_session()
			summary += ' The browser restarts on this route at the next call; open tabs were closed.'
		return summary

	async def _drop_browser_session(self) -> None:
		"""Close the current browser and its eyes so the next call launches fresh (new route, new proxy)."""
		eyes, self._eyes_instance = getattr(self, '_eyes_instance', None), None
		if eyes is not None:
			try:
				await eyes.close()
			except Exception:
				pass
		if self.browser_session is not None:
			await self._close_session(self.browser_session.id)
		self.browser_session = None
		self.tools = None

	async def _page_outcome(self) -> Outcome:
		"""Classify the page now in front of us: ok, geo_blocked or walled."""
		from browser_use.explore import walls

		assert self.browser_session is not None
		try:
			cdp = await self.browser_session.get_or_create_cdp_session(focus=False)
			r = await cdp.cdp_client.send.Runtime.evaluate(
				params={'expression': walls.PROBE_JS, 'returnByValue': True}, session_id=cdp.session_id
			)
			info = json.loads((r.get('result') or {}).get('value') or '{}')
		except Exception:
			return 'ok'
		return classify_navigation(**info) if info else 'ok'

	async def _navigate_routed(self, url: str, new_tab: bool = False, strict: bool = False) -> str:
		"""Navigate; in `auto`, retry once through Tor after a network failure or a geo-block.

		Returns a short note for the reply ('' when nothing notable happened). A bot wall is never
		retried. `strict` raises navigation errors; without it they stay as quiet as they always were here.
		"""
		from browser_use.browser.events import NavigateToUrlEvent

		assert self.browser_session is not None
		self.network.check_url(url)

		async def go() -> Exception | None:
			assert self.browser_session is not None
			try:
				event = self.browser_session.event_bus.dispatch(NavigateToUrlEvent(url=url, new_tab=new_tab))
				await event
				await event.event_result(raise_if_any=True, raise_if_none=False)
			except Exception as e:
				return e
			return None

		error = await go()
		note = ''
		if error is not None and PROXY_CA_LEARNED in str(error) and not self.cdp_url and self.bridge is None:
			# the proxy's CA was just learned from this refusal (the environment's own CA bundle trusts it): a browser
			# trusts it from its start, so start one and go again. Not censorship, so no Tor.
			await self._drop_browser_session()
			await self._init_browser_session()
			error = await go()
			note = " (the browser restarted to trust this network's proxy CA, learned from the refused page)"
		outcome = classify_navigation(str(error)) if error else await self._page_outcome()
		self.network.note_outcome(outcome)
		if self.network.wants_fallback(outcome) and not self.cdp_url:
			host = urlparse(url).hostname or url
			if await self.network.engage(f'{outcome} at {host}'):
				await self._drop_browser_session()
				await self._init_browser_session()
				self.network.check_url(url)
				error = await go()
				self.network.note_outcome('ok' if error is None else 'network_error')
				note = f' (retried through Tor, exit {(self.network.exit_country or "any").upper()}, after a {outcome.replace("_", " ")})'
			elif self.network.last_error:
				note = f' Tor fallback unavailable: {self.network.last_error}'
		if error is not None and NOT_ALLOWED in str(error):
			# the bridge refused the site before the navigation was sent; the person is being asked
			raise Refused(str(error)) from error
		if error is not None and strict:
			raise RuntimeError(f'{error}{note}') from error
		return note

	async def _navigate(self, url: str, new_tab: bool = False) -> str:
		"""Navigate to a URL."""
		if not self.browser_session:
			return 'Error: No browser session active'

		# Update session activity
		self._update_session_activity(self.browser_session.id)

		note = await self._navigate_routed(url, new_tab, strict=self.bridge is not None)  # quiet only off the bridge
		opened = (f'Opened new tab with URL: {url}' if new_tab else f'Navigated to: {url}') + note

		# The tool surface belongs to the page, so it changes when the page does.
		await self._refresh_site_tools()
		if self._site_tools:
			opened += f'\n{len(self._site_tools)} tool(s) on this site: ' + ', '.join(
				f'{SITE_TOOL_PREFIX}{name}' for name in sorted(self._site_tools)
			)
		return opened

	async def _click(
		self,
		index: int | None = None,
		coordinate_x: int | None = None,
		coordinate_y: int | None = None,
		new_tab: bool = False,
	) -> str:
		"""Click an element by index or at viewport coordinates."""
		if not self.browser_session:
			return 'Error: No browser session active'

		# Update session activity
		self._update_session_activity(self.browser_session.id)

		# Coordinate-based clicking
		if coordinate_x is not None and coordinate_y is not None:
			from browser_use.browser.events import ClickCoordinateEvent

			event = self.browser_session.event_bus.dispatch(
				ClickCoordinateEvent(coordinate_x=coordinate_x, coordinate_y=coordinate_y)
			)
			await event
			await event.event_result(raise_if_any=True, raise_if_none=False)  # a failed click is not "Clicked"
			return f'Clicked at coordinates ({coordinate_x}, {coordinate_y})'

		# Index-based clicking
		if index is None:
			return 'Error: Provide either index or both coordinate_x and coordinate_y'

		# Get the element
		element = await self.browser_session.get_dom_element_by_index(index)
		if not element:
			return f'Element with index {index} not found'

		if new_tab:
			# For links, extract href and open in new tab
			href = element.attributes.get('href')
			if href:
				# Convert relative href to absolute URL
				state = await self.browser_session.get_browser_state_summary()
				current_url = state.url
				if href.startswith('/'):
					# Relative URL - construct full URL
					from urllib.parse import urlparse

					parsed = urlparse(current_url)
					full_url = f'{parsed.scheme}://{parsed.netloc}{href}'
				else:
					full_url = href

				# Open link in new tab
				from browser_use.browser.events import NavigateToUrlEvent

				event = self.browser_session.event_bus.dispatch(NavigateToUrlEvent(url=full_url, new_tab=True))
				await event
				return f'Clicked element {index} and opened in new tab {full_url[:20]}...'
			else:
				# For non-link elements, just do a normal click
				from browser_use.browser.events import ClickElementEvent

				event = self.browser_session.event_bus.dispatch(ClickElementEvent(node=element))
				await event
				await event.event_result(raise_if_any=True, raise_if_none=False)
				return f'Clicked element {index} (new tab not supported for non-link elements)'
		else:
			# Normal click
			from browser_use.browser.events import ClickElementEvent

			event = self.browser_session.event_bus.dispatch(ClickElementEvent(node=element))
			await event
			await event.event_result(raise_if_any=True, raise_if_none=False)
			return f'Clicked element {index}'

	async def _type_text(self, index: int, text: str) -> str:
		"""Type text into an element."""
		if not self.browser_session:
			return 'Error: No browser session active'

		element = await self.browser_session.get_dom_element_by_index(index)
		if not element:
			return f'Element with index {index} not found'

		from browser_use.browser.events import TypeTextEvent

		if self.bridge is not None and _is_secret_field(element.attributes or {}):
			return (
				"Refused: that is a password, card or one-time-code field in the person's own browser, and they enter "
				'those themselves. Ask them to fill it in, then carry on.'
			)

		# Conservative heuristic to detect potentially sensitive data
		# Only flag very obvious patterns to minimize false positives
		is_potentially_sensitive = len(text) >= 6 and (
			# Email pattern: contains @ and a domain-like suffix
			('@' in text and '.' in text.split('@')[-1] if '@' in text else False)
			# Mixed alphanumeric with reasonable complexity (likely API keys/tokens)
			or (
				len(text) >= 16
				and any(char.isdigit() for char in text)
				and any(char.isalpha() for char in text)
				and any(char in '.-_' for char in text)
			)
		)

		# Use generic key names to avoid information leakage about detection patterns
		sensitive_key_name = None
		if is_potentially_sensitive:
			if '@' in text and '.' in text.split('@')[-1]:
				sensitive_key_name = 'email'
			else:
				sensitive_key_name = 'credential'

		event = self.browser_session.event_bus.dispatch(
			TypeTextEvent(node=element, text=text, is_sensitive=is_potentially_sensitive, sensitive_key_name=sensitive_key_name)
		)
		await event

		if is_potentially_sensitive:
			if sensitive_key_name:
				return f'Typed <{sensitive_key_name}> into element {index}'
			else:
				return f'Typed <sensitive> into element {index}'
		else:
			return f"Typed '{text}' into element {index}"

	async def _get_browser_state(self, include_screenshot: bool = False) -> tuple[str, str | None]:
		"""Get current browser state. Returns (state_json, screenshot_b64 | None)."""
		if not self.browser_session:
			return 'Error: No browser session active', None

		state = await self.browser_session.get_browser_state_summary()

		result: dict[str, Any] = {
			'url': state.url,
			'title': state.title,
			'tabs': [{'url': tab.url, 'title': tab.title} for tab in state.tabs],
			'interactive_elements': [],
		}

		# Add viewport info so the LLM knows the coordinate space
		if state.page_info:
			pi = state.page_info
			result['viewport'] = {
				'width': pi.viewport_width,
				'height': pi.viewport_height,
			}
			result['page'] = {
				'width': pi.page_width,
				'height': pi.page_height,
			}
			result['scroll'] = {
				'x': pi.scroll_x,
				'y': pi.scroll_y,
			}

		# Add interactive elements with their indices
		for index, element in state.dom_state.selector_map.items():
			elem_info: dict[str, Any] = {
				'index': index,
				'tag': element.tag_name,
				'text': element.get_all_children_text(max_depth=2)[:100],
			}
			if element.attributes.get('placeholder'):
				elem_info['placeholder'] = element.attributes['placeholder']
			if element.attributes.get('href'):
				elem_info['href'] = element.attributes['href']
			result['interactive_elements'].append(elem_info)

		# Return screenshot separately as ImageContent instead of embedding base64 in JSON
		screenshot_b64 = None
		if include_screenshot and state.screenshot:
			screenshot_b64 = state.screenshot
			# Include viewport dimensions in JSON so LLM can map pixels to coordinates
			if state.page_info:
				result['screenshot_dimensions'] = {
					'width': state.page_info.viewport_width,
					'height': state.page_info.viewport_height,
				}

		return json.dumps(result, indent=2), screenshot_b64

	async def _run_script(self, script: str) -> str:
		"""Run agent-authored JavaScript against the page and return its result."""
		if not self.browser_session:
			return 'Error: No browser session active'
		self._update_session_activity(self.browser_session.id)

		result = await self.browser_session.run_page_script(script)
		if not result.ok:
			# Verbatim: the client's next move is to rewrite the script, which it can only
			# do from the real error.
			return f'Script failed: {result.error or "unknown error"}'
		body = result.value or 'null'
		if result.truncated:
			body += f'\n[truncated: {len(result.value)} of {result.full_length} chars. Return fewer fields, or slice the list.]'
		return body

	def _site_tool_entries(self) -> list[types.Tool]:
		"""The current page's tools, namespaced so they cannot collide with ours.

		Built from a snapshot refreshed on navigation rather than scanned here: tools/list
		is called often and synchronously, and a client should never wait on a page scan to
		find out what it can do.
		"""
		entries: list[types.Tool] = []
		for tool in self._site_tools.values():
			provenance = (
				'published by this site'
				if tool.source != 'synthesized'
				else (
					'worked out from the page and previously run successfully'
					if tool.verified
					else 'worked out from the page, not yet run'
				)
			)
			entries.append(
				types.Tool(
					name=f'{SITE_TOOL_PREFIX}{tool.name}',
					description=f'{tool.description or tool.name} ({provenance})',
					input_schema=tool.input_schema or {'type': 'object', 'properties': {}},
				)
			)
		return entries

	async def _refresh_site_tools(self) -> None:
		"""Re-read what the current page offers. Never raises: this is a convenience."""
		self._site_tools = {}
		self._site_tools_url = ''
		if not self.browser_session:
			return
		try:
			page_tools = await self.browser_session.get_webmcp_tools()
		except Exception as e:
			logger.debug(f'Could not refresh site tools: {type(e).__name__}: {e}')
			return
		self._site_tools_url = page_tools.url
		self._site_tools = {tool.name: tool for tool in page_tools.tools}
		if self._site_tools:
			logger.debug(f'{len(self._site_tools)} site tool(s) now advertised: {", ".join(self._site_tools)}')

	async def _refresh_site_tools_if_moved(self) -> None:
		"""Re-scan only when the browser is somewhere other than where the snapshot is from."""
		if not self.browser_session:
			return
		try:
			current = await self.browser_session.get_current_page_url()
		except Exception:
			return
		if current != self._site_tools_url:
			await self._refresh_site_tools()

	async def _call_site_tool(self, tool_name: str, arguments: dict) -> str:
		"""Invoke one of the current page's tools."""
		if not self.browser_session:
			return 'Error: No browser session active'
		# A click, a form submit, or another site tool can navigate, and only _navigate used
		# to refresh. Without this the client is calling a tool from the previous page.
		await self._refresh_site_tools_if_moved()
		name = tool_name[len(SITE_TOOL_PREFIX) :]
		if name not in self._site_tools:
			known = ', '.join(sorted(self._site_tools)) or 'none on this page'
			return f'Error: "{name}" is not a tool on the current page. Available: {known}'

		result = await self.browser_session.call_webmcp_tool(name, arguments or {})
		if not result.ok:
			return f'Site tool "{name}" failed: {result.error or "unknown error"}'
		return result.content or '(the tool succeeded and returned no content)'

	async def _list_page_tools(self) -> str:
		"""List WebMCP tools the current page declares."""
		if not self.browser_session:
			return 'Error: No browser session active'
		self._update_session_activity(self.browser_session.id)

		page_tools = await self.browser_session.get_webmcp_tools()
		if not page_tools.tools:
			return 'This page declares no WebMCP tools. Drive it through the UI instead.'
		return json.dumps(
			[
				{
					'name': tool.name,
					'description': tool.description,
					'input_schema': tool.input_schema,
					# 'js'/'manifest' means the site published it. 'synthesized' means it was
					# worked out from the page, and `verified` says whether it has ever run.
					'source': tool.source,
					'verified': tool.verified,
				}
				for tool in page_tools.tools
			],
			indent=1,
		)

	async def _call_page_tool(self, name: str, arguments: str) -> str:
		"""Invoke a tool the current page declared."""
		if not self.browser_session:
			return 'Error: No browser session active'
		self._update_session_activity(self.browser_session.id)

		try:
			parsed = json.loads(arguments) if arguments.strip() else {}
		except json.JSONDecodeError as e:
			return f'Error: arguments must be a JSON object string ({e.msg})'
		if not isinstance(parsed, dict):
			return 'Error: arguments must be a JSON object'

		result = await self.browser_session.call_webmcp_tool(name, parsed)
		if not result.ok:
			return f'Page tool "{name}" failed: {result.error or "unknown error"}'
		return result.content or '(the tool succeeded and returned no content)'

	async def _get_html(self, selector: str | None = None) -> str:
		"""Get raw HTML of the page or a specific element."""
		if not self.browser_session:
			return 'Error: No browser session active'

		self._update_session_activity(self.browser_session.id)

		cdp_session = await self.browser_session.get_or_create_cdp_session(target_id=None, focus=False)
		if not cdp_session:
			return 'Error: No active CDP session'

		if selector:
			js = (
				f'(function(){{ const el = document.querySelector({json.dumps(selector)}); return el ? el.outerHTML : null; }})()'
			)
		else:
			js = 'document.documentElement.outerHTML'

		result = await cdp_session.cdp_client.send.Runtime.evaluate(
			params={'expression': js, 'returnByValue': True},
			session_id=cdp_session.session_id,
		)
		html = result.get('result', {}).get('value')
		if html is None:
			return f'No element found for selector: {selector}' if selector else 'Error: Could not get page HTML'
		return html

	async def _screenshot(self, full_page: bool = False) -> tuple[str, str | None]:
		"""Take a screenshot. Returns (metadata_json, screenshot_b64 | None)."""
		if not self.browser_session:
			return 'Error: No browser session active', None

		import base64

		self._update_session_activity(self.browser_session.id)

		data = await self.browser_session.take_screenshot(full_page=full_page)
		b64 = base64.b64encode(data).decode()

		# Return screenshot separately as ImageContent instead of embedding base64 in JSON
		state = await self.browser_session.get_browser_state_summary()
		result: dict[str, Any] = {
			'size_bytes': len(data),
		}
		if state.page_info:
			result['viewport'] = {
				'width': state.page_info.viewport_width,
				'height': state.page_info.viewport_height,
			}
		return json.dumps(result), b64

	async def _extract_content(self, query: str, extract_links: bool = False) -> str:
		"""Extract content from current page."""
		if not self.llm:
			return 'Error: LLM not initialized (set OPENAI_API_KEY)'

		if not self.file_system:
			return 'Error: FileSystem not initialized'

		if not self.browser_session:
			return 'Error: No browser session active'

		if not self.tools:
			return 'Error: Tools not initialized'

		state = await self.browser_session.get_browser_state_summary()

		# Use the extract action
		# Create a dynamic action model that matches the tools's expectations
		from pydantic import create_model

		# Create action model dynamically
		ExtractAction = create_model(
			'ExtractAction',
			__base__=ActionModel,
			extract=dict[str, Any],
		)

		# Use model_validate because Pyright does not understand the dynamic model
		action = ExtractAction.model_validate(
			{
				'extract': {'query': query, 'extract_links': extract_links},
			}
		)
		action_result = await self.tools.act(
			action=action,
			browser_session=self.browser_session,
			page_extraction_llm=self.llm,
			file_system=self.file_system,
		)

		return action_result.extracted_content or 'No content extracted'

	async def _scroll(self, direction: str = 'down') -> str:
		"""Scroll the page."""
		if not self.browser_session:
			return 'Error: No browser session active'

		from browser_use.browser.events import ScrollEvent

		# Scroll by a standard amount (500 pixels)
		event = self.browser_session.event_bus.dispatch(
			ScrollEvent(
				direction=direction,  # type: ignore
				amount=500,
			)
		)
		await event
		return f'Scrolled {direction}'

	async def _go_back(self) -> str:
		"""Go back in browser history."""
		if not self.browser_session:
			return 'Error: No browser session active'

		from browser_use.browser.events import GoBackEvent

		event = self.browser_session.event_bus.dispatch(GoBackEvent())
		await event
		return 'Navigated back'

	async def _close_browser(self) -> str:
		"""Close the browser session."""
		if self.browser_session:
			from browser_use.browser.events import BrowserStopEvent

			event = self.browser_session.event_bus.dispatch(BrowserStopEvent())
			await event
			self.browser_session = None
			self.tools = None
			return 'Browser closed'
		return 'No browser session to close'

	async def _list_tabs(self) -> str:
		"""List all open tabs."""
		if not self.browser_session:
			return 'Error: No browser session active'

		tabs_info = await self.browser_session.get_tabs()
		tabs = []
		for i, tab in enumerate(tabs_info):
			tabs.append({'tab_id': tab.target_id[-4:], 'url': tab.url, 'title': tab.title or ''})
		return json.dumps(tabs, indent=2)

	async def _switch_tab(self, tab_id: str) -> str:
		"""Switch to a different tab."""
		if not self.browser_session:
			return 'Error: No browser session active'

		from browser_use.browser.events import SwitchTabEvent

		target_id = await self.browser_session.get_target_id_from_tab_id(tab_id)
		event = self.browser_session.event_bus.dispatch(SwitchTabEvent(target_id=target_id))
		await event
		state = await self.browser_session.get_browser_state_summary()
		return f'Switched to tab {tab_id}: {state.url}'

	async def _close_tab(self, tab_id: str) -> str:
		"""Close a specific tab."""
		if not self.browser_session:
			return 'Error: No browser session active'

		from browser_use.browser.events import CloseTabEvent

		target_id = await self.browser_session.get_target_id_from_tab_id(tab_id)
		event = self.browser_session.event_bus.dispatch(CloseTabEvent(target_id=target_id))
		await event
		current_url = await self.browser_session.get_current_page_url()
		return f'Closed tab # {tab_id}, now on {current_url}'

	def _track_session(self, session: BrowserSession) -> None:
		"""Track a browser session for management."""
		self.active_sessions[session.id] = {
			'session': session,
			'created_at': time.time(),
			'last_activity': time.time(),
			'url': getattr(session, 'current_url', None),
		}

	def _update_session_activity(self, session_id: str) -> None:
		"""Update the last activity time for a session."""
		if session_id in self.active_sessions:
			self.active_sessions[session_id]['last_activity'] = time.time()

	async def _list_sessions(self) -> str:
		"""List all active browser sessions."""
		if not self.active_sessions:
			return 'No active browser sessions'

		sessions_info = []
		for session_id, session_data in self.active_sessions.items():
			session = session_data['session']
			created_at = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(session_data['created_at']))
			last_activity = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(session_data['last_activity']))

			# Check if session is still active
			is_active = hasattr(session, 'cdp_client') and session.cdp_client is not None

			sessions_info.append(
				{
					'session_id': session_id,
					'created_at': created_at,
					'last_activity': last_activity,
					'active': is_active,
					'current_url': session_data.get('url', 'Unknown'),
					'age_minutes': (time.time() - session_data['created_at']) / 60,
				}
			)

		return json.dumps(sessions_info, indent=2)

	async def _close_session(self, session_id: str) -> str:
		"""Close a specific browser session."""
		if session_id not in self.active_sessions:
			return f'Error: session {session_id} not found'

		session_data = self.active_sessions[session_id]
		session = session_data['session']

		# The eyes on this browser (and their archiver) go with it, or they outlive it.
		eyes = getattr(self, '_eyes_instance', None)
		if eyes is not None and eyes.browser_session is session:
			self._eyes_instance = None
			try:
				await eyes.close()
			except Exception:
				pass

		try:
			# Close the session
			if hasattr(session, 'kill'):
				await session.kill()
			elif hasattr(session, 'close'):
				await session.close()

			# Remove from tracking
			del self.active_sessions[session_id]

			# If this was the current session, clear it
			if self.browser_session and self.browser_session.id == session_id:
				self.browser_session = None
				self.tools = None

			return f'Successfully closed session {session_id}'
		except Exception as e:
			return f'Error closing session {session_id}: {str(e)}'

	async def _close_all_sessions(self) -> str:
		"""Close all active browser sessions."""
		if not self.active_sessions:
			return 'No active sessions to close'

		closed_count = 0
		errors = []

		for session_id in list(self.active_sessions.keys()):
			try:
				result = await self._close_session(session_id)
				if 'Successfully closed' in result:
					closed_count += 1
				else:
					errors.append(f'{session_id}: {result}')
			except Exception as e:
				errors.append(f'{session_id}: {str(e)}')

		# Clear current session references
		self.browser_session = None
		self.tools = None

		result = f'Closed {closed_count} sessions'
		if errors:
			result += f'. Errors: {"; ".join(errors)}'

		return result

	async def _cleanup_expired_sessions(self) -> None:
		"""Background task to clean up expired sessions."""
		current_time = time.time()
		timeout_seconds = self.session_timeout_minutes * 60

		expired_sessions = []
		for session_id, session_data in self.active_sessions.items():
			last_activity = session_data['last_activity']
			if current_time - last_activity > timeout_seconds:
				expired_sessions.append(session_id)

		for session_id in expired_sessions:
			try:
				await self._close_session(session_id)
				logger.info(f'Auto-closed expired session {session_id}')
			except Exception as e:
				logger.error(f'Error auto-closing session {session_id}: {e}')

	async def _start_cleanup_task(self) -> None:
		"""Start the background cleanup task."""

		async def cleanup_loop():
			while True:
				try:
					await self._cleanup_expired_sessions()
					# Check every 2 minutes
					await asyncio.sleep(120)
				except Exception as e:
					logger.error(f'Error in cleanup task: {e}')
					await asyncio.sleep(120)

		self._cleanup_task = create_task_with_error_handling(cleanup_loop(), name='mcp_cleanup_loop', suppress_exceptions=True)

	def _capture_started(self) -> None:
		self._telemetry.capture(
			MCPServerTelemetryEvent(
				version=get_browser_use_version(),
				action='start',
				parent_process_cmdline=get_parent_process_cmdline(),
			)
		)

	def _capture_stopped(self) -> None:
		self._telemetry.capture(
			MCPServerTelemetryEvent(
				version=get_browser_use_version(),
				action='stop',
				duration_seconds=time.time() - self._start_time,
				parent_process_cmdline=get_parent_process_cmdline(),
			)
		)
		self._telemetry.flush()

	async def run(self):
		"""Run the MCP server."""
		# Start the cleanup task
		await self._start_cleanup_task()

		if sys.stdin is None:
			raise RuntimeError('MCP stdio transport requires stdin, but this process was launched without one.')

		async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
			try:
				await self.server.run(
					read_stream,
					write_stream,
					self.server.create_initialization_options(),
				)
			except BrokenPipeError:
				logger.warning('MCP client disconnected while writing to stdio; shutting down server cleanly.')


def _is_secret_field(attributes: dict[str, str]) -> bool:
	"""Password, card-number, card-code and one-time-code inputs: the person fills these in themselves."""
	autocomplete = attributes.get('autocomplete', '')
	return attributes.get('type') == 'password' or any(
		token in autocomplete for token in ('password', 'cc-number', 'cc-csc', 'one-time-code')
	)


from browser_use.serving import _close_everything, _log_stopped_by_signal, serve_until_stopped  # noqa: E402,F401


async def main(session_timeout_minutes: int = 10):
	"""Serve with everything loaded first. `python -m browser_use.mcp` and `browser-use --mcp` serve through the front
	(`mcp/front.py`) instead, which answers the client while this module loads."""
	if not MCP_AVAILABLE:
		print('MCP SDK is required. Install with: pip install mcp', file=sys.stderr)
		sys.exit(1)

	server = BrowserUseServer(session_timeout_minutes=session_timeout_minutes)
	if os.environ.get('BROWSER_USE_BRIDGE'):
		from browser_use.bridge import EXTENSION_DIR, BridgeRelay

		server.bridge = await BridgeRelay(port=int(os.environ['BROWSER_USE_BRIDGE'])).start()
		server.cdp_url = server.bridge.cdp_url
		print(f'Bridge on {server.cdp_url}; load the extension from {EXTENSION_DIR} and share a tab.', file=sys.stderr)
	server._capture_started()
	try:
		await serve_until_stopped(server)
	finally:
		server._capture_stopped()


if __name__ == '__main__':
	asyncio.run(main())
