"""Read a page's affordances the way an assistive technology would.

Not the HTML. The three ways an agent can perceive a page each fail differently: pixels
carry no semantics and cost a fortune, a DOM dump buries the four things you can actually
do under ten thousand nodes of layout, and WebMCP is perfect but almost nobody implements
it. What is left is the layer browsers already compute for screen readers — role,
accessible name, value, state — which is small, semantic, and exists on every site whether
its authors thought about agents or not.

This scanner extracts affordances from that layer: the forms, the controls, the buttons a
person could operate. It runs as one script in the page, so the cost is a single round
trip rather than a document crossing the context window.
"""

# Accessible-name computation, trimmed to what actually decides a control's name in
# practice: the explicit label, then the associated <label>, then the visible text, then
# the fallbacks authors reach for. Full AccName is a spec unto itself and the remainder of
# it almost never changes the answer for interactive controls.
SCAN_JS = r"""
const MAX_CONTROLS = 40;
const MAX_FORMS = 12;
const MAX_BUTTONS = 25;

const clean = (s) => (s || '').replace(/\s+/g, ' ').trim().slice(0, 120);

const INTERACTIVE = 'a[href], button, input, select, textarea, option, label, [role="button"], [role="link"], [role="tab"], [role="checkbox"], [role="switch"], [role="menuitem"]';

const accessibleName = (el) => {
	if (!el) return '';
	const aria = el.getAttribute && el.getAttribute('aria-label');
	if (aria) return clean(aria);
	const by = el.getAttribute && el.getAttribute('aria-labelledby');
	if (by) {
		const parts = by.split(/\s+/).map(id => document.getElementById(id)).filter(Boolean);
		if (parts.length) return clean(parts.map(p => p.innerText || p.textContent).join(' '));
	}
	if (el.id) {
		const lab = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
		if (lab) return clean(lab.innerText || lab.textContent);
	}
	const wrapping = el.closest && el.closest('label');
	if (wrapping) return clean(wrapping.innerText || wrapping.textContent);
	for (const attr of ['placeholder', 'title', 'alt', 'name']) {
		const v = el.getAttribute && el.getAttribute(attr);
		if (v) return clean(v);
	}
	// Visible text names a control; it does not name a container. Falling back to innerText
	// for a <table> or <form> yields its entire subtree as the "name", which is both useless
	// and unmatchable when resolving the locator later.
	if (el.matches && el.matches(INTERACTIVE)) return clean(el.innerText || el.value || '');
	return '';
};

// The role the platform would report. Explicit role wins; otherwise derive it from the
// tag and type the way the HTML-AAM mapping does for the elements that matter here.
const roleOf = (el) => {
	const explicit = el.getAttribute && el.getAttribute('role');
	if (explicit) return explicit.toLowerCase();
	const tag = el.tagName.toLowerCase();
	if (tag === 'a') return el.hasAttribute('href') ? 'link' : 'generic';
	if (tag === 'button') return 'button';
	if (tag === 'select') return 'combobox';
	if (tag === 'textarea') return 'textbox';
	if (tag === 'input') {
		const t = (el.getAttribute('type') || 'text').toLowerCase();
		if (t === 'submit' || t === 'button' || t === 'reset' || t === 'image') return 'button';
		if (t === 'checkbox') return 'checkbox';
		if (t === 'radio') return 'radio';
		if (t === 'search') return 'searchbox';
		return 'textbox';
	}
	return 'generic';
};

// A handle that survives a re-render. data-testid first because it is the one attribute
// authors keep stable on purpose; then id; then role plus accessible name, which is what
// a person would say out loud; a CSS path only as a last resort.
const locatorFor = (el) => {
	const testid = el.getAttribute('data-testid') || el.getAttribute('data-test-id') || el.getAttribute('data-test');
	const loc = {testid: testid || null, id: el.id || null, role: roleOf(el), name: accessibleName(el), css: null};
	if (!testid && !el.id && !loc.name) {
		const parts = [];
		let node = el;
		for (let depth = 0; node && node.nodeType === 1 && depth < 4; depth++) {
			let seg = node.tagName.toLowerCase();
			if (node.classList && node.classList.length) seg += '.' + [...node.classList].slice(0, 2).join('.');
			parts.unshift(seg);
			node = node.parentElement;
		}
		loc.css = parts.join(' > ');
	}
	return loc;
};

const visible = (el) => {
	const r = el.getBoundingClientRect();
	if (r.width < 2 || r.height < 2) return false;
	const s = getComputedStyle(el);
	return s.visibility !== 'hidden' && s.display !== 'none' && s.opacity !== '0';
};

// Never describe a field that holds a secret, and never read its value back.
const sensitive = (el) => {
	const t = (el.getAttribute('type') || '').toLowerCase();
	if (t === 'password') return true;
	const ac = (el.getAttribute('autocomplete') || '').toLowerCase();
	return ac.includes('cc-') || ac.includes('one-time-code') || ac === 'current-password' || ac === 'new-password';
};

const describeControl = (el) => ({
	role: roleOf(el),
	name: accessibleName(el),
	type: (el.getAttribute('type') || '').toLowerCase() || null,
	required: !!(el.required || el.getAttribute('aria-required') === 'true'),
	sensitive: sensitive(el),
	options: el.tagName.toLowerCase() === 'select'
		? [...el.options].slice(0, 25).map(o => clean(o.textContent)).filter(Boolean)
		: null,
	locator: locatorFor(el),
});

const CONTROL_SELECTOR = 'input, textarea, select, [contenteditable="true"], [role="textbox"], [role="searchbox"], [role="combobox"]';
const BUTTON_SELECTOR = 'button, input[type=submit], input[type=button], [role="button"], a[href]';

// Deliberately NOT accessibleName(form): a form's innerText is every label, option and
// hint inside it, which yields names like "Email Password Sign in". What a person would
// call a form is what its submit button says.
const formName = (form, submit) => {
	const aria = form.getAttribute('aria-label');
	if (aria) return clean(aria);
	const by = form.getAttribute('aria-labelledby');
	if (by) {
		const parts = by.split(/\s+/).map(id => document.getElementById(id)).filter(Boolean);
		if (parts.length) return clean(parts.map(p => p.innerText).join(' '));
	}
	if (submit) {
		const label = accessibleName(submit);
		if (label) return label;
	}
	const heading = form.querySelector('h1, h2, h3, legend');
	if (heading) return clean(heading.innerText);
	return clean(form.getAttribute('name') || form.getAttribute('id') || '');
};

const forms = [];
for (const form of [...document.querySelectorAll('form')].slice(0, MAX_FORMS)) {
	if (!visible(form)) continue;
	const controls = [...form.querySelectorAll(CONTROL_SELECTOR)]
		.filter(visible).slice(0, MAX_CONTROLS).map(describeControl);
	if (!controls.length) continue;
	const submit = form.querySelector('button[type=submit], input[type=submit], button:not([type])');
	forms.push({
		name: formName(form, submit),
		action: form.getAttribute('action') || '',
		controls: controls,
		submit: submit ? {label: accessibleName(submit), locator: locatorFor(submit)} : null,
		locator: locatorFor(form),
	});
}

// Buttons that are not inside a form: the standalone verbs of the page.
const buttons = [];
for (const el of [...document.querySelectorAll(BUTTON_SELECTOR)].slice(0, 200)) {
	if (buttons.length >= MAX_BUTTONS) break;
	if (!visible(el) || el.closest('form')) continue;
	const name = accessibleName(el);
	if (!name) continue;
	buttons.push({role: roleOf(el), name: name, locator: locatorFor(el)});
}

// Controls outside any form — a site-wide search box usually lives here.
const loose = [];
for (const el of [...document.querySelectorAll(CONTROL_SELECTOR)].slice(0, 120)) {
	if (loose.length >= MAX_CONTROLS) break;
	if (!visible(el) || el.closest('form')) continue;
	loose.push(describeControl(el));
}

// Tables and repeated lists are where the page's *data* lives. Turning them into read
// tools is what stops an agent paging a table into its context one screenshot at a time.
const tables = [];
for (const table of [...document.querySelectorAll('table, [role="table"], [role="grid"]')].slice(0, 6)) {
	if (!visible(table)) continue;
	const headerCells = [...table.querySelectorAll('thead th, thead td, tr:first-child th')]
		.map(h => clean(h.innerText)).filter(Boolean).slice(0, 12);
	const bodyRows = table.querySelectorAll('tbody tr').length || Math.max(0, table.querySelectorAll('tr').length - 1);
	if (!headerCells.length || !bodyRows) continue;
	const caption = table.querySelector('caption');
	tables.push({
		name: clean(caption ? caption.innerText : '') || accessibleName(table) || '',
		headers: headerCells,
		rows: bodyRows,
		locator: locatorFor(table),
	});
}

// Tabs and in-page navigation: the verbs that move between views without a form.
const views = [];
for (const el of [...document.querySelectorAll('[role="tab"], nav a[href], [role="navigation"] a[href]')].slice(0, 60)) {
	if (views.length >= 12) break;
	if (!visible(el)) continue;
	const name = accessibleName(el);
	if (!name || name.length > 40) continue;
	views.push({role: roleOf(el), name: name, locator: locatorFor(el)});
}

// Pagination, recognised by what the control says rather than by any particular markup.
const PAGER = {next: /^(next|next page|\u203a|\u00bb|\u2192)$/i, previous: /^(prev|previous|previous page|\u2039|\u00ab|\u2190)$/i};
const pagers = [];
for (const el of [...document.querySelectorAll(BUTTON_SELECTOR)].slice(0, 200)) {
	if (!visible(el)) continue;
	const name = accessibleName(el);
	for (const kind of Object.keys(PAGER)) {
		if (PAGER[kind].test(name) && !pagers.some(p => p.kind === kind)) {
			pagers.push({kind: kind, name: name, locator: locatorFor(el)});
		}
	}
}

// Standalone checkboxes and switches: settings, filters, consent.
const toggles = [];
for (const el of [...document.querySelectorAll('input[type=checkbox], [role="switch"], [role="checkbox"]')].slice(0, 40)) {
	if (toggles.length >= 12) break;
	if (!visible(el) || el.closest('form')) continue;
	const name = accessibleName(el);
	if (!name) continue;
	toggles.push({name: name, checked: !!(el.checked || el.getAttribute('aria-checked') === 'true'), locator: locatorFor(el)});
}

return {url: location.href, origin: location.origin, title: document.title,
        forms: forms, buttons: buttons, controls: loose,
        tables: tables, views: views, pagers: pagers, toggles: toggles};
"""
