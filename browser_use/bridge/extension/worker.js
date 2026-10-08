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
let POLICY = null; // the globs as loaded, reported in hello so `doctor` can spot a copy that differs from the relay's
// Phrases on controls that place an order, pay, delete an account or grant access (policy.json). The page's copy of
// watch.js holds such an activation while the AI acts, in the event itself, and the person is asked.
let CONSEQUENTIAL = [];

function acts(method) {
	return !PASSIVE.some((re) => re.test(method));
}

const state = {
	// resumeAfterMs: after the person's last input in a shared tab, how long until the AI may carry on (0: never)
	// alwaysAllow: sites (origin globs, like https://*.example.com) the person lets the AI use without asking
	settings: { relay: 'ws://127.0.0.1:9333/extension', alwaysShare: [], alwaysAllow: [], resumeAfterMs: 8000 },
	shared: new Set(), // tab ids the person shared, or the AI opened
	offered: new Set(), // tabs the person (or a page) opened from a shared tab: unshared until they press Share
	moved: new Map(), // tab id -> {site, byAi}: a shared tab that moved to a site not allowed, unshared and offered back
	urls: new Map(), // tab id -> the address a shared tab was last seen on
	allowed: new Set(), // sites (origins) the person let the AI use until the browser closes
	declined: new Set(), // sites the person said no to: asked about again only if they share a tab there
	asking: null, // {kind, origin, url, label, tabId, windowId}: the one ask waiting for the person
	dialogs: new Map(), // tab id -> the type of the page's open native dialog (alert, confirm, prompt, beforeunload)
	held: new Map(), // tab id -> the last activation the page held for the person: {id, label, origin}
	heldWaiters: new Map(), // arm id -> resolve, for a send waiting to hear whether its event was held
	armSeq: 0,
	clickGrants: [], // {tabId, origin, label, until}: one click the person allowed
	clickDeclined: new Set(), // origin + '|' + label the person said no to, until the browser closes
	children: new Map(), // child session id -> {tabId, targetId, url}: frames of other sites and workers in shared tabs
	attached: new Set(), // tab ids with a live chrome.debugger session
	targets: new Map(), // tab id -> DevTools target id
	holder: 'agent', // who drives shared tabs: 'agent' or 'human'
	autoHeld: false, // the person took the wheel just by using a shared tab (resumes on its own)
	stopped: false, // the person pressed Cancel on the debugging bar: nothing until they share a tab again
	personAt: 0, // when the person last used a shared tab
	personInputAt: new Map(), // tab id -> when the person last used it
	aiInputAt: new Map(), // tab id -> when the AI last sent input there
	aiActAt: new Map(), // tab id -> when the AI last sent anything but looking there (input, script, navigation)
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

// A site is a web origin: scheme://host:port. about:blank has none, so an empty tab needs no leave. Anything that
// isn't a web page (data:, an opaque origin, chrome:, this extension's own pages) is never a site the AI may use.
function siteOf(url) {
	if (!url || /^about:(blank|srcdoc)([?#]|$)/.test(url)) return '';
	try {
		const u = new URL(url);
		return ['http:', 'https:', 'file:'].includes(u.protocol) ? u.origin : 'null';
	} catch (e) {
		return 'null';
	}
}

function allowedUrl(url) {
	const site = siteOf(url);
	if (site === '') return true;
	if (site === 'null') return false;
	return state.allowed.has(site) || state.settings.alwaysAllow.some((p) => siteMatches(p, site));
}

// An alwaysAllow entry is a site (https://example.com:8443), a site's subdomains (https://*.example.com, which
// matches a.example.com but not example.com or badexample.com), or "*" for every site, as before sites were asked.
function siteMatches(pattern, site) {
	if (pattern === '*' || pattern === site) return true;
	const m = /^(https?):\/\/\*\.([a-z0-9.-]+)(?::(\d+))?$/i.exec(pattern);
	if (!m) return false;
	try {
		const u = new URL(site);
		return u.protocol === m[1].toLowerCase() + ':' && u.hostname.endsWith('.' + m[2].toLowerCase()) && u.port === (m[3] || '');
	} catch (e) {
		return false;
	}
}

async function allowSite(url) {
	const site = siteOf(url);
	if (!site || site === 'null') return;
	state.allowed.add(site);
	state.declined.delete(site);
	await save();
}

function notAllowed(url, more) {
	return `${siteOf(url)} is refused through the extension bridge: it is not a site the person has allowed the AI to use; ${more}`;
}

// The AI asks for a new site in a window of this extension's own: no shared tab can reach it, and the AI's input
// goes only to shared tabs, so only the person can answer. One ask at a time; a site they declined isn't asked again.
async function ask(url) {
	const site = siteOf(url);
	if (site === 'null') return `${url} is refused through the extension bridge: only web pages can be opened there`;
	if (state.declined.has(site)) return notAllowed(url, 'they declined it. They can share a tab on it themselves if they change their mind');
	if (state.asking && state.asking.origin !== site) return notAllowed(url, `another ask (${state.asking.origin}) is waiting for them`);
	if (!state.asking) await openAsk({ kind: 'site', origin: site, url });
	return notAllowed(url, 'a window asks them now. Once they allow it, try again');
}

async function openAsk(asked) {
	const page = C.runtime.getURL('ask.html') + '#' + encodeURIComponent(JSON.stringify(asked));
	const win = await call(C.windows, 'create', { url: page, type: 'popup', width: 520, height: 240, left: 640, top: 80, focused: true });
	state.asking = { ...asked, windowId: win.id };
	await save();
}

// -- Consequential clicks --------------------------------------------------------------------------------------------
// Before an AI action that can activate something (a press, a key, a touch, page script), the page's copy of watch.js
// is armed. While armed, its capture listeners (registered before any of the page's own) hold a press, click, Enter
// or form submit on a control that places an order, pays, deletes an account or grants access, in that same event,
// and tell this worker. The action then comes back refused and the person is asked: Allow this one click, or No.
const ACTIVATING = /^(Input\.dispatch(Mouse|Key|Touch)Event|Input\.insertText|Runtime\.(evaluate|callFunctionOn|runScript)|DOM\.focus)$/;
const HELD_WAIT_MS = 60; // the page reports a held event while dispatching it, before the command's reply

function activates(msg) {
	if (!ACTIVATING.test(msg.method)) return false;
	const type = (msg.params || {}).type;
	return !(msg.method === 'Input.dispatchMouseEvent' && (type === 'mouseMoved' || type === 'mouseWheel'));
}

// Whether to wait to hear if the page held it: what can activate a control (typing a letter can't).
function mayActivate(msg) {
	const p = msg.params || {};
	if (msg.method.startsWith('Runtime.')) return true;
	if (msg.method === 'Input.dispatchMouseEvent') return p.type === 'mousePressed' || p.type === 'mouseReleased';
	if (msg.method === 'Input.dispatchTouchEvent') return p.type === 'touchStart' || p.type === 'touchEnd';
	if (msg.method === 'Input.dispatchKeyEvent') {
		return ['Enter', ' ', 'NumpadEnter'].includes(p.key) || p.code === 'Enter' || p.code === 'NumpadEnter' || p.code === 'Space' ||
			p.text === '\r' || p.text === '\n' || p.text === ' ' || p.windowsVirtualKeyCode === 13 || p.windowsVirtualKeyCode === 32;
	}
	return false;
}

function clickKey(origin, label) {
	return origin + '|' + label;
}

async function armPage(tabId) {
	const id = ++state.armSeq;
	const now = Date.now();
	state.clickGrants = state.clickGrants.filter((g) => g.until > now);
	const grants = state.clickGrants.filter((g) => g.tabId === tabId).map((g) => clickKey(g.origin, g.label));
	const arm = { id, ms: 3000, words: CONSEQUENTIAL, grants };
	// every frame gets it (payment forms live in frames); the top frame's answer says the page is listening
	await Promise.race([
		new Promise((resolve) => C.tabs.sendMessage(tabId, { arm }, () => resolve(void C.runtime.lastError))),
		new Promise((resolve) => setTimeout(resolve, 300)),
	]);
	return id;
}

// Listen before sending: the page reports while it dispatches, which can be before the command's reply arrives.
function listenHeld(id) {
	const box = { report: null, wake: null };
	state.heldWaiters.set(id, (report) => {
		box.report = box.report || report;
		if (box.wake) box.wake();
	});
	const done = () => state.heldWaiters.delete(id);
	const wait = (ms) =>
		new Promise((resolve) => {
			if (box.report) return resolve(box.report);
			box.wake = () => resolve(box.report);
			setTimeout(() => resolve(box.report), ms);
		}).finally(done);
	return { wait, done };
}

function onHeld(tabId, report) {
	state.held.set(tabId, report);
	const waiter = state.heldWaiters.get(report.id);
	if (waiter) waiter(report);
}

async function heldRefusal(tabId, report) {
	const label = String(report.label || '').replace(/[\u0000-\u001f]/g, ' ').slice(0, 80);
	const what = `the press on "${label}" (the page's own words) is held through the extension bridge: it would place an order, pay, delete an account or grant access, which waits for the person`;
	if (state.clickDeclined.has(clickKey(report.origin, report.label))) return `${what}; they said no to it. It did not happen.`;
	if (state.asking && !(state.asking.kind === 'click' && state.asking.label === report.label)) {
		return `${what}; another ask is waiting for them first. It did not happen.`;
	}
	if (!state.asking) await openAsk({ kind: 'click', origin: report.origin, label: report.label, tabId });
	return `${what}; a window asks them now. It did not happen. Once they allow it, click again.`;
}

async function answer(reply, windowId) {
	const asked = state.asking;
	// only from the window this worker opened for this ask: a copy of the page in some tab is not the person's answer
	if (!asked || reply.origin !== asked.origin || windowId !== asked.windowId) return;
	state.asking = null;
	await save();
	call(C.windows, 'remove', asked.windowId).catch(() => {});
	if (asked.kind === 'click') {
		// one click on that control, never the site: a site the person allowed is not consent to pay there
		if (reply.answer === 'allow') state.clickGrants.push({ tabId: asked.tabId, origin: asked.origin, label: asked.label, until: Date.now() + 60000 });
		if (reply.answer === 'no') state.clickDeclined.add(clickKey(asked.origin, asked.label));
		emit({ event: 'site', origin: asked.origin, answer: reply.answer, click: asked.label });
		return;
	}
	if (reply.answer === 'allow' || reply.answer === 'always') await allowSite(asked.url);
	if (reply.answer === 'always' && !state.settings.alwaysAllow.includes(asked.origin)) {
		state.settings.alwaysAllow = [...state.settings.alwaysAllow, asked.origin];
		await keepSettings({ alwaysAllow: state.settings.alwaysAllow });
	}
	if (reply.answer === 'no') {
		state.declined.add(asked.origin);
		await save();
	}
	emit({ event: 'site', origin: asked.origin, answer: reply.answer });
}

// Take a site back: no longer allowed (this session or always) and no longer declined. A shared tab on it stops being
// shared, as if it had just moved there. This only ever takes access away, so the relay may ask for it too.
async function forgetSite(site) {
	state.allowed.delete(site);
	state.declined.delete(site);
	if (state.settings.alwaysAllow.includes(site)) {
		state.settings.alwaysAllow = state.settings.alwaysAllow.filter((s) => s !== site);
		await keepSettings({ alwaysAllow: state.settings.alwaysAllow });
	}
	for (const g of [...state.clickDeclined]) if (g.startsWith(site + '|')) state.clickDeclined.delete(g);
	await save();
	for (const tabId of [...state.shared]) {
		const url = state.urls.get(tabId) || (await call(C.tabs, 'get', tabId).catch(() => ({}))).url;
		if (url && siteOf(url) === site && !allowedUrl(url)) await moved(tabId, url);
	}
	emit({ event: 'site', origin: site, answer: 'forgotten' });
}

function sitesNow() {
	const asking = state.asking ? state.asking.origin : null;
	return { allowed: [...state.allowed], always: state.settings.alwaysAllow, declined: [...state.declined], asking };
}

async function keepSettings(changes) {
	// settings the person changed here, kept across restarts on top of the packaged ones
	const local = (await call(C.storage.local, 'get', 'settings')).settings || {};
	await call(C.storage.local, 'set', { settings: { ...local, ...changes } });
}

// A shared tab that reaches a site the person hasn't allowed (a link, a redirect, page script, or the person's own
// navigation) stops being shared at once: unshare() drops it from `shared` before its first await, so not one more
// event from the new page is passed on. Its pill asks the person whether the AI may carry on there.
const AI_CAUSED_MS = 5000;

async function moved(tabId, url) {
	if (!state.shared.has(tabId)) return;
	const ai = state.aiActAt.get(tabId) || 0;
	const byAi = Date.now() - ai < AI_CAUSED_MS && ai >= (state.personInputAt.get(tabId) || 0);
	const site = siteOf(url);
	const done = unshare(tabId, `it moved to ${site}, a site the person has not allowed`);
	state.moved.set(tabId, { site, byAi });
	state.offered.add(tabId);
	await done;
	pushPill(tabId);
	emit({ event: 'offered', tabId, why: `moved to ${site}, a site the person has not allowed` });
}

async function checkSite(tabId, acting) {
	// Before anything reaches the tab, the check Claude in Chrome makes before each action: is the tab still on a site
	// the person allowed? Looking uses the address last seen; acting asks the browser, pending navigation included.
	let url = state.urls.get(tabId);
	let pending = null;
	if (acting || url === undefined) {
		const tab = await call(C.tabs, 'get', tabId);
		url = tab.url;
		pending = tab.pendingUrl || null;
		state.urls.set(tabId, url);
	}
	if (!allowedUrl(url)) {
		await moved(tabId, url);
		throw new Error(notAllowed(url, 'the tab went there, so it is no longer shared; its pill asks the person'));
	}
	if (pending && !allowedUrl(pending)) throw new Error(notAllowed(pending, 'the tab is on its way there'));
}

// -- Frames and workers of other sites ------------------------------------------------------------------------------
// A shared tab's page can hold frames of other sites (out-of-process: a sign-in widget, a payment form) and workers.
// Through CDP their own sessions read what the page itself never could. A child on a site the person hasn't allowed
// is never shown to the AI: it is let run (never left paused for a debugger, which would freeze the person's page),
// then let go, and its events and commands stop here. Its pixels are still in the tab's screenshots.
// Chrome attaches a cross-site frame before its navigation commits, with no address yet: whose it is isn't known. Such
// a child is held back (let run, its events kept) until its address is known, then shown to the AI or let go.
function childAttached(source, params) {
	const info = params.targetInfo || {};
	const child = { tabId: source.tabId, targetId: info.targetId, url: info.url || '', parent: source.sessionId, attach: params };
	state.children.set(params.sessionId, child);
	if (params.waitingForDebugger) resumeChild(source.tabId, params.sessionId); // a paused frame freezes the person's page
	if (siteOf(child.url) === '') {
		child.pending = [];
		return false;
	}
	return decideChild(params.sessionId);
}

async function resumeChild(tabId, sessionId) {
	// The first try can come before the child takes commands; repeat while it is still waiting to be told its site.
	for (let attempt = 0; attempt < 4; attempt++) {
		const child = state.children.get(sessionId);
		if (!child || (attempt > 0 && !child.pending)) return;
		try {
			await call(C.debugger, 'sendCommand', { tabId, sessionId }, 'Runtime.runIfWaitingForDebugger', {});
		} catch (e) {
			// not ready yet, or already gone
		}
		await new Promise((r) => setTimeout(r, 150 * (attempt + 1)));
	}
}

// Show a child whose address is now known to the AI (its attach, then what it said meanwhile), or let it go.
function decideChild(sessionId) {
	const child = state.children.get(sessionId);
	if (!child || child.hidden) return false;
	if (!allowedUrl(child.url)) {
		letGo(child.tabId, sessionId);
		return false;
	}
	const held = child.pending;
	child.pending = null;
	if (held) {
		const attach = { ...child.attach, waitingForDebugger: false, targetInfo: { ...(child.attach.targetInfo || {}), url: child.url } };
		emit({ event: 'cdp', tabId: child.tabId, sessionId: child.parent, method: 'Target.attachedToTarget', params: attach });
		for (const e of held) emit({ event: 'cdp', tabId: child.tabId, sessionId, method: e.method, params: e.params });
	}
	return true;
}

function letGo(tabId, sessionId) {
	// Kept attached but hidden: detaching it would end the auto-attach that later frames of the page rely on.
	const child = state.children.get(sessionId);
	if (child) {
		child.hidden = true;
		child.pending = null;
	}
}

function childAllowed(sessionId) {
	const child = state.children.get(sessionId);
	return !!child && !child.hidden && !child.pending && siteOf(child.url) !== '' && allowedUrl(child.url);
}

// Sessions reach their own tab and its frames, never other tabs: tabs are opened, closed and switched with the tab
// tools, which the person's consent covers.
const SESSION_TARGET_OK = new Set(['Target.setAutoAttach', 'Target.getTargetInfo', 'Target.detachFromTarget']);

async function checkNavigation(msg) {
	// Navigations the AI starts are checked before any request leaves, so the person's cookies go nowhere new.
	const params = msg.params || {};
	let url = null;
	if (msg.method === 'Page.navigate') url = params.url;
	if (msg.method === 'Page.navigateToHistoryEntry') {
		const target = msg.sessionId ? { tabId: msg.tabId, sessionId: msg.sessionId } : { tabId: msg.tabId };
		const history = await call(C.debugger, 'sendCommand', target, 'Page.getNavigationHistory', {});
		const entry = (history.entries || []).find((e) => e.id === params.entryId);
		url = entry ? entry.url : null;
	}
	if (url !== null && !allowedUrl(url)) throw new Error(await ask(url));
}

function alwaysShared(url) {
	return !!url && state.settings.alwaysShare.some((g) => globToRegExp(g).test(url));
}

function emit(msg) {
	if (state.ws && state.ws.readyState === 1) state.ws.send(JSON.stringify(msg));
}

async function save() {
	await call(store, 'set', {
		shared: [...state.shared],
		holder: state.holder,
		aiWindow: state.aiWindow,
		stopped: state.stopped,
		// grants for this browser session only: without session storage (old Chromium) they are kept in memory
		...(C.storage.session ? { allowed: [...state.allowed], declined: [...state.declined], asking: state.asking } : {}),
	});
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

function pillState(tabId) {
	const offered = state.offered.has(tabId) && !state.shared.has(tabId);
	const moved = offered ? state.moved.get(tabId) || null : null;
	return { shared: state.shared.has(tabId), offered, moved, holder: state.holder, stopped: state.stopped };
}

function pushPill(tabId) {
	// the page's own copy of watch.js draws it; a tab without one (chrome:// pages) just has no pill
	C.tabs.sendMessage(tabId, { pill: pillState(tabId) }, () => void C.runtime.lastError);
}

// The person's own ways of sharing: sharing a tab lets the AI use that tab's site.
const BY_PERSON = new Set(['shared by the person', 'always shared site']);

async function share(tabId, why) {
	const tab = await call(C.tabs, 'get', tabId);
	const url = tab.pendingUrl || tab.url;
	if ((url || '').startsWith(C.runtime.getURL(''))) throw new Error("the bridge's own pages are never shared");
	if (BY_PERSON.has(why)) await allowSite(url);
	else if (!allowedUrl(url)) throw new Error(notAllowed(url, 'the tab was not shared'));
	state.offered.delete(tabId);
	state.moved.delete(tabId);
	state.urls.delete(tabId); // the next check reads the address afresh
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
		pushPill(tabId);
	} catch (e) {
		state.shared.delete(tabId); // chrome:// and other extensions' pages cannot be shared
		await save();
		badge(tabId);
		throw e;
	}
}

async function unshare(tabId, why) {
	if (!state.shared.delete(tabId)) return;
	for (const [sid, child] of state.children) if (child.tabId === tabId) state.children.delete(sid);
	if (state.attached.delete(tabId)) await call(C.debugger, 'detach', { tabId }).catch(() => {});
	await save();
	badge(tabId);
	pushPill(tabId);
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
	if (state.stopped && !['ping', 'tabs', 'forget'].includes(msg.op)) throw new Error(STOPPED); // forgetting only takes access away
	switch (msg.op) {
		case 'ping':
			return { sites: sitesNow() };
		case 'forget':
			await forgetSite(String(msg.site || ''));
			return { sites: sitesNow() };
		case 'reload': {
			// The relay saw an older copy running: Chrome keeps the old service worker after the files change until
			// the extension is reloaded. Reload only if the files on disk differ from the code running: otherwise it
			// changes nothing, and Chromium unloads an extension loaded with --load-extension when it reloads.
			const read = async (name) => (await fetch(C.runtime.getURL(name), { cache: 'no-store' })).json();
			const [disk, policy] = await Promise.all([read('manifest.json'), read('policy.json')]);
			const same = disk.version === C.runtime.getManifest().version && JSON.stringify(policy.passive) === JSON.stringify(POLICY);
			if (same) throw new Error('the files on disk are the ones running, so a reload would not change them');
			setTimeout(() => C.runtime.reload(), 100); // answer first; the person shares their tabs again afterwards
			return { reloading: true };
		}
		case 'tabs': {
			const infos = await Promise.all([...state.shared].map((id) => targetInfo(id).catch(() => null)));
			return { tabs: infos.filter(Boolean), holder: state.holder };
		}
		case 'send': {
			needShared(msg.tabId);
			if (state.holder === 'human' && acts(msg.method)) throw new Error('the person is using the browser right now');
			if (msg.method.startsWith('Target.') && !SESSION_TARGET_OK.has(msg.method)) {
				throw new Error(`${msg.method} is refused through the extension bridge: it reaches past the shared tab; use the tab tools`);
			}
			if (msg.sessionId && !childAllowed(msg.sessionId)) {
				const child = state.children.get(msg.sessionId);
				throw new Error(child
					? notAllowed(child.url, 'that frame or worker belongs to it, so the AI cannot use it here')
					: `${msg.method} is refused through the extension bridge: that session is not a frame or worker the bridge knows of`);
			}
			await checkSite(msg.tabId, acts(msg.method));
			await checkNavigation(msg);
			if (msg.method === 'Page.handleJavaScriptDialog' && (msg.params || {}).accept) {
				// The page's own question (confirm, prompt) is the person's to answer: it is in front of them in the tab.
				const type = state.dialogs.get(msg.tabId);
				if (type !== 'alert' && type !== 'beforeunload') {
					throw new Error(`Page.handleJavaScriptDialog is refused through the extension bridge: accepting the page's ${type || 'unknown'} dialog is the person's to answer, in their tab; dismissing it is allowed`);
				}
			}
			const arming = activates(msg) ? await armPage(msg.tabId) : 0;
			const held = arming && mayActivate(msg) ? listenHeld(arming) : null;
			await ensureAttached(msg.tabId);
			const input = msg.method.startsWith('Input.');
			if (input && !msg.sessionId) await bringToFront(msg.tabId);
			const target = msg.sessionId ? { tabId: msg.tabId, sessionId: msg.sessionId } : { tabId: msg.tabId };
			const acting = acts(msg.method);
			const mark = () => {
				if (input) state.aiInputAt.set(msg.tabId, Date.now());
				if (acting) state.aiActAt.set(msg.tabId, Date.now());
			};
			mark();
			let result;
			try {
				result = (await call(C.debugger, 'sendCommand', target, msg.method, msg.params || {})) || {};
			} catch (e) {
				if (held) held.done();
				throw e;
			} finally {
				mark();
			}
			const report = held ? await held.wait(HELD_WAIT_MS) : null;
			if (report) throw new Error(await heldRefusal(msg.tabId, report));
			return result;
		}
		case 'open':
			if (!allowedUrl(msg.url || 'about:blank')) throw new Error(await ask(msg.url));
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
			policy: POLICY,
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
	for (const tabId of state.shared) {
		badge(tabId);
		pushPill(tabId);
	}
}

// Input the AI did not send is the person's: their clicks, keys and wheel turns are trusted events too,
// so the only way to tell them apart is that the AI's own input went through this worker moments before.
const AI_ECHO_MS = 600;

async function onPersonInput(tabId, type) {
	if (Date.now() - (state.aiInputAt.get(tabId) || 0) < AI_ECHO_MS) return;
	state.personAt = Date.now();
	state.personInputAt.set(tabId, state.personAt);
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
		sites: sitesNow(),
	};
}

async function boot() {
	try {
		const policy = await (await fetch(C.runtime.getURL('policy.json'))).json();
		PASSIVE = policy.passive.map(globToRegExp);
		POLICY = policy.passive;
		CONSEQUENTIAL = policy.consequential || [];
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
	const kept = await call(store, 'get', ['shared', 'holder', 'aiWindow', 'stopped', 'allowed', 'declined', 'asking']);
	state.asking = kept.asking || null; // an ask still open after a worker restart is still answered
	for (const site of kept.allowed || []) state.allowed.add(site);
	for (const site of kept.declined || []) state.declined.add(site);
	state.holder = kept.holder || 'agent';
	state.stopped = !!kept.stopped;
	state.aiWindow = kept.aiWindow ?? null;
	const tabs = await call(C.tabs, 'query', {});
	const live = new Set(tabs.map((t) => t.id));
	for (const t of tabs) {
		if (!(kept.shared || []).includes(t.id)) continue;
		if (allowedUrl(t.url)) state.shared.add(t.id);
		else {
			state.offered.add(t.id); // it reached a site not allowed while the worker was stopped
			state.moved.set(t.id, { site: siteOf(t.url), byAi: false });
		}
	}
	for (const t of tabs) {
		if (state.stopped || !alwaysShared(t.url)) continue;
		state.shared.add(t.id);
		state.allowed.add(siteOf(t.url)); // the person chose to share it, so its site too
	}
	await save();
	for (const id of state.shared) badge(id);
	connect();
}

C.debugger.onEvent.addListener((source, method, params) => {
	if (!state.shared.has(source.tabId)) return;
	if (source.sessionId && state.children.has(source.sessionId)) {
		const child = state.children.get(source.sessionId);
		if (child.pending) {
			if (child.pending.length < 200) child.pending.push({ method, params }); // kept until its site is known
			return;
		}
	}
	if (source.sessionId && !childAllowed(source.sessionId)) return; // a frame or worker of a site not allowed
	if (method === 'Target.attachedToTarget' && !childAttached(source, params)) return;
	if (method === 'Target.detachedFromTarget') {
		const child = state.children.get(params.sessionId);
		state.children.delete(params.sessionId);
		if (child && child.pending) return; // the AI was never told of it
	}
	if (method === 'Target.targetInfoChanged') {
		for (const [sid, child] of state.children) {
			if (child.targetId !== (params.targetInfo || {}).targetId || child.hidden) continue;
			const known = siteOf(child.url) !== '';
			child.url = params.targetInfo.url || child.url;
			if (child.pending) {
				if (siteOf(child.url) !== '') decideChild(sid);
				return; // the AI hears of it, if at all, through its attach
			}
			if (known && !allowedUrl(child.url)) {
				// a frame that went to a site not allowed: the AI's session on it ends here
				letGo(source.tabId, sid);
				emit({ event: 'cdp', tabId: source.tabId, method: 'Target.detachedFromTarget', params: { sessionId: sid, targetId: child.targetId } });
				return;
			}
		}
	}
	if (method === 'Page.javascriptDialogOpening' && !source.sessionId) state.dialogs.set(source.tabId, params.type);
	if (method === 'Page.javascriptDialogClosed' && !source.sessionId) state.dialogs.delete(source.tabId);
	if (method === 'Page.frameNavigated' && !source.sessionId && params && params.frame && !params.frame.parentId) {
		state.urls.set(source.tabId, params.frame.url);
		if (!allowedUrl(params.frame.url)) return void moved(source.tabId, params.frame.url); // nothing of it is passed on
	}
	emit({ event: 'cdp', tabId: source.tabId, sessionId: source.sessionId, method, params });
});

C.debugger.onDetach.addListener(async (source, reason) => {
	const tabId = source.tabId;
	state.attached.delete(tabId);
	if (reason === 'canceled_by_user') {
		// Cancel is the person's stop button, not just "this tab": unshare everything and open nothing new.
		if (state.stopped) return; // Chrome detaches every tab at once; the first one does the work
		state.stopped = true;
		if (state.asking) {
			call(C.windows, 'remove', state.asking.windowId).catch(() => {});
			state.asking = null;
		}
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

C.tabs.onRemoved.addListener((tabId) => {
	state.offered.delete(tabId);
	state.moved.delete(tabId);
	state.urls.delete(tabId);
	state.dialogs.delete(tabId);
	state.held.delete(tabId);
	unshare(tabId, 'tab closed');
});

// A tab opened from a shared tab (target=_blank, window.open) follows whoever opened it. The AI's, opened by its
// own click or script there, is part of what it was doing and is shared. The person's (a middle-click from a shared
// mail to their bank) or one a page opened on its own stays unshared; its pill offers to share it.
const OPENED_BY_AI_MS = 2500;
const INPUT_ARRIVES_MS = 300; // the page reports the person's click a moment after the tab may already exist

C.tabs.onCreated.addListener((tab) => {
	const opener = tab.openerTabId;
	if (opener === undefined || !state.shared.has(opener)) return;
	const created = Date.now();
	const ai = state.aiActAt.get(opener) || 0;
	setTimeout(() => adoptOrOffer(tab.id, opener, created, ai), INPUT_ARRIVES_MS);
});

async function adoptOrOffer(tabId, opener, created, ai) {
	await state.ready;
	const person = state.personInputAt.get(opener) || 0;
	const tab = await call(C.tabs, 'get', tabId).catch(() => null);
	if (!tab) return;
	const url = tab.pendingUrl || tab.url;
	if (created - ai < OPENED_BY_AI_MS && person < ai) {
		if (allowedUrl(url)) return share(tabId, 'opened by the AI from a shared tab').catch(() => {});
		state.moved.set(tabId, { site: siteOf(url), byAi: true }); // the AI's popup went to a site not allowed: theirs to say
	}
	state.offered.add(tabId);
	pushPill(tabId);
	let why = person >= ai && created - person < OPENED_BY_AI_MS ? 'the person opened it' : 'the page opened it';
	if (state.moved.has(tabId)) why = 'the AI opened it on a site the person has not allowed';
	emit({ event: 'offered', tabId, why });
}

C.tabs.onUpdated.addListener(async (tabId, change) => {
	await state.ready;
	if (state.shared.has(tabId)) {
		if (change.url) state.urls.set(tabId, change.url);
		if (change.url && !allowedUrl(change.url)) return moved(tabId, change.url);
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
	if (state.asking && id === state.asking.windowId) {
		state.asking = null; // closed unanswered: they may be asked again
		save();
	}
});

C.runtime.onMessage.addListener((msg, sender, reply) => {
	if (msg.input) {
		if (sender.tab && state.shared.has(sender.tab.id)) onPersonInput(sender.tab.id, msg.input);
		return false;
	}
	if (msg.held || msg.consumed) {
		// from this extension's content script in a shared tab (any frame); page script can't reach this channel
		if (!sender.tab || sender.id !== C.runtime.id || !state.shared.has(sender.tab.id)) return false;
		if (msg.held) onHeld(sender.tab.id, msg.held);
		if (msg.consumed) {
			const at = state.clickGrants.findIndex((g) => g.tabId === sender.tab.id && clickKey(g.origin, g.label) === msg.consumed);
			if (at >= 0) state.clickGrants.splice(at, 1);
		}
		return false;
	}
	if (msg.answer) {
		// only from this extension's ask window, never from a content script in some page
		if (sender.id === C.runtime.id && sender.tab && (sender.url || '').startsWith(C.runtime.getURL('ask.html'))) {
			answer(msg, sender.tab.windowId);
		}
		return false;
	}
	// The popup (an extension page) may share any tab and change the relay; content scripts in pages may only act on
	// their own tab through its pill.
	const fromExtension = sender.id === C.runtime.id && (sender.url || '').startsWith(C.runtime.getURL(''));
	if (['share', 'unshare', 'relay', 'forget'].includes(msg.ask) && !fromExtension) return false;
	(async () => {
		await state.ready;
		if (msg.ask === 'share') await share(msg.tabId, 'shared by the person');
		else if (msg.ask === 'unshare') await unshare(msg.tabId, 'unshared by the person');
		else if (msg.ask === 'holder') await setHolder(msg.holder);
		else if (msg.ask === 'pill') return pillState(sender.tab ? sender.tab.id : -1);
		else if (msg.ask === 'share-here' && sender.tab) await share(sender.tab.id, 'shared by the person');
		else if (msg.ask === 'forget') await forgetSite(String(msg.site || ''));
		else if (msg.ask === 'sites' && sender.tab) {
			// from the pill: the person's list of sites, in a tab of its own that is never shared
			await call(C.tabs, 'create', { url: C.runtime.getURL('popup.html') + '#page', active: true });
		}
		else if (msg.ask === 'decline-here' && sender.tab) {
			state.offered.delete(sender.tab.id);
			state.moved.delete(sender.tab.id);
			pushPill(sender.tab.id);
		}
		else if (msg.ask === 'relay') {
			state.settings.relay = msg.relay;
			await keepSettings({ relay: msg.relay });
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
// These wake the worker straight after an install, an update or a reload (the relay may have asked for one) and when
// the browser starts, rather than at the next alarm.
C.runtime.onInstalled.addListener(() => state.ready.then(connect));
if (C.runtime.onStartup) C.runtime.onStartup.addListener(() => state.ready.then(connect));

state.ready = boot();
