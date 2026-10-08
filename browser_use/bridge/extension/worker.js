// Retinat bridge: relays the Chrome DevTools Protocol between the local relay and the tabs the person shares.
// Runs as an MV3 service worker (Chromium 88+) or, with manifest.v2.json, as an MV2 background page.
// It never hides itself: Chromium shows its "started debugging this browser" bar while a tab is shared,
// and pressing Cancel on that bar unshares everything.
'use strict';

const C = globalThis.chrome;
const action = C.action || C.browserAction;
const store = C.storage.session || C.storage.local;

// The methods that only look (policy.json, shared with the relay). While the person holds the wheel, or after
// Cancel, anything else is refused, page script included. Until the list is loaded nothing counts as looking.
let PASSIVE = [];

function acts(method) {
	return !PASSIVE.some((re) => re.test(method));
}

const state = {
	// resumeAfterMs: after the person's last input in a shared tab, how long until the AI may carry on (0: never)
	settings: { relay: 'ws://127.0.0.1:9333/extension', alwaysShare: [], resumeAfterMs: 8000 },
	shared: new Set(), // tab ids the person shared, or the AI opened
	attached: new Set(), // tab ids with a live chrome.debugger session
	targets: new Map(), // tab id -> DevTools target id
	holder: 'agent', // who drives shared tabs: 'agent' or 'human'
	autoHeld: false, // the person took the wheel just by using a shared tab (resumes on its own)
	stopped: false, // the person pressed Cancel on the debugging bar: nothing until they share a tab again
	personAt: 0, // when the person last used a shared tab
	aiInputAt: new Map(), // tab id -> when the AI last sent input there
	aiWindow: null,
	ws: null,
	backoff: 500,
	ready: null,
};

function call(ns, name, ...args) {
	return new Promise((resolve, reject) => {
		ns[name](...args, (result) => {
			const err = C.runtime.lastError;
			if (err) reject(new Error(err.message));
			else resolve(result);
		});
	});
}

function globToRegExp(glob) {
	return new RegExp('^' + glob.split('*').map((s) => s.replace(/[.+?^${}()|[\]\\]/g, '\\$&')).join('.*') + '$');
}

function alwaysShared(url) {
	return !!url && state.settings.alwaysShare.some((g) => globToRegExp(g).test(url));
}

function emit(msg) {
	if (state.ws && state.ws.readyState === 1) state.ws.send(JSON.stringify(msg));
}

async function save() {
	await call(store, 'set', { shared: [...state.shared], holder: state.holder, aiWindow: state.aiWindow, stopped: state.stopped });
}

const STOPPED = 'the person pressed Cancel on the debugging bar, which stops the AI; ask them to share a tab again';

async function targetInfo(tabId) {
	const targets = await call(C.debugger, 'getTargets');
	const t = targets.find((x) => x.tabId === tabId);
	if (!t) throw new Error(`tab ${tabId} has no page target`);
	state.targets.set(tabId, t.id);
	return {
		targetId: t.id,
		tabId,
		type: 'page',
		title: t.title,
		url: t.url,
		attached: true,
		canAccessOpener: false,
		browserContextId: 'default',
	};
}

async function badge(tabId) {
	const on = state.shared.has(tabId);
	const human = state.holder === 'human';
	try {
		await call(action, 'setBadgeText', { tabId, text: on ? (human ? 'YOU' : 'AI') : '' });
		if (on) await call(action, 'setBadgeBackgroundColor', { tabId, color: human ? '#b26a00' : '#1a7f37' });
	} catch (e) {
		// the tab may already be gone
	}
}

async function share(tabId, why) {
	const resuming = state.stopped && why === 'shared by the person'; // sharing again is the person's go-ahead
	if (state.stopped && !resuming) throw new Error(STOPPED);
	if (resuming) {
		state.stopped = false;
		await setHolder('agent', 'the person shared a tab again');
	}
	if (state.shared.has(tabId)) return;
	state.shared.add(tabId);
	await save();
	badge(tabId);
	try {
		emit({ event: 'shared', why, tab: await targetInfo(tabId) });
	} catch (e) {
		state.shared.delete(tabId); // chrome:// and other extensions' pages cannot be shared
		await save();
		badge(tabId);
		throw e;
	}
}

