// Execute the *real* settings-page script against a small DOM shim.
//
// The Python regression feeds this probe the page rendered by the actual Jinja
// templates (with the real settings snapshot), so `collectConfig()` runs on the
// control values a browser would see instead of on a hand-written copy.
//
// stdin:  {"html": "<rendered page>", "scenarios": [{...}]}
// stdout: {"scriptBytes": n, "results": [{name, payload, poolHtml}]}
import fs from 'node:fs';

const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const html = String(input.html || '');
const scenarios = Array.isArray(input.scenarios) ? input.scenarios : [];

function parseAttributes(raw) {
  const attrs = {};
  const re = /([A-Za-z_:][-A-Za-z0-9_:.]*)(?:\s*=\s*"([^"]*)")?/g;
  let match;
  while ((match = re.exec(raw))) {
    attrs[match[1].toLowerCase()] = match[2] === undefined ? true : match[2];
  }
  return attrs;
}

function escapeText(value) {
  return String(value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function stubMarkup(node) {
  // A created subtree (e.g. pool checkboxes built with DOM APIs) has no HTML of
  // its own; expose its text so assertions can still see what the browser
  // would render.  This is a test shim, not a DOM implementation.
  if (node.children && node.children.length) {
    return node.children.map(stubMarkup).join('');
  }
  return escapeText(node._text || '');
}

function makeStub(id, init = {}) {
  const stub = {
    id,
    tagName: String(init.tag || 'div').toLowerCase(),
    value: init.value === undefined ? '' : String(init.value),
    checked: init.checked === true,
    max: init.max === undefined ? '' : String(init.max),
    min: init.min === undefined ? '' : String(init.min),
    hidden: false,
    disabled: false,
    dataset: {},
    style: {},
    files: [],
    children: [],
    parentNode: null,
    classList: { toggle() {}, add() {}, remove() {}, contains() { return false; } },
    setAttribute() {},
    getAttribute() { return null; },
    removeAttribute() {},
    appendChild(child) { this.children.push(child); child.parentNode = this; return child; },
    append(...nodes) { for (const node of nodes) this.appendChild(node); },
    removeChild(child) { this.children = this.children.filter(item => item !== child); },
    insertBefore(child) { this.children.unshift(child); child.parentNode = this; return child; },
    addEventListener() {},
    removeEventListener() {},
    remove() {},
    focus() {},
    click() {},
    querySelectorAll() { return []; },
    querySelector() { return null; },
    closest() { return null; },
    showModal() {},
    close() {},
  };
  // Real DOM semantics the page script depends on:
  //   * assigning textContent replaces children, so innerHTML returns escaped
  //     text -- escapeHtml() in both templates relies on exactly that;
  //   * assigning innerHTML keeps the markup and re-derives a <select> value.
  Object.defineProperty(stub, 'textContent', {
    get() {
      if (this.children && this.children.length) {
        return this.children.map(child => child.textContent).join('');
      }
      return this._text || '';
    },
    set(value) {
      this._text = String(value);
      this._html = escapeText(this._text);
      this.children = [];
    },
  });
  Object.defineProperty(stub, 'innerHTML', {
    get() {
      if (this.children && this.children.length) {
        return this.children.map(stubMarkup).join('');
      }
      return this._html || '';
    },
    set(raw) {
      const markup = String(raw);
      this._text = markup.replace(/<[^>]*>/g, '');
      this._html = markup;
      this.children = [];
      if (this.tagName === 'select') {
        const optionRe = /<option value="([^"]*)"([^>]*)>/g;
        let first = null;
        let selected = null;
        let option;
        while ((option = optionRe.exec(markup))) {
          if (first === null) first = option[1];
          if (/\bselected\b/.test(option[2])) selected = option[1];
        }
        if (selected !== null) this.value = selected;
        else if (first !== null) this.value = first;
      }
    },
  });
  return stub;
}

function parsePageElements(page) {
  const elements = new Map();
  const inputRe = /<(input|textarea)([^>]*)>/g;
  let match;
  while ((match = inputRe.exec(page))) {
    const attrs = parseAttributes(match[2]);
    if (!attrs.id) continue;
    elements.set(attrs.id, makeStub(attrs.id, {
      tag: match[1],
      value: attrs.value,
      checked: attrs.checked === true,
      max: attrs.max,
      min: attrs.min,
    }));
  }
  const selectRe = /<select([^>]*)>([\s\S]*?)<\/select>/g;
  while ((match = selectRe.exec(page))) {
    const attrs = parseAttributes(match[1]);
    if (!attrs.id) continue;
    const stub = makeStub(attrs.id, { tag: 'select' });
    stub.innerHTML = match[2];
    elements.set(attrs.id, stub);
  }
  return elements;
}

