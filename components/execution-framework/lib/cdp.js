'use strict';
/**
 * CDP / DOM helpers. Pure formatting (formatSnapshot) is separated from the in-page collector so it can be unit tested.
 * Snapshots are what the LLM sees (resilience + agent): compact, truncated, and redaction-aware - form VALUES are never
 * included (only whether one is present), query strings are masked, no raw page text beyond labels/headings.
 */

/**
 * Runs INSIDE the browser (serialised by Playwright) - must be fully self-contained.
 * Returns {url, title, nodes:[{i,tag,role,type,text,name,selector,href,disabled,checked,hasValue,placeholder}]}.
 */
function collectNodesInPage(maxNodes) {
  var SEL = 'a[href],button,input,select,textarea,[role=button],[role=link],[role=checkbox],[role=menuitem],[role=tab],[onclick],summary,h1,h2,h3,label,th';
  function cssEsc(s) {
    return (window.CSS && window.CSS.escape) ? window.CSS.escape(String(s)) : String(s).replace(/[^a-zA-Z0-9_-]/g, '\\$&');
  }
  function isVisible(el) {
    var r = el.getBoundingClientRect();
    var st = window.getComputedStyle(el);
    return r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none';
  }
  function count(sel) {
    try { return document.querySelectorAll(sel).length; } catch (e) { return 0; }
  }
  function selectorFor(el) {
    if (el.id && count('#' + cssEsc(el.id)) === 1) return '#' + cssEsc(el.id);
    var attrs = ['data-testid', 'data-test', 'data-qa', 'name', 'aria-label'];
    for (var a = 0; a < attrs.length; a++) {
      var v = el.getAttribute(attrs[a]);
      if (v) {
        var s = el.tagName.toLowerCase() + '[' + attrs[a] + '="' + v.replace(/"/g, '\\"') + '"]';
        if (count(s) === 1) return s;
      }
    }
    var parts = [];
    var cur = el;
    while (cur && cur.nodeType === 1 && cur !== document.documentElement && parts.length < 6) {
      var part = cur.tagName.toLowerCase();
      if (cur.id && count('#' + cssEsc(cur.id)) === 1) { parts.unshift('#' + cssEsc(cur.id)); break; }
      var sibs = [];
      if (cur.parentElement) {
        for (var c = 0; c < cur.parentElement.children.length; c++) {
          if (cur.parentElement.children[c].tagName === cur.tagName) sibs.push(cur.parentElement.children[c]);
        }
      }
      if (sibs.length > 1) part += ':nth-of-type(' + (sibs.indexOf(cur) + 1) + ')';
      parts.unshift(part);
      cur = cur.parentElement;
    }
    return parts.join(' > ');
  }
  var els = document.querySelectorAll(SEL);
  var nodes = [];
  for (var i = 0; i < els.length && nodes.length < maxNodes; i++) {
    var el = els[i];
    if (!isVisible(el)) continue;
    var tag = el.tagName.toLowerCase();
    var type = el.getAttribute('type') || '';
    var text = '';
    if (tag === 'input' && (type === 'submit' || type === 'button')) text = el.getAttribute('value') || '';
    else if (type !== 'password' && tag !== 'input' && tag !== 'textarea' && tag !== 'select') text = (el.innerText || el.textContent || '');
    text = text.replace(/\s+/g, ' ').trim().slice(0, 80);
    var hasValue = false;
    if (tag === 'input' || tag === 'textarea' || tag === 'select') {
      if (type !== 'submit' && type !== 'button' && type !== 'checkbox' && type !== 'radio') hasValue = !!el.value;
    }
    nodes.push({
      i: nodes.length + 1, tag: tag, role: el.getAttribute('role') || '', type: type, text: text,
      name: el.getAttribute('aria-label') || el.getAttribute('name') || el.getAttribute('placeholder') || el.getAttribute('title') || '',
      selector: selectorFor(el), href: tag === 'a' ? (el.getAttribute('href') || '').split('?')[0].slice(0, 120) : '',
      disabled: !!el.disabled, checked: (type === 'checkbox' || type === 'radio') ? !!el.checked : null, hasValue: hasValue,
    });
  }
  return { url: location.href, title: document.title, nodes: nodes };
}

/** Mask query-string values and fragments (tokens often live there). */
function safeUrl(url) {
  try {
    const u = new URL(url);
    const q = [...u.searchParams.keys()].map((k) => `${k}=…`).join('&');
    return `${u.origin}${u.pathname}${q ? `?${q}` : ''}`;
  } catch (_) {
    return String(url || '').split(/[?#]/)[0];
  }
}

function formatNode(n) {
  const bits = [`[${n.i}] ${n.tag}`];
  if (n.role) bits.push(`role=${n.role}`);
  if (n.type) bits.push(`type=${n.type}`);
  if (n.text) bits.push(JSON.stringify(n.text));
  if (n.name && n.name !== n.text) bits.push(`name=${JSON.stringify(n.name)}`);
  if (n.href) bits.push(`href=${n.href}`);
  if (n.hasValue) bits.push('value=[REDACTED]');
  if (n.checked !== null && n.checked !== undefined) bits.push(n.checked ? 'checked' : 'unchecked');
  if (n.disabled) bits.push('disabled');
  bits.push(`sel=${n.selector}`);
  return bits.join(' ');
}

/** Pure: format collected nodes into the compact text the LLM sees, truncated to maxChars. */
function formatSnapshot(raw, { maxChars = 12000 } = {}) {
  const head = `URL: ${safeUrl(raw.url)}\nTITLE: ${String(raw.title || '').slice(0, 120)}\n`;
  let out = head;
  let shown = 0;
  for (const n of raw.nodes || []) {
    const line = `${formatNode(n)}\n`;
    if (out.length + line.length > maxChars - 60) break;
    out += line;
    shown += 1;
  }
  const total = (raw.nodes || []).length;
  if (shown < total) out += `... (${total - shown} more elements truncated)\n`;
  return out.slice(0, maxChars);
}

async function domSnapshot(page, opts = {}) {
  const maxChars = opts.maxChars || 12000;
  const raw = await page.evaluate(collectNodesInPage, opts.maxNodes || 400);
  return formatSnapshot(raw, { maxChars });
}

async function screenshotB64(page, opts = {}) {
  const buf = await page.screenshot({ type: 'png', fullPage: Boolean(opts.fullPage) });
  return Buffer.from(buf).toString('base64');
}

async function networkIdle(page, { timeoutMs = 10000 } = {}) {
  try {
    await page.waitForLoadState('networkidle', { timeout: timeoutMs });
    return true;
  } catch (_) {
    return false;
  }
}

/** Thin CDP session helper (Chromium only). */
async function createCdp(page) {
  const session = await page.context().newCDPSession(page);
  return {
    session,
    send: (method, params) => session.send(method, params || {}),
    accessibilityTree: async () => (await session.send('Accessibility.getFullAXTree')).nodes,
    clearCookies: async () => session.send('Network.clearBrowserCookies'),
    clearCache: async () => session.send('Network.clearBrowserCache'),
    setOffline: async (offline) => {
      await session.send('Network.enable');
      return session.send('Network.emulateNetworkConditions', { offline: Boolean(offline), latency: 0, downloadThroughput: -1, uploadThroughput: -1 });
    },
    screenshotB64: async () => (await session.send('Page.captureScreenshot', { format: 'png' })).data,
    detach: async () => session.detach(),
  };
}

async function clearCookies(context) {
  await context.clearCookies();
}

module.exports = { collectNodesInPage, formatSnapshot, formatNode, safeUrl, domSnapshot, screenshotB64, networkIdle, createCdp, clearCookies };