async function unshare(tabId, why) {
	if (!state.shared.delete(tabId)) return;
	if (state.attached.delete(tabId)) await call(C.debugger, 'detach', { tabId }).catch(() => {});
	await save();
	badge(tabId);
	emit({ event: 'unshared', why, tabId, targetId: state.targets.get(tabId) });
}

async function ensureAttached(tabId) {
	if (state.attached.has(tabId)) return;
	try {
		await call(C.debugger, 'attach', { tabId }, '1.3');
	} catch (e) {
		// After a service-worker restart our own earlier session may still be live.
		const t = (await call(C.debugger, 'getTargets')).find((x) => x.tabId === tabId);
		if (!(t && t.attached && t.extensionId === C.runtime.id)) throw e;
	}
	state.attached.add(tabId);
}

async function openForAgent(url) {
	let tab = null;
	if (state.aiWindow !== null) {
		tab = await call(C.tabs, 'create', { windowId: state.aiWindow, url, active: true }).catch(() => null);
	}
	if (!tab) {
		// The AI works in its own window so the person keeps theirs; it stays visible so pages render at full rate.
		const win = await call(C.windows, 'create', { url, focused: false, type: 'normal' });
		state.aiWindow = win.id;
		tab = win.tabs[0];
	}
	await share(tab.id, 'opened by the AI');
	return await targetInfo(tab.id);
}

async function bringToFront(tabId) {
	// A person switches to a tab before clicking in it; hidden tabs get no input. The window keeps its focus state.
	const tab = await call(C.tabs, 'get', tabId);
	if (tab.active) return;
	await call(C.tabs, 'update', tabId, { active: true });
	for (let i = 0; i < 40; i++) {
		const r = await call(C.debugger, 'sendCommand', { tabId }, 'Runtime.evaluate', { expression: 'document.visibilityState', returnByValue: true });
		if (r.result.value === 'visible') return;
		await new Promise((res) => setTimeout(res, 25));
	}
}

function needShared(tabId) {
	if (!state.shared.has(tabId)) throw new Error('that tab is not shared with the AI');
}

async function handle(msg) {
	if (state.stopped && msg.op !== 'ping' && msg.op !== 'tabs') throw new Error(STOPPED);
	switch (msg.op) {
		case 'ping':
			return {};
		case 'tabs': {
			const infos = await Promise.all([...state.shared].map((id) => targetInfo(id).catch(() => null)));
			return { tabs: infos.filter(Boolean), holder: state.holder };
		}
		case 'send': {
			needShared(msg.tabId);
			if (state.holder === 'human' && acts(msg.method)) throw new Error('the person is using the browser right now');
			await ensureAttached(msg.tabId);
			const input = msg.method.startsWith('Input.');
			if (input && !msg.sessionId) await bringToFront(msg.tabId);
			const target = msg.sessionId ? { tabId: msg.tabId, sessionId: msg.sessionId } : { tabId: msg.tabId };
			if (input) state.aiInputAt.set(msg.tabId, Date.now());
			try {
				return (await call(C.debugger, 'sendCommand', target, msg.method, msg.params || {})) || {};
			} finally {
				if (input) state.aiInputAt.set(msg.tabId, Date.now());
			}
		}
		case 'open':
			return await openForAgent(msg.url || 'about:blank');
		case 'close':
			needShared(msg.tabId);
			await call(C.tabs, 'remove', msg.tabId);
			return {};
		case 'activate':
			needShared(msg.tabId);
			await call(C.tabs, 'update', msg.tabId, { active: true });
			return {};
		default:
			throw new Error(`unknown op ${msg.op}`);
	}
}

async function onRelay(msg) {
	try {
		emit({ id: msg.id, result: await handle(msg) });
	} catch (e) {
		emit({ id: msg.id, error: String((e && e.message) || e) });
	}
}