function extractInlineScripts(page) {
  const parts = [];
  const scriptRe = /<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/g;
  let match;
  while ((match = scriptRe.exec(page))) {
    if (match[1].trim()) parts.push(match[1]);
  }
  return parts.join('\n;\n');
}

const scriptSource = extractInlineScripts(html);
if (!scriptSource) throw new Error('no inline script found in rendered page');
const renderedElements = parsePageElements(html);

function cloneStub(stub) {
  const clone = makeStub(stub.id, {
    tag: stub.tagName,
    value: stub.value,
    checked: stub.checked,
    max: stub.max,
    min: stub.min,
  });
  clone._html = stub._html || '';
  clone._text = stub._text || '';
  return clone;
}

function buildApi() {
  // Each scenario starts from the page exactly as rendered: no control state
  // may leak from a previous scenario.
  const elements = new Map();
  for (const [id, stub] of renderedElements) elements.set(id, cloneStub(stub));
  const document = {
    getElementById(id) {
      if (!elements.has(id)) elements.set(id, makeStub(id, {}));
      return elements.get(id);
    },
    querySelectorAll() { return []; },
    querySelector() { return null; },
    createElement(tag) { return makeStub('created-' + tag, { tag }); },
    addEventListener() {},
    body: { appendChild() {}, removeChild() {} },
    documentElement: {},
  };
  const window = { addEventListener() {}, location: { hash: '' } };
  const location = { hash: '' };
  const history = { replaceState() {} };
  const PixivUI = {
    toast() {},
    confirm: async () => true,
    openDialog() {},
  };
  const fetchStub = async () => ({ ok: true, json: async () => ({}), blob: async () => ({}) });
  const navigator = { userAgent: 'probe' };
  const URLStub = { createObjectURL: () => 'blob:probe', revokeObjectURL() {} };
  const factory = new Function(
    'document', 'window', 'location', 'history', 'PixivUI', 'fetch', 'navigator', 'URL', 'Blob',
    scriptSource + `
    return {
      collectConfig,
      togglePoolProvider,
      toggleClassificationBackend,
      renderSearchProviderSelection,
      openProviderDialog,
      saveProviderDialog,
      initialConfig: () => initialConfig,
    };`,
  );
  return {
    api: factory(document, window, location, history, PixivUI, fetchStub, navigator, URLStub, function Blob() {}),
    document,
  };
}

function snapshotPools(document) {
  const ids = ['native_jev_exa_selection', 'native_jev_tavily_selection', 'brave_provider_selection',
    'tavily_provider_selection', 'exa_provider_selection'];
  const out = {};
  for (const id of ids) {
    const node = document.getElementById(id);
    out[id] = node ? node.innerHTML : null;
  }
  return out;
}

const results = [];
for (const scenario of scenarios) {
  const { api, document } = buildApi();
  const errors = [];
  try {
    for (const [id, value] of Object.entries(scenario.set || {})) {
      document.getElementById(id).value = String(value);
    }
    for (const [id, value] of Object.entries(scenario.check || {})) {
      document.getElementById(id).checked = Boolean(value);
    }
    if (scenario.backend !== undefined) {
      document.getElementById('tag_classifier_backend').value = String(scenario.backend);
      if (typeof api.toggleClassificationBackend === 'function') api.toggleClassificationBackend();
    }
    // Provider rename through the real dialog handler (the shim has no events,
    // so the handler is invoked directly; the browser run covers the real UI).
    for (const rename of scenario.renameProvider || []) {
      api.openProviderDialog(rename.from);
      document.getElementById('provider_dialog_name').value = rename.to;
      api.saveProviderDialog({ preventDefault() {} });
    }
    for (const pool of scenario.togglePool || []) {
      api.togglePoolProvider(pool.path, pool.name, pool.enabled !== false);
    }
    const payload = api.collectConfig();
    results.push({ name: scenario.name, payload, poolHtml: snapshotPools(document), errors });
  } catch (error) {
    results.push({ name: scenario.name, payload: null, error: String(error && error.stack || error) });
  }
}

process.stdout.write(JSON.stringify({
  scriptBytes: scriptSource.length,
  results,
}));
