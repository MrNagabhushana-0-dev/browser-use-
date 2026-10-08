'use strict';
const C = globalThis.chrome;
let tabId = null;

function ask(msg) {
	return new Promise((resolve) => C.runtime.sendMessage({ ...msg, tabId }, resolve));
}

function siteRow(site, what) {
	const li = document.createElement('li');
	const name = document.createElement('span');
	name.textContent = what ? `${site} (${what})` : site;
	const forget = document.createElement('button');
	forget.className = 'forget';
	forget.textContent = 'Remove';
	forget.onclick = async () => render(await ask({ ask: 'forget', site }));
	li.append(name, forget);
	return li;
}

function renderSites(sites) {
	const list = document.getElementById('sites');
	list.replaceChildren();
	if (!sites) return;
	const always = new Set(sites.always || []);
	for (const site of [...new Set([...(sites.allowed || []), ...always])].sort()) list.append(siteRow(site, always.has(site) ? 'always' : ''));
	for (const site of [...(sites.declined || [])].sort()) list.append(siteRow(site, 'you said no'));
	document.getElementById('nosites').hidden = list.children.length > 0;
}

function render(s) {
	if (!s) return;
	renderSites(s.sites);
	document.getElementById('dot').className = 'dot' + (s.connected ? ' on' : '');
	document.getElementById('conn').textContent = s.connected ? 'AI connected' : 'AI not connected';
	const share = document.getElementById('share');
	share.textContent = s.shared ? 'Stop sharing this tab' : 'Share this tab with the AI';
	share.className = s.shared ? '' : 'primary';
	const hold = document.getElementById('hold');
	hold.textContent = s.holder === 'human' ? 'Hand back to the AI' : 'Take the wheel (pause the AI)';
	hold.className = s.holder === 'human' ? 'hold' : '';
	document.getElementById('note').textContent = s.error
		? s.error
		: s.stopped
		? 'Stopped: you pressed Cancel on the debugging bar. Share a tab to let the AI work again.'
		: `${s.count} tab${s.count === 1 ? '' : 's'} shared. The AI sees and acts only in shared tabs, with the same clicks and keys you use.`;
	document.getElementById('relay').value = s.relay || '';
	window.__status = s;
}

document.getElementById('share').onclick = async () => render(await ask({ ask: window.__status.shared ? 'unshare' : 'share' }));
document.getElementById('hold').onclick = async () =>
	render(await ask({ ask: 'holder', holder: window.__status.holder === 'human' ? 'agent' : 'human' }));
document.getElementById('relay').onchange = async (e) => render(await ask({ ask: 'relay', relay: e.target.value.trim() }));

// Opened as a page of its own (from the pill's "Sites" button) rather than the toolbar popup.
if (location.hash === '#page') document.body.className = 'page';

C.tabs.query({ active: true, currentWindow: true }, async ([tab]) => {
	tabId = tab ? tab.id : null;
	render(await ask({ ask: 'status' }));
});
