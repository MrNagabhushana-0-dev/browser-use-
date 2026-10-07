'use strict';
const C = globalThis.chrome;
let tabId = null;

function ask(msg) {
	return new Promise((resolve) => C.runtime.sendMessage({ ...msg, tabId }, resolve));
}

function render(s) {
	if (!s) return;
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

C.tabs.query({ active: true, currentWindow: true }, async ([tab]) => {
	tabId = tab ? tab.id : null;
	render(await ask({ ask: 'status' }));
});