function connect() {
	if (state.ws && state.ws.readyState <= 1) return;
	let ws;
	try {
		ws = new WebSocket(state.settings.relay);
	} catch (e) {
		return retry();
	}
	state.ws = ws;
	ws.onopen = async () => {
		state.backoff = 500;
		const m = C.runtime.getManifest();
		emit({
			event: 'hello',
			userAgent: navigator.userAgent,
			extension: m.version,
			manifest: m.manifest_version,
			holder: state.holder,
			stopped: state.stopped,
		});
		for (const tabId of state.shared) {
			targetInfo(tabId)
				.then((tab) => emit({ event: 'shared', why: 'already shared', tab }))
				.catch(() => unshare(tabId, 'gone'));
		}
	};
	ws.onmessage = (m) => onRelay(JSON.parse(m.data));
	ws.onclose = () => {
		if (state.ws === ws) {
			state.ws = null;
			// No AI on the other end: let go of the tabs so the debugging bar goes away until it comes back.
			for (const tabId of state.attached) call(C.debugger, 'detach', { tabId }).catch(() => {});
			state.attached.clear();
			retry();
		}
	};
	ws.onerror = () => {};
}

function retry() {
	setTimeout(connect, state.backoff);
	state.backoff = Math.min(state.backoff * 2, 10000);
}

async function setHolder(holder, why = 'set by the person') {
	state.holder = holder;
	state.autoHeld = false;
	await save();
	emit({ event: 'control', holder, why });
	for (const tabId of state.shared) badge(tabId);
}

// Input the AI did not send is the person's: their clicks, keys and wheel turns are trusted events too,
// so the only way to tell them apart is that the AI's own input went through this worker moments before.
const AI_ECHO_MS = 600;

async function onPersonInput(tabId, type) {
	if (Date.now() - (state.aiInputAt.get(tabId) || 0) < AI_ECHO_MS) return;
	state.personAt = Date.now();
	if (state.holder !== 'agent') return;
	await setHolder('human', `the person used a shared tab (${type})`);
	state.autoHeld = true;
	if (state.settings.resumeAfterMs > 0) setTimeout(resumeWhenQuiet, state.settings.resumeAfterMs);
}

async function resumeWhenQuiet() {
	if (!state.autoHeld || state.stopped) return; // the person took or gave the wheel explicitly since
	const quiet = Date.now() - state.personAt;
	if (quiet >= state.settings.resumeAfterMs) await setHolder('agent', 'the person has been idle');
	else setTimeout(resumeWhenQuiet, state.settings.resumeAfterMs - quiet);
}

async function activeTab() {
	const [tab] = await call(C.tabs, 'query', { active: true, lastFocusedWindow: true });
	return tab;
}

async function status(tabId) {
	return {
		stopped: state.stopped,
		connected: !!(state.ws && state.ws.readyState === 1),
		relay: state.settings.relay,
		holder: state.holder,
		shared: state.shared.has(tabId),
		count: state.shared.size,
	};
}

async function boot() {
	try {
		const policy = await (await fetch(C.runtime.getURL('policy.json'))).json();
		PASSIVE = policy.passive.map(globToRegExp);
	} catch (e) {
		// no policy: nothing counts as looking, so a held wheel refuses everything
	}
	try {
		const packaged = await (await fetch(C.runtime.getURL('settings.json'))).json();
		Object.assign(state.settings, packaged);
	} catch (e) {
		// no packaged settings: keep the defaults
	}
	const local = await call(C.storage.local, 'get', 'settings');
	Object.assign(state.settings, local.settings || {});
	const kept = await call(store, 'get', ['shared', 'holder', 'aiWindow', 'stopped']);
	state.holder = kept.holder || 'agent';
	state.stopped = !!kept.stopped;
	state.aiWindow = kept.aiWindow ?? null;
	const tabs = await call(C.tabs, 'query', {});
	const live = new Set(tabs.map((t) => t.id));
	for (const id of kept.shared || []) if (live.has(id)) state.shared.add(id);
	if (!state.stopped) for (const t of tabs) if (alwaysShared(t.url)) state.shared.add(t.id);
	await save();
	for (const id of state.shared) badge(id);
	connect();
}

C.debugger.onEvent.addListener((source, method, params) => {
	if (state.shared.has(source.tabId)) emit({ event: 'cdp', tabId: source.tabId, sessionId: source.sessionId, method, params });
});

C.debugger.onDetach.addListener(async (source, reason) => {
	const tabId = source.tabId;
	state.attached.delete(tabId);
	if (reason === 'canceled_by_user') {
		// Cancel is the person's stop button, not just "this tab": unshare everything and open nothing new.
		if (state.stopped) return; // Chrome detaches every tab at once; the first one does the work
		state.stopped = true;
		state.autoHeld = false;
		for (const id of [...state.shared]) {
			// one tab failing to let go must not leave the others shared
			await unshare(id, 'the person pressed Cancel on the debugging bar').catch(() => state.shared.delete(id));
		}
		await save();
		state.holder = 'human';
		await save();
		emit({ event: 'control', holder: 'human', stopped: true, why: 'the person pressed Cancel on the debugging bar' });
		for (const id of state.shared) badge(id);
		return;
	}
	if (state.shared.has(tabId) && reason !== 'target_closed') {
		// e.g. DevTools opened on the tab: tell the relay so clients re-attach and re-enable their domains
		state.shared.delete(tabId);
		emit({ event: 'unshared', why: reason, tabId, targetId: state.targets.get(tabId) });
		share(tabId, 'reattached').catch(() => {});
	}
});

C.tabs.onRemoved.addListener((tabId) => unshare(tabId, 'tab closed'));

C.tabs.onCreated.addListener((tab) => {
	// A tab a shared page opens (target=_blank, window.open) is part of what the AI was doing.
	if (tab.openerTabId !== undefined && state.shared.has(tab.openerTabId)) share(tab.id, 'opened from a shared tab').catch(() => {});
});

C.tabs.onUpdated.addListener(async (tabId, change) => {
	if (state.shared.has(tabId)) {
		if (change.url || change.title || change.status === 'complete') {
			targetInfo(tabId).then((tab) => emit({ event: 'changed', tab })).catch(() => {});
		}
		badge(tabId);
	} else if (change.url && alwaysShared(change.url)) {
		share(tabId, 'always shared site').catch(() => {});
	}
});

C.windows.onRemoved.addListener((id) => {
	if (id === state.aiWindow) {
		state.aiWindow = null;
		save();
	}
});

C.runtime.onMessage.addListener((msg, sender, reply) => {
	if (msg.input) {
		if (sender.tab && state.shared.has(sender.tab.id)) onPersonInput(sender.tab.id, msg.input);
		return false;
	}
	(async () => {
		if (msg.ask === 'share') await share(msg.tabId, 'shared by the person');
		else if (msg.ask === 'unshare') await unshare(msg.tabId, 'unshared by the person');
		else if (msg.ask === 'holder') await setHolder(msg.holder);
		else if (msg.ask === 'relay') {
			state.settings.relay = msg.relay;
			await call(C.storage.local, 'set', { settings: { relay: msg.relay } });
			if (state.ws) state.ws.close();
			connect();
		}
		return status(msg.tabId);
	})().then(reply, (e) => reply({ error: String(e.message || e) }));
	return true;
});

if (C.commands) {
	C.commands.onCommand.addListener(async (command) => {
		if (command === 'toggle-control') return setHolder(state.holder === 'human' ? 'agent' : 'human');
		const tab = await activeTab();
		if (command === 'toggle-share' && tab) {
			if (state.shared.has(tab.id)) await unshare(tab.id, 'unshared by the person');
			else await share(tab.id, 'shared by the person').catch(() => {});
		}
	});
}

if (C.alarms) {
	C.alarms.create('reconnect', { periodInMinutes: 1 });
	C.alarms.onAlarm.addListener(() => connect());
}

state.ready = boot();
