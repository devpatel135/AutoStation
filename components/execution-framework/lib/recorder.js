'use strict';
/**
 * Recorder -> payload: converts Playwright codegen JavaScript (library script or @playwright/test body, legacy or modern
 * locator API) into an AUTOSTATION Node payload. PURE: text in, files out (no I/O except writeProject, which takes an fs).
 *
 * What the converter does (and refuses to do):
 *  - actions become resilient calls  `await rp.click((p) => p.getByRole(...))`  (selector self-healing, exact chain preserved)
 *  - fill/selectOption literals become schedule params (AUTOMATION_PARAM_*), listed in params.suggested.yaml
 *  - anything that looks like a password / token / SSN / card / OTP is NEVER written into the payload: the step becomes an
 *    explicit `throw new Error('credential required: use the user profile / Credential Manager')`
 *  - sso-detect assertions follow every navigation; repeated row patterns become a runItem loop; irreversible-looking
 *    clicks are skipped in --dry-run; assertions/sleeps/unknown statements become comments + warnings (never silently dropped)
 */
const nodeFs = require('node:fs');
const path = require('node:path');

const SENSITIVE_NAME = /(?:^|[^a-z])pass(?:word|wd|code|phrase)?s?(?![a-z])|pwd|secret|token|api[-_ ]?key|ssn|social[-_ ]?security|tax[-_ ]?id|\bcard\b|cvv|cvc|iban|account[-_ ]?number|routing|\botp\b|one[-_ ]time|\bmfa\b|\b2fa\b|verification[-_ ]code|security[-_ ]code|\bpin\b|type\s*=\s*["']?password/i;
const IRREVERSIBLE = /submit|save|send|delete|remove|confirm|pay\b|payment|approve|publish|checkout|\border\b|\bbuy\b|transfer|\bpost\b|\bsign\s*off\b/i;
const HEALABLE = new Set(['click', 'dblclick', 'fill', 'selectOption', 'check', 'uncheck', 'hover', 'press']);
const ACTIONS = new Set(['click', 'dblclick', 'fill', 'selectOption', 'check', 'uncheck', 'hover', 'press', 'setInputFiles', 'focus', 'blur', 'type', 'clear', 'dispatchEvent', 'tap', 'dragTo', 'scrollIntoViewIfNeeded']);
const RESERVED_PARAMS = new Set(['headless', 'channel', 'timeout_s']);
const CREDENTIAL_THROW = "throw new Error('credential required: use the user profile / Credential Manager');";

// ---- lexical scanning ------------------------------------------------------------------------------------
/** Find string / template / regex / comment ranges. -> [{start, end (exclusive), type: 'str'|'tpl'|'regex'|'comment'}] */
function scanLiterals(src) {
  const lits = [];
  let prev = '';
  let i = 0;
  const n = src.length;
  const regexAllowedAfter = '(,=:[!&|?{};';
  while (i < n) {
    const c = src[i];
    const c2 = src[i + 1];
    if (c === '/' && c2 === '/') {
      let j = i;
      while (j < n && src[j] !== '\n') j += 1;
      lits.push({ start: i, end: j, type: 'comment' });
      i = j;
      continue;
    }
    if (c === '/' && c2 === '*') {
      const j = src.indexOf('*/', i + 2);
      const end = j < 0 ? n : j + 2;
      lits.push({ start: i, end, type: 'comment' });
      i = end;
      continue;
    }
    if (c === '"' || c === "'" || c === '`') {
      let j = i + 1;
      while (j < n && src[j] !== c) j += (src[j] === '\\' ? 2 : 1);
      const end = Math.min(n, j + 1);
      lits.push({ start: i, end, type: c === '`' ? 'tpl' : 'str' });
      prev = c;
      i = end;
      continue;
    }
    if (c === '/' && (prev === '' || regexAllowedAfter.includes(prev))) {
      let j = i + 1;
      let inClass = false;
      while (j < n) {
        if (src[j] === '\\') { j += 2; continue; }
        if (src[j] === '[') inClass = true;
        else if (src[j] === ']') inClass = false;
        else if (src[j] === '/' && !inClass) break;
        j += 1;
      }
      let end = Math.min(n, j + 1);
      while (end < n && /[a-z]/i.test(src[end])) end += 1;
      lits.push({ start: i, end, type: 'regex' });
      prev = ')';
      i = end;
      continue;
    }
    if (!/\s/.test(c)) prev = c;
    i += 1;
  }
  return lits;
}

/** Same-length copy where literal contents/comments are blanked, so structural scans can ignore them. */
function mask(src, lits = scanLiterals(src)) {
  const chars = src.split('');
  for (const l of lits) {
    if (l.type === 'comment') {
      for (let k = l.start; k < l.end; k += 1) if (chars[k] !== '\n') chars[k] = ' ';
    } else {
      const innerStart = l.start + 1;
      const innerEnd = l.type === 'regex' ? src.lastIndexOf('/', l.end - 1) : l.end - 1;
      for (let k = innerStart; k < innerEnd; k += 1) chars[k] = '_';
    }
  }
  return chars.join('');
}

function matchBrace(src, openIdx) {
  const m = mask(src);
  let depth = 0;
  for (let i = openIdx; i < m.length; i += 1) {
    if (m[i] === '{') depth += 1;
    else if (m[i] === '}') { depth -= 1; if (depth === 0) return i; }
  }
  return -1;
}

/** The recorded actions, wherever codegen put them. -> {body, variant} */
function extractBody(text) {
  const t = String(text || '');
  const test = /async\s*\(\s*\{[^}]*\}\s*\)\s*=>\s*\{/.exec(t);
  if (test) {
    const open = test.index + test[0].length - 1;
    const close = matchBrace(t, open);
    return { body: t.slice(open + 1, close < 0 ? t.length : close), variant: 'playwright-test' };
  }
  const lib = /const\s+page\s*=\s*await\s+context\.newPage\(\)\s*;/.exec(t);
  if (lib) {
    let rest = t.slice(lib.index + lib[0].length);
    const cut = rest.search(/\/\/\s*-{5,}|await\s+context\.close\(\)|await\s+browser\.close\(\)/);
    if (cut >= 0) rest = rest.slice(0, cut);
    return { body: rest, variant: 'playwright-library' };
  }
  return { body: t, variant: 'statements' };
}

function splitStatements(body) {
  const lits = scanLiterals(body);
  const m = mask(body, lits);
  const out = [];
  let depth = 0;
  let start = 0;
  const push = (a, b) => {
    let x = a;
    let y = b;
    while (x < y && /\s/.test(m[x])) x += 1;
    while (y > x && /\s/.test(m[y - 1])) y -= 1;
    if (y > x) out.push({ text: body.slice(x, y), line: body.slice(0, x).split('\n').length });
  };
  for (let i = 0; i < m.length; i += 1) {
    const c = m[i];
    if (c === '(' || c === '[' || c === '{') depth += 1;
    else if (c === ')' || c === ']' || c === '}') depth -= 1;
    else if (c === ';' && depth === 0) { push(start, i); start = i + 1; }
  }
  push(start, m.length);
  return out;
}

function splitTopLevel(text, sep) {
  const m = mask(text);
  const parts = [];
  let depth = 0;
  let start = 0;
  for (let i = 0; i < m.length; i += 1) {
    const c = m[i];
    if (c === '(' || c === '[' || c === '{') depth += 1;
    else if (c === ')' || c === ']' || c === '}') depth -= 1;
    else if (c === sep && depth === 0) { parts.push(text.slice(start, i)); start = i + 1; }
  }
  parts.push(text.slice(start));
  return parts;
}

function unquote(raw) {
  const q = raw[0];
  if (!['"', "'", '`'].includes(q) || raw[raw.length - 1] !== q || raw.length < 2) return null;
  if (q === '`' && raw.includes('${')) return null;
  let out = '';
  for (let i = 1; i < raw.length - 1; i += 1) {
    const c = raw[i];
    if (c !== '\\') { out += c; continue; }
    const nx = raw[i + 1];
    i += 1;
    if (nx === 'n') out += '\n';
    else if (nx === 't') out += '\t';
    else if (nx === 'r') out += '\r';
    else if (nx === 'u') { out += String.fromCharCode(parseInt(raw.slice(i + 1, i + 5), 16)); i += 4; } else out += nx;
  }
  return out;
}

/**
 * JS single-quoted literal for any string. Everything that could end the literal, start a new line, or terminate an enclosing
 * <script> is escaped (control chars, U+2028/U+2029, `<`), so the result is always exactly one inert string token.
 */
function jsStr(s) {
  const body = String(s).replace(/[\\'\u0000-\u001f\u007f\u2028\u2029<]/g, (c) => {
    if (c === '\\') return '\\\\';
    if (c === "'") return "\\'";
    if (c === '\n') return '\\n';
    if (c === '\r') return '\\r';
    if (c === '\t') return '\\t';
    return `\\u${c.charCodeAt(0).toString(16).padStart(4, '0')}`;
  });
  return `'${body}'`;
}

/** JSON.stringify whose output is also a safe JS expression (U+2028/U+2029 and `<` are escaped, which JSON itself does not do). */
function safeJson(v) {
  return JSON.stringify(v).replace(/[\u2028\u2029<]/g, (c) => `\\u${c.charCodeAt(0).toString(16).padStart(4, '0')}`);
}

/** Text that is safe inside a `//` or `/** *\/` comment: no line terminators, no comment delimiters, bounded length. */
function commentText(s, max = 200) {
  let t = String(s === undefined || s === null ? '' : s).replace(/[\u0000-\u001f\u007f\u2028\u2029]+/g, ' ');
  while (/\*\/|\/\*/.test(t)) t = t.replace(/\*\//g, '').replace(/\/\*/g, '');
  t = t.trim();
  return t.length > max ? `${t.slice(0, max)}...` : t;
}

// ---- strict literal-argument parser -----------------------------------------------------------------------
// Recorded text is untrusted (a codegen file can come from anywhere). Nothing from it is ever copied into generated JS verbatim:
// arguments are parsed with this tiny grammar (strings, numbers, true/false/null, arrays, allow-listed-key objects, regex
// literals) and RE-EMITTED canonically. Anything outside the grammar - calls, identifiers, operators, templates - is rejected.
const OPTION_KEYS = new Set(['timeout', 'force', 'delay', 'button', 'clickCount', 'position', 'modifiers', 'noWaitAfter', 'trial']);
const LOCATOR_KEYS = new Set(['name', 'exact', 'hasText', 'hasNotText', 'level', 'checked', 'disabled', 'expanded', 'pressed', 'selected', 'includeHidden']);
const SELECT_KEYS = new Set(['value', 'label', 'index']);
const NAV_KEYS = new Set(['timeout', 'waitUntil']);
const ALL_KEYS = new Set([...OPTION_KEYS, ...LOCATOR_KEYS, ...SELECT_KEYS, ...NAV_KEYS, 'x', 'y']);
const MAX_ARG_TEXT = 4000;

function parseLiteralArgs(text, { regex = false } = {}) {
  const src = String(text === undefined || text === null ? '' : text);
  if (src.length > MAX_ARG_TEXT) return null;
  let i = 0;
  let budget = 200;
  const ws = () => { while (i < src.length && /[ \t\r\n]/.test(src[i])) i += 1; };
  const fail = () => { throw new Error('literal-args'); };
  const value = (depth) => {
    budget -= 1;
    if (budget < 0 || depth > 4) fail();
    ws();
    const c = src[i];
    if (c === "'" || c === '"') {
      i += 1;
      let out = '';
      for (;;) {
        if (i >= src.length) fail();
        const ch = src[i];
        if (ch === c) { i += 1; return { t: 'str', v: out }; }
        if (/[\n\r\u2028\u2029]/.test(ch)) fail();
        if (ch !== '\\') { out += ch; i += 1; continue; }
        const nx = src[i + 1];
        if (nx === undefined || /[\n\r\u2028\u2029]/.test(nx)) fail();
        i += 2;
        if (nx === 'n') out += '\n';
        else if (nx === 't') out += '\t';
        else if (nx === 'r') out += '\r';
        else if (nx === 'u') {
          const hex = src.slice(i, i + 4);
          if (!/^[0-9a-fA-F]{4}$/.test(hex)) fail();
          out += String.fromCharCode(parseInt(hex, 16));
          i += 4;
        } else if (nx === 'x') {
          const hex = src.slice(i, i + 2);
          if (!/^[0-9a-fA-F]{2}$/.test(hex)) fail();
          out += String.fromCharCode(parseInt(hex, 16));
          i += 2;
        } else if (/[0-9bfv]/.test(nx)) fail();
        else out += nx;
      }
    }
    if (c === '-' || (c >= '0' && c <= '9')) {
      const m = /^-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?/.exec(src.slice(i));
      if (!m) fail();
      i += m[0].length;
      const v = Number(m[0]);
      if (!Number.isFinite(v)) fail();
      return { t: 'num', v };
    }
    if (c === '/' && regex) {
      let j = i + 1;
      let inClass = false;
      let body = '';
      for (;;) {
        if (j >= src.length) fail();
        const ch = src[j];
        if (/[\n\r\u2028\u2029'"`]/.test(ch)) fail();
        if (ch === '\\') {
          const nx = src[j + 1];
          if (nx === undefined || /[\n\r\u2028\u2029'"`]/.test(nx)) fail();
          body += ch + nx;
          j += 2;
          continue;
        }
        if (ch === '[') inClass = true;
        else if (ch === ']') inClass = false;
        else if (ch === '/' && !inClass) break;
        body += ch;
        j += 1;
      }
      const fm = /^[dgimsuvy]*/.exec(src.slice(j + 1));
      const flags = fm[0];
      if (!body || body[0] === '*') fail();
      try { new RegExp(body, flags); } catch (_) { fail(); } // eslint-disable-line no-new
      i = j + 1 + flags.length;
      return { t: 're', body, flags };
    }
    if (c === '[') {
      i += 1;
      const items = [];
      for (;;) {
        ws();
        if (src[i] === ']') { i += 1; return { t: 'arr', items }; }
        items.push(value(depth + 1));
        ws();
        if (src[i] === ',') i += 1;
        else if (src[i] !== ']') fail();
      }
    }
    if (c === '{') {
      i += 1;
      const entries = [];
      for (;;) {
        ws();
        if (src[i] === '}') { i += 1; return { t: 'obj', entries }; }
        let key;
        if (src[i] === "'" || src[i] === '"') key = value(depth + 1).v;
        else {
          const km = /^[A-Za-z_$][\w$]*/.exec(src.slice(i));
          if (!km) fail();
          key = km[0];
          i += key.length;
        }
        if (!ALL_KEYS.has(key)) fail();
        ws();
        if (src[i] !== ':') fail();
        i += 1;
        entries.push([key, value(depth + 1)]);
        ws();
        if (src[i] === ',') i += 1;
        else if (src[i] !== '}') fail();
      }
    }
    const im = /^[A-Za-z_$][\w$]*/.exec(src.slice(i));
    if (im && (im[0] === 'true' || im[0] === 'false' || im[0] === 'null')) {
      i += im[0].length;
      return { t: im[0] === 'null' ? 'null' : 'bool', v: im[0] === 'true' };
    }
    return fail();
  };
  try {
    const list = [];
    ws();
    while (i < src.length) {
      list.push(value(0));
      ws();
      if (i < src.length) {
        if (src[i] !== ',') fail();
        i += 1;
        ws();
      }
    }
    return list;
  } catch (err) {
    if (err.message === 'literal-args') return null;
    throw err;
  }
}

function emitLiteral(node) {
  switch (node.t) {
    case 'str': return jsStr(node.v);
    case 'num': return String(node.v);
    case 'bool': return node.v ? 'true' : 'false';
    case 'null': return 'null';
    case 're': return `/${node.body}/${node.flags}`;
    case 'arr': return `[${node.items.map(emitLiteral).join(', ')}]`;
    case 'obj': return node.entries.length ? `{ ${node.entries.map(([k, v]) => `${k}: ${emitLiteral(v)}`).join(', ')} }` : '{}';
    default: throw new Error('cannot emit literal');
  }
}

/** Strict shape check of a Playwright options object: only allow-listed keys with correctly typed literal values. */
function optionsOk(node, keys = OPTION_KEYS) {
  if (!node || node.t !== 'obj') return false;
  return node.entries.every(([k, v]) => {
    if (!keys.has(k)) return false;
    switch (k) {
      case 'timeout': case 'delay': case 'clickCount': case 'x': case 'y': case 'index': case 'level': return v.t === 'num';
      case 'force': case 'noWaitAfter': case 'trial': case 'exact': case 'checked': case 'disabled': case 'expanded': case 'pressed': case 'selected': case 'includeHidden': return v.t === 'bool';
      case 'button': return v.t === 'str' && /^(?:left|right|middle)$/.test(v.v);
      case 'modifiers': return v.t === 'arr' && v.items.every((m) => m.t === 'str' && /^(?:Alt|Control|ControlOrMeta|Meta|Shift)$/.test(m.v));
      case 'position': return v.t === 'obj' && v.entries.every(([pk, pv]) => (pk === 'x' || pk === 'y') && pv.t === 'num');
      case 'waitUntil': return v.t === 'str' && /^(?:load|domcontentloaded|networkidle|commit)$/.test(v.v);
      default: return v.t === 'str' || v.t === 're';
    }
  });
}

const LOCATOR_SEGS = new Set(['getByRole', 'getByLabel', 'getByText', 'getByPlaceholder', 'getByAltText', 'getByTitle', 'getByTestId', 'locator', 'frameLocator', 'contentFrame', 'filter', 'first', 'last', 'nth']);

/** Re-render one locator-chain segment from parsed literals, or null if it is not a plain locator call. */
function canonSeg(seg) {
  if (!LOCATOR_SEGS.has(seg.name)) return null;
  if (seg.name === 'first' || seg.name === 'last' || seg.name === 'contentFrame') {
    return seg.args === null || seg.args === '' ? { ...seg, args: '', src: `${seg.name}()` } : null;
  }
  if (seg.args === null) return null;
  const list = parseLiteralArgs(seg.args, { regex: true });
  if (!list || list.length < 1 || list.length > 2) return null;
  const [a, b] = list;
  if (seg.name === 'nth') { if (list.length !== 1 || a.t !== 'num' || !Number.isInteger(a.v)) return null; } else if (seg.name === 'filter') {
    if (list.length !== 1 || !optionsOk(a, LOCATOR_KEYS)) return null;
  } else if (seg.name === 'getByRole') {
    if (a.t !== 'str' || (b && !optionsOk(b, LOCATOR_KEYS))) return null;
  } else if (seg.name === 'locator') {
    if (a.t !== 'str' || (b && !optionsOk(b, LOCATOR_KEYS))) return null;
  } else if (seg.name === 'frameLocator') {
    if (list.length !== 1 || a.t !== 'str') return null;
  } else if ((a.t !== 'str' && a.t !== 're') || (b && !optionsOk(b, LOCATOR_KEYS))) return null; // getByText/getByLabel/...
  const args = list.map(emitLiteral).join(', ');
  return { ...seg, args, src: `${seg.name}(${args})` };
}

const SAFE_VAR = /^[A-Za-z][A-Za-z0-9_]{0,30}$/;
// names the generated main.js already uses (or that would be dangerous to shadow) can never become page variables
const RESERVED_IDENTS = new Set(['require', 'process', 'module', 'exports', 'fw', 'framework', 'w', 'sso', 'P', 'L', 'rp', 'session', 'path', 'NAME', 'eval',
  'Function', 'constructor', 'globalThis', 'global', 'console', 'Buffer', 'import', 'await', 'async', 'undefined', 'arguments', 'ROWS', 'row']);
const OPTIONS_ONLY = new Set(['click', 'dblclick', 'check', 'uncheck', 'hover', 'focus', 'blur', 'clear', 'tap', 'scrollIntoViewIfNeeded']);

/**
 * Re-render an action's argument text from parsed literals. -> {text, dropped} or null when the step cannot be kept.
 * Option-only methods (click, hover, ...) drop a non-literal / non-allow-listed options argument (dropped=true) and keep the action.
 */
function sanitizeActionArgs(method, argsText) {
  const raw = String(argsText || '').trim();
  const list = parseLiteralArgs(raw);
  if (OPTIONS_ONLY.has(method)) {
    if (list && list.length === 0) return { text: '', dropped: false };
    if (list && list.length === 1 && optionsOk(list[0])) return { text: emitLiteral(list[0]), dropped: false };
    return { text: '', dropped: true };
  }
  if (!list || list.length < 1 || list.length > 2) return null;
  const [a, b] = list;
  if (b && !optionsOk(b)) return null;
  switch (method) {
    case 'fill': case 'press': case 'type': case 'dispatchEvent':
      if (a.t !== 'str' || (method === 'dispatchEvent' && b)) return null;
      break;
    case 'selectOption':
      if (!(a.t === 'str' || (a.t === 'arr' && a.items.every((x) => x.t === 'str')) || (a.t === 'obj' && optionsOk(a, SELECT_KEYS)))) return null;
      break;
    case 'setInputFiles':
      if (!(a.t === 'str' || (a.t === 'arr' && a.items.every((x) => x.t === 'str')))) return null;
      break;
    default: return null; // dragTo (takes a locator expression) and anything else
  }
  return { text: list.map(emitLiteral).join(', '), dropped: false };
}

function canonLocSegs(segs) {
  const out = [];
  for (const s of segs) {
    const c = canonSeg(s);
    if (!c) return null;
    out.push(c);
  }
  return out;
}

function parseChain(expr) {
  const parts = splitTopLevel(expr.trim(), '.');
  if (parts.length < 2) return null;
  const base = parts[0].trim();
  if (!/^[A-Za-z_$][\w$]*$/.test(base)) return null;
  const segs = [];
  for (const raw of parts.slice(1)) {
    const mm = /^\s*([A-Za-z_$][\w$]*)\s*(?:\(([\s\S]*)\))?\s*$/.exec(raw);
    if (!mm) return null;
    segs.push({ name: mm[1], args: mm[2] === undefined ? null : mm[2].trim(), src: raw.trim() });
  }
  return { base, segs };
}

function stringLiterals(text) {
  const out = [];
  for (const l of scanLiterals(text)) if (l.type === 'str' || l.type === 'tpl') { const v = unquote(text.slice(l.start, l.end)); if (v !== null) out.push(v); }
  return out;
}

function describeSeg(seg) {
  const lits = stringLiterals(seg.args || '');
  const first = lits[0] !== undefined ? lits[0] : String(seg.args || '').split(',')[0].trim();
  switch (seg.name) {
    case 'getByRole': return lits.length > 1 && /name\s*:/.test(seg.args) ? `${lits[0]} "${lits[1]}"` : String(lits[0] || 'role');
    case 'getByLabel': return `label "${first}"`;
    case 'getByText': return `text "${first}"`;
    case 'getByPlaceholder': return `placeholder "${first}"`;
    case 'getByAltText': return `alt "${first}"`;
    case 'getByTitle': return `title "${first}"`;
    case 'getByTestId': return `test-id "${first}"`;
    case 'locator': return lits[0] !== undefined ? lits[0] : (seg.args || '');
    case 'frameLocator': return `frame ${lits[0] || ''}`.trim();
    case 'contentFrame': return 'iframe content';
    case 'filter': return `filter ${lits.length ? `"${lits[0]}"` : (seg.args || '')}`;
    case 'first': return 'first';
    case 'last': return 'last';
    case 'nth': return `#${(seg.args || '').trim()}`;
    default: return seg.src;
  }
}

function slug(s, max = 30) {
  return String(s).toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '').slice(0, max);
}

function looksSensitiveValue(v) {
  const s = String(v);
  if (/^\d{3}-\d{2}-\d{4}$/.test(s)) return true; // SSN
  const digits = s.replace(/[ -]/g, '');
  if (/^\d{13,19}$/.test(digits) && luhn(digits)) return true; // card
  if (/^eyJ[\w-]{8,}\.[\w-]{8,}\.[\w-]{8,}$/.test(s)) return true; // JWT
  if (/^Bearer\s+\S{8,}/i.test(s)) return true;
  if (/^[A-Fa-f0-9]{32,}$/.test(s)) return true; // long hex secret
  if (s.length >= 20 && !/\s/.test(s) && /[A-Za-z]/.test(s) && /\d/.test(s) && /^[\w+/=.-]+$/.test(s)) return true; // opaque token
  return false;
}

function luhn(d) {
  let sum = 0;
  let alt = false;
  for (let i = d.length - 1; i >= 0; i -= 1) {
    let n = d.charCodeAt(i) - 48;
    if (alt) { n *= 2; if (n > 9) n -= 9; }
    sum += n;
    alt = !alt;
  }
  return sum % 10 === 0;
}

// ---- statement parsing ------------------------------------------------------------------------------------
function locatorSource(chainSegs) {
  // source relative to `p`: p.getByRole(...).first() ...
  return chainSegs.map((s) => s.src).join('.');
}

function parseStatement(raw, line) {
  let text = raw.trim();
  const base = { line, source: text };
  text = text.replace(/^await\s+/, '');

  let m = /^const\s+(\w+)\s*=\s*(\w+)\.waitForEvent\(\s*['"]popup['"]\s*\)$/.exec(text);
  if (m) return { ...base, kind: 'popup-wait', promiseVar: m[1], from: m[2], pass: raw.trim().replace(/^await\s+/, '') };
  m = /^const\s+(\w+)\s*=\s*(?:await\s+)?(\w+)$/.exec(text);
  if (m && /Promise$/.test(m[2])) return { ...base, kind: 'popup-bind', pageVar: m[1], promiseVar: m[2] };

  if (/^expect\s*\(/.test(text)) return { ...base, kind: 'assert' };

  const chain = parseChain(text);
  if (!chain) return { ...base, kind: 'unknown' };
  const { base: pv, segs } = chain;
  const last = segs[segs.length - 1];

  if (segs.length === 1) {
    if (last.name === 'goto') return { ...base, kind: 'goto', pageVar: pv, argsText: last.args || '' };
    if (last.name === 'waitForURL') return { ...base, kind: 'waitForURL', pageVar: pv, argsText: last.args || '' };
    if (last.name === 'waitForTimeout') return { ...base, kind: 'sleep', pageVar: pv, argsText: last.args || '' };
    if (last.name === 'waitForLoadState') return { ...base, kind: 'loadstate', pageVar: pv, argsText: last.args || '' };
    if (last.name === 'close' || last.name === 'bringToFront') return { ...base, kind: 'noop' };
    if (ACTIONS.has(last.name) && last.args !== null) { // legacy API: page.click('#id'), page.fill('#id', 'x')
      const args = splitTopLevel(last.args, ',');
      const selArg = args[0].trim();
      return makeAction(base, pv, [{ name: 'locator', args: selArg, src: `locator(${selArg})` }], last.name, args.slice(1).join(',').trim());
    }
  }
  if (segs.length === 2 && last.name === 'press' && segs[0].name === 'keyboard') return { ...base, kind: 'keyboard', pageVar: pv, method: 'press', argsText: last.args || '' };
  if (segs.length === 2 && last.name === 'type' && segs[0].name === 'keyboard') return { ...base, kind: 'keyboard', pageVar: pv, method: 'type', argsText: last.args || '' };
  if (segs.length >= 2 && ACTIONS.has(last.name) && last.args !== null) return makeAction(base, pv, segs.slice(0, -1), last.name, last.args);
  return { ...base, kind: 'unknown' };
}

function makeAction(base, pageVar, rawLocSegs, method, argsText) {
  const locSegs = canonLocSegs(rawLocSegs); // locator text is re-rendered from parsed literals, never copied verbatim
  if (!locSegs) return { ...base, kind: 'unknown' };
  const locSrc = locatorSource(locSegs);
  const desc = locSegs.map(describeSeg).join(' > ');
  return { ...base, kind: 'action', pageVar, method, locSegs, locSrc, desc, argsText: argsText || '' };
}

// ---- model building ---------------------------------------------------------------------------------------
function tokenizeLits(src) {
  const parts = [];
  let pos = 0;
  const strLits = scanLiterals(src).filter((l) => l.type === 'str');
  const numRe = /\bnth\(\s*(\d+)\s*\)/g;
  const marks = strLits.map((l) => ({ start: l.start, end: l.end, kind: 'str', raw: src.slice(l.start, l.end) }));
  let nm;
  const masked = mask(src);
  // the role in getByRole('role', ...) is structure ('row' vs 'cell'), never a per-row value
  const roleRe = /getByRole\(\s*(?=['"])/g;
  const roleStarts = new Set();
  while ((nm = roleRe.exec(masked)) !== null) roleStarts.add(nm.index + nm[0].length);
  for (let k = marks.length - 1; k >= 0; k -= 1) if (roleStarts.has(marks[k].start)) marks.splice(k, 1);
  while ((nm = numRe.exec(masked)) !== null) {
    const off = nm[0].indexOf(nm[1]);
    marks.push({ start: nm.index + off, end: nm.index + off + nm[1].length, kind: 'num', raw: nm[1] });
  }
  marks.sort((a, b) => a.start - b.start);
  for (const mk of marks) {
    if (mk.start < pos) continue;
    if (mk.start > pos) parts.push({ t: 'text', s: src.slice(pos, mk.start) });
    parts.push({ t: 'lit', kind: mk.kind, raw: mk.raw, value: mk.kind === 'str' ? unquote(mk.raw) : Number(mk.raw) });
    pos = mk.end;
  }
  if (pos < src.length) parts.push({ t: 'text', s: src.slice(pos) });
  return parts;
}

function renderParts(parts, subst) {
  let k = 0;
  return parts.map((p) => {
    if (p.t === 'text') return p.s;
    const out = subst && subst[k] !== undefined ? subst[k] : p.raw;
    k += 1;
    return out;
  }).join('');
}

function unique(name, used) {
  let n = name || 'field';
  if (RESERVED_PARAMS.has(n)) n = `${n}_value`;
  let cand = n;
  for (let i = 2; used.has(cand); i += 1) cand = `${n}_${i}`;
  used.add(cand);
  return cand;
}

function deriveParamName(step, idx) {
  const names = [];
  for (const s of step.locSegs) {
    const lits = stringLiterals(s.args || '');
    if (s.name === 'getByRole') { if (lits[1]) names.push(lits[1]); else if (lits[0]) names.push(lits[0]); } else if (['getByLabel', 'getByPlaceholder', 'getByTitle', 'getByTestId', 'getByAltText'].includes(s.name)) names.push(lits[0] || '');
    else if (s.name === 'locator' && lits[0]) {
      const attr = /\[(?:name|id)=["']?([^\]"']+)["']?\]/.exec(lits[0]);
      names.push(attr ? attr[1] : lits[0].replace(/^[#.]/, ''));
    }
  }
  const first = names.find((x) => slug(x));
  return slug(first || `field_${idx}`) || `field_${idx}`;
}

/** statements -> steps (+params, warnings). */
function buildModel(statements, { url }) {
  const warnings = [];
  const steps = [];
  const params = [];
  const usedParams = new Set();
  const pageVars = new Set(['page']);
  let gotoSeen = false;
  let fieldN = 0;

  const todo = (s, why) => {
    steps.push({ kind: 'todo', text: String(s.source || '').replace(/^await\s+/, ''), line: s.line });
    warnings.push(`line ${s.line}: ${why} (kept as a TODO comment): ${commentText(s.source, 80)}`);
  };
  const knownVar = (v) => SAFE_VAR.test(v) && !RESERVED_IDENTS.has(v) && pageVars.has(v);

  for (const st of statements) {
    const s = parseStatement(st.text, st.line);
    switch (s.kind) {
      case 'unknown':
        todo(s, 'statement not converted');
        break;
      case 'assert':
        steps.push({ kind: 'comment', text: `recorded assertion (not converted): ${String(s.source).replace(/^await\s+/, '')}`, line: s.line });
        warnings.push(`line ${s.line}: assertion not converted - add an explicit check if the flow depends on it`);
        break;
      case 'noop': break;
      case 'popup-wait':
        if (!SAFE_VAR.test(s.promiseVar) || RESERVED_IDENTS.has(s.promiseVar) || !/Promise$/.test(s.promiseVar) || !knownVar(s.from)
          || RESERVED_IDENTS.has(s.promiseVar.replace(/Promise$/, ''))) { todo(s, 'popup wait on an unexpected variable not converted'); break; }
        pageVars.add(s.promiseVar.replace(/Promise$/, ''));
        steps.push({ kind: 'raw', text: `const ${s.promiseVar} = ${s.from}.waitForEvent('popup');`, line: s.line });
        break;
      case 'popup-bind':
        if (!SAFE_VAR.test(s.pageVar) || RESERVED_IDENTS.has(s.pageVar) || !SAFE_VAR.test(s.promiseVar)) { todo(s, 'popup binding not converted'); break; }
        pageVars.add(s.pageVar);
        steps.push({ kind: 'popup', pageVar: s.pageVar, promiseVar: s.promiseVar, line: s.line });
        break;
      case 'goto': {
        const args = knownVar(s.pageVar) ? parseLiteralArgs(s.argsText) : null;
        const lit = args && args.length >= 1 && args.length <= 2 && args[0].t === 'str' && (!args[1] || optionsOk(args[1], NAV_KEYS)) ? args[0].v : undefined;
        let parsed = null;
        try { parsed = lit !== undefined ? new URL(lit) : null; } catch (_) { parsed = null; }
        if (!parsed || !/^https?:$/.test(parsed.protocol) || parsed.username || parsed.password) {
          todo(s, 'goto not converted (only literal http/https URLs without credentials are accepted)');
          break;
        }
        let urlExpr = jsStr(lit);
        if (!gotoSeen) {
          gotoSeen = true;
          const pn = unique('start_url', usedParams);
          params.push({ name: pn, default: lit, source: 'first navigation', sensitive: false });
          urlExpr = `P.${pn}`;
        }
        steps.push({ kind: 'goto', pageVar: s.pageVar, urlExpr, origin: parsed.origin, desc: lit, line: s.line });
        break;
      }
      case 'waitForURL': {
        const args = knownVar(s.pageVar) ? parseLiteralArgs(s.argsText, { regex: true }) : null;
        if (!args || args.length < 1 || args.length > 2 || (args[0].t !== 'str' && args[0].t !== 're') || (args[1] && !optionsOk(args[1], NAV_KEYS))) { todo(s, 'waitForURL not converted'); break; }
        steps.push({ kind: 'waitForURL', pageVar: s.pageVar, argsText: args.map(emitLiteral).join(', '), line: s.line });
        break;
      }
      case 'sleep':
        if (!knownVar(s.pageVar) || !/^\d{1,7}$/.test(s.argsText.trim())) { todo(s, 'sleep not converted'); break; }
        steps.push({ kind: 'sleep', pageVar: s.pageVar, argsText: s.argsText.trim(), line: s.line });
        warnings.push(`line ${s.line}: fixed sleep (${s.argsText.trim()} ms) replaced by a network-idle wait - fixed sleeps are fragile`);
        break;
      case 'loadstate': {
        const args = knownVar(s.pageVar) ? parseLiteralArgs(s.argsText) : null;
        if (!args || args.length > 2 || (args[0] && (args[0].t !== 'str' || !/^(?:load|domcontentloaded|networkidle)$/.test(args[0].v))) || (args[1] && !optionsOk(args[1], NAV_KEYS))) { todo(s, 'waitForLoadState not converted'); break; }
        steps.push({ kind: 'loadstate', pageVar: s.pageVar, argsText: args.map(emitLiteral).join(', '), line: s.line });
        break;
      }
      case 'keyboard': {
        const args = knownVar(s.pageVar) ? parseLiteralArgs(s.argsText) : null;
        if (!args || args.length < 1 || args.length > 2 || args[0].t !== 'str' || (args[1] && !optionsOk(args[1]))) { todo(s, 'keyboard step not converted'); break; }
        const lit = args[0].v;
        if (s.method === 'type' && (looksSensitiveValue(lit || '') )) {
          steps.push({ kind: 'sensitive', desc: 'keyboard.type (sensitive value)', line: s.line });
          warnings.push(`line ${s.line}: typed value looks like a secret - not recorded`);
        } else steps.push({ kind: 'keyboard', pageVar: s.pageVar, method: s.method, argsText: args.map(emitLiteral).join(', '), desc: `keyboard.${s.method} ${lit}`, line: s.line });
        break;
      }
      case 'action': {
        const step = { ...s };
        if (!knownVar(step.pageVar)) { todo(s, `unknown page variable ${commentText(step.pageVar, 30)}`); break; }
        const sanitized = sanitizeActionArgs(step.method, step.argsText);
        if (!sanitized) { todo(s, `${step.method} arguments are not plain literals`); break; }
        if (sanitized.dropped) {
          steps.push({ kind: 'todo', text: `options dropped (not a plain literal options object): ${step.argsText}`, line: s.line });
          warnings.push(`line ${s.line}: ${step.method} options were not a strict literal options object and were dropped (see TODO)`);
        }
        step.argsText = sanitized.text;
        const valueMethods = step.method === 'fill' || step.method === 'selectOption';
        const argLit = valueMethods ? stringLiterals(step.argsText)[0] : undefined;
        const argRaw = step.argsText.trim();
        const argIsPlainLiteral = argLit !== undefined && /^(['"])[\s\S]*\1$/.test(argRaw);
        step.healable = HEALABLE.has(step.method);
        step.irreversible = (step.method === 'click' || step.method === 'dblclick') && IRREVERSIBLE.test(step.desc);
        if (valueMethods && argIsPlainLiteral) {
          fieldN += 1;
          const named = deriveParamName(step, fieldN);
          const sensitive = SENSITIVE_NAME.test(`${step.desc} ${step.locSrc}`) || looksSensitiveValue(argLit);
          if (sensitive) {
            steps.push({ kind: 'sensitive', desc: `${step.method} ${step.desc}`, line: s.line });
            params.push({ name: unique(named, usedParams), default: null, source: step.desc, sensitive: true });
            warnings.push(`line ${s.line}: "${step.desc}" looks like a credential/secret - NOT recorded; the step throws until you wire in the user profile / Credential Manager`);
            break;
          }
          step.paramName = unique(named, usedParams);
          step.valueLiteral = argLit;
          params.push({ name: step.paramName, default: argLit, source: step.desc, sensitive: false });
        }
        if (step.method === 'setInputFiles') warnings.push(`line ${s.line}: setInputFiles is not self-healing - consider fw.adapters.get('file-upload').uploadFiles (validates the files first)`);
        if (!step.healable && step.method !== 'setInputFiles') warnings.push(`line ${s.line}: ${step.method} is not self-healing (plain Playwright call)`);
        if (step.pageVar !== 'page' && !pageVars.has(step.pageVar)) warnings.push(`line ${s.line}: unknown page variable ${step.pageVar}`);
        steps.push(step);
        break;
      }
      default: break;
    }
  }
  return { steps, params, warnings, pageVars: [...pageVars] };
}

// ---- repeat detection -------------------------------------------------------------------------------------
function stepTemplate(step) {
  const locParts = tokenizeLits(step.locSrc);
  const valueMethods = step.method === 'fill' || step.method === 'selectOption';
  const argRaw = step.argsText.trim();
  const argParts = valueMethods && step.valueLiteral !== undefined ? tokenizeLits(argRaw) : null;
  const litsOf = (parts) => parts.filter((p) => p.t === 'lit');
  const tpl = (parts) => parts.map((p) => (p.t === 'text' ? p.s : '§')).join('');
  const slots = [...litsOf(locParts), ...(argParts ? litsOf(argParts) : [])];
  const template = `${step.pageVar}|${step.method}|${tpl(locParts)}|${argParts ? tpl(argParts) : argRaw}`;
  return { locParts, argParts, slots, template };
}

/** Group consecutive action steps that repeat with different literals (e.g. one block per table row). */
function detectRepeats(steps) {
  const blocks = [];
  const info = steps.map((s) => (s.kind === 'action' ? stepTemplate(s) : null));
  const isAct = (i) => steps[i] && steps[i].kind === 'action' && info[i];
  let i = 0;
  while (i < steps.length) {
    if (!isAct(i)) { blocks.push({ type: 'single', step: steps[i] }); i += 1; continue; }
    let best = null;
    for (let L = 1; L <= 4; L += 1) {
      let ok = true;
      for (let j = 0; j < L; j += 1) if (!isAct(i + j)) ok = false;
      if (!ok) break;
      let k = 1;
      for (;;) {
        let same = true;
        for (let j = 0; j < L; j += 1) {
          if (!isAct(i + k * L + j) || info[i + k * L + j].template !== info[i + j].template) { same = false; break; }
        }
        if (!same) break;
        k += 1;
      }
      if (k >= 2) {
        let varying = false;
        for (let r = 1; r < k && !varying; r += 1) {
          for (let j = 0; j < L && !varying; j += 1) {
            const a = info[i + j].slots.map((x) => x.raw).join('\u0001');
            const b = info[i + r * L + j].slots.map((x) => x.raw).join('\u0001');
            if (a !== b) varying = true;
          }
        }
        if (varying && (!best || k * L > best.k * best.L)) best = { L, k };
      }
    }
    if (!best) { blocks.push({ type: 'single', step: steps[i] }); i += 1; continue; }
    const { L, k } = best;
    const first = steps.slice(i, i + L);
    const firstInfo = info.slice(i, i + L);
    // varying slots: (blockStep j, slot s) whose raw literal differs in any repeat
    const varying = [];
    firstInfo.forEach((fi, j) => fi.slots.forEach((slot, s) => {
      for (let r = 1; r < k; r += 1) if (info[i + r * L + j].slots[s].raw !== slot.raw) { varying.push({ j, s }); return; }
    }));
    const rows = [];
    for (let r = 0; r < k; r += 1) {
      const row = {};
      varying.forEach((v, idx) => { row[`v${idx}`] = info[i + r * L + v.j].slots[v.s].value; });
      rows.push(row);
    }
    blocks.push({ type: 'loop', steps: first, covered: steps.slice(i, i + k * L), infos: firstInfo, varying, rows, repeats: k });
    i += k * L;
  }
  return blocks;
}

// ---- rendering --------------------------------------------------------------------------------------------
const rpVar = (pv) => (pv === 'page' ? 'rp' : `rp_${pv}`);

function locFn(parts, subst) {
  return `(p) => p.${renderParts(parts, subst)}`;
}

function valueExpr(step, argParts, subst) {
  const argVaries = Boolean(subst && subst.arg && Object.keys(subst.arg).length > 0);
  if (step.valueLiteral !== undefined && step.paramName && !argVaries) return `P.${step.paramName}`;
  if (argParts) return renderParts(argParts, subst && subst.arg);
  return step.argsText.trim();
}

function renderAction(step, info, { subst = null, inLoop = false, indent }) {
  const locSubst = subst && subst.loc;
  const loc = locFn(info.locParts, locSubst);
  const rp = rpVar(step.pageVar);
  const lines = [];
  const extra = step.argsText.trim();
  let call;
  if (step.healable) {
    if (step.method === 'fill' || step.method === 'selectOption') call = `await ${rp}.${step.method}(${loc}, ${valueExpr(step, info.argParts, subst)});`;
    else if (step.method === 'press') call = `await ${rp}.press(${loc}, ${extra});`;
    else call = `await ${rp}.${step.method}(${loc}${extra ? `, ${extra}` : ''});`;
  } else {
    const pg = step.pageVar === 'page' ? '' : `, ${step.pageVar}`;
    call = `await L(${loc}${pg}).${step.method}(${extra});`;
  }
  if (step.irreversible) {
    lines.push(`${indent}// irreversible-looking action: skipped in --dry-run`);
    lines.push(`${indent}if (!w.isDryRun()) ${call}`);
  } else lines.push(`${indent}${call}`);
  return lines;
}

function renderMain(model, { name, url, stamp }) {
  const { steps } = model;
  const blocks = detectRepeats(steps);
  // values inside a repeated block come from ROWS_n, so their per-step params are not needed
  // repeats 2..n reuse repeat 1's step, so their per-step params are redundant; a value that VARIES per row comes from ROWS_n
  const inLoopParams = new Set();
  for (const b of blocks) {
    if (b.type !== 'loop') continue;
    b.covered.slice(b.steps.length).forEach((st) => { if (st.paramName) inLoopParams.add(st.paramName); });
    b.steps.forEach((st, j) => {
      const locLits = b.infos[j].locParts.filter((p) => p.t === 'lit').length;
      if (st.paramName && b.varying.some((v) => v.j === j && v.s >= locLits)) inLoopParams.add(st.paramName);
    });
  }
  const params = model.params.filter((p) => !inLoopParams.has(p.name));
  const lines = [];
  const needsL = steps.some((s) => s.kind === 'action' && !s.healable);
  let n = 0;
  let loopN = 0;
  const I = '      ';
  const table = [];
  const emitStep = (step, indent, ctx = {}) => {
    n += 1;
    const label = `step ${n}`;
    switch (step.kind) {
      case 'goto':
        lines.push(`${indent}// ${label}: open ${commentText(step.desc)}`);
        lines.push(`${indent}await ${step.pageVar}.goto(${step.urlExpr}, { waitUntil: 'domcontentloaded' });`);
        lines.push(`${indent}await sso.assertSession(${step.pageVar}, ${step.origin ? `{ expectedOrigin: ${jsStr(step.origin)} }` : '{}'});`);
        table.push([n, 'goto', step.desc, step.urlExpr.startsWith('P.') ? `param ${step.urlExpr.slice(2)}` : '', 'sso-detect assertion added']);
        break;
      case 'waitForURL':
        lines.push(`${indent}// ${label}: wait for URL`);
        lines.push(`${indent}await ${step.pageVar}.waitForURL(${step.argsText});`);
        lines.push(`${indent}await sso.assertSession(${step.pageVar}, {});`);
        table.push([n, 'waitForURL', step.argsText, '', 'sso-detect assertion added']);
        break;
      case 'sleep':
        lines.push(`${indent}// ${label}: recorded fixed sleep of ${step.argsText} ms -> network-idle wait`);
        lines.push(`${indent}await framework.adapters.get('wait-helpers').waitForNetworkIdle(${step.pageVar});`);
        table.push([n, 'sleep', `${step.argsText} ms`, '', 'replaced by network-idle wait']);
        break;
      case 'loadstate':
        lines.push(`${indent}// ${label}: wait for load state`);
        lines.push(`${indent}await ${step.pageVar}.waitForLoadState(${step.argsText});`);
        table.push([n, 'waitForLoadState', step.argsText, '', '']);
        break;
      case 'keyboard':
        lines.push(`${indent}// ${label}: ${commentText(step.desc)}`);
        lines.push(`${indent}await ${step.pageVar}.keyboard.${step.method}(${step.argsText});`);
        table.push([n, `keyboard.${step.method}`, step.desc, '', '']);
        break;
      case 'sensitive':
        lines.push(`${indent}// ${label}: ${commentText(step.desc)} - SENSITIVE: the recorded value was NOT kept`);
        lines.push(`${indent}${CREDENTIAL_THROW}`);
        table.push([n, 'fill', step.desc.replace(/^fill /, ''), '(not recorded)', 'throws until credentials are wired in']);
        break;
      case 'popup':
        lines.push(`${indent}const ${step.pageVar} = await ${step.promiseVar};`);
        lines.push(`${indent}const ${rpVar(step.pageVar)} = framework.resilience.resilientPage(${step.pageVar}, { caller: NAME });`);
        n -= 1;
        break;
      case 'raw':
        lines.push(`${indent}${step.text}`);
        n -= 1;
        break;
      case 'comment':
        lines.push(`${indent}// ${commentText(step.text)}`);
        n -= 1;
        break;
      case 'todo':
        lines.push(`${indent}// TODO(unconverted): ${commentText(step.text)}`);
        n -= 1;
        break;
      case 'action': {
        const info = ctx.info || stepTemplate(step);
        const valueNote = step.paramName ? `param ${step.paramName}` : (ctx.loop ? 'from ROWS' : '');
        lines.push(`${indent}// ${label}: ${step.method} ${commentText(step.desc)}`);
        lines.push(...renderAction(step, info, { subst: ctx.subst, inLoop: Boolean(ctx.loop), indent }));
        table.push([n, step.method, step.desc, valueNote, [step.healable ? 'self-healing' : 'plain call', step.irreversible ? 'skipped in dry-run' : '', ctx.loop ? `loop ${ctx.loop}` : ''].filter(Boolean).join('; ')]);
        break;
      }
      default: break;
    }
  };

  for (const b of blocks) {
    if (b.type === 'single') { emitStep(b.step, I); continue; }
    loopN += 1;
    const rowsName = `ROWS_${loopN}`;
    lines.push(`${I}// Repeated pattern (${b.repeats}x ${b.steps.length} step(s)) detected: one runItem per row. Edit ROWS_${loopN} or load it from a file/param.`);
    lines.push(`${I}const ${rowsName} = ${safeJson(b.rows)};`);
    lines.push(`${I}for (const row of ${rowsName}) {`);
    lines.push(`${I}  await w.runItem(String(row.v0), async () => {`);
    b.steps.forEach((st, j) => {
      const info = b.infos[j];
      const subst = { loc: {}, arg: {} };
      const locLits = info.locParts.filter((p) => p.t === 'lit').length;
      b.varying.forEach((v, idx) => {
        if (v.j !== j) return;
        if (v.s < locLits) subst.loc[v.s] = `row.v${idx}`;
        else subst.arg[v.s - locLits] = `row.v${idx}`;
      });
      emitStep(st, `${I}    `, { loop: loopN, info, subst });
    });
    lines.push(`${I}  });`);
    lines.push(`${I}}`);
  }

  const total = n;
  const head = [];
  head.push("'use strict';");
  head.push('/**');
  head.push(` * ${name}: GENERATED by amx-record from a Playwright codegen recording of ${commentText(url) || '(unknown url)'} on ${commentText(stamp)}.`);
  head.push(' * REVIEW EVERY STEP before releasing: see README.md (checklist + recorded steps) and params.suggested.yaml.');
  head.push(' * Credentials are never recorded: sensitive steps throw until you use the user profile / Credential Manager.');
  head.push(' */');
  head.push("const path = require('node:path');");
  head.push("const fw = require(process.env.AUTOMAGIX_FRAMEWORK_DIR || path.resolve(__dirname, '..', '..', 'execution-framework'));");
  head.push('');
  head.push(`const NAME = ${jsStr(name)};`);
  head.push('');
  head.push('fw.wrapper.entrypoint(async (framework) => {');
  head.push('  const w = framework.wrapper;');
  head.push("  const sso = framework.adapters.get('sso-detect');");
  const pLines = params.filter((p) => !p.sensitive && p.default !== null).map((p) => `    ${p.name}: w.param(${jsStr(p.name)}, ${jsStr(p.default)}),`);
  head.push('  const P = {');
  head.push(...pLines);
  head.push('  };');
  head.push(`  w.logEvent('start', { recorded_steps: ${total}, dry_run: w.isDryRun() });`);
  head.push('');
  head.push('  const session = await framework.browser.launch({');
  head.push("    headless: String(w.param('headless', 'true')) !== 'false',");
  head.push("    channel: w.param('channel', 'msedge'),");
  head.push('  });');
  head.push('  try {');
  head.push('    // guard: any error inside captures failure evidence (screenshot + redacted DOM + context) before it propagates');
  head.push('    await session.guard(async () => {');
  head.push('      const { page } = session;');
  head.push('      const rp = framework.resilience.resilientPage(page, { caller: NAME });');
  if (needsL) head.push('      const L = (fn, pg = page) => fn(pg); // plain (non-healing) locator access');
  const tail = [];
  tail.push("      w.setResult({ steps: " + total + ", final_host: new URL(page.url()).host });");
  tail.push('    });');
  tail.push('  } finally {');
  tail.push('    await session.close();');
  tail.push('  }');
  tail.push('});');
  tail.push('');
  return { code: [...head, ...lines, ...tail].join('\n'), table, total, params };
}

function yamlStr(s) {
  return JSON.stringify(String(s));
}

function renderParamsYaml(params) {
  const out = ['# Suggested schedule-slot params for the registry manifest (schedule.slots[].params).', '# Values are what was recorded. Secrets are never recorded.'];
  for (const p of params) {
    if (p.sensitive || p.default === null) out.push(`# ${p.name}: <not recorded - secret; use the user profile / Credential Manager>  (${p.source})`);
    else out.push(`${p.name}: ${yamlStr(p.default)}   # ${String(p.source).replace(/[\r\n]+/g, ' ')}`);
  }
  return `${out.join('\n')}\n`;
}

function renderReadme({ name, url, table, warnings, params }) {
  const esc = (v) => String(v === undefined ? '' : v).replace(/\|/g, '\\|').replace(/[\r\n\u2028\u2029]+/g, ' ');
  const rows = table.map((r) => `| ${r.map(esc).join(' | ')} |`).join('\n');
  return `# ${name}

> **Generated by \`amx-record\` from a Playwright codegen recording of ${commentText(url).replace(/[\x60*|]/g, '') || '(unknown url)'} - NOT production-ready until reviewed.**

## Review checklist
- [ ] Every recorded step below is what you intended (codegen cannot record conditions or loops; add them by hand).
- [ ] Credentials: no secret was recorded. Steps marked **throws** need the user profile / Windows Credential Manager - never a literal.
- [ ] Add per-item telemetry (\`w.runItem\` / \`w.logItem\`) around the unit of work; wrap irreversible steps in dry-run guards.
- [ ] Move recorded values you want to vary into schedule params (see \`params.suggested.yaml\`).
- [ ] Fill in \`manifest-entry\` (owner, systems, data classification, schedule) and the standard checklist (docs/AUTOMATION_STANDARD.md).
- [ ] \`python -m wrapper_core dev .\` runs cleanly; \`tests/test_lifecycle.py\` passes.

## Recorded steps

| # | Action | Target | Value / param | Notes |
|---|---|---|---|---|
${rows || '| | | | | |'}

## Params
${params.length ? params.map((p) => `- \`${p.name}\`${p.sensitive ? ' (secret - not recorded)' : ` = ${JSON.stringify(p.default)}`} - ${esc(p.source)}`).join('\n') : '(none)'}

## Conversion warnings
${warnings.length ? warnings.map((x) => `- ${x}`).join('\n') : '(none)'}
`;
}

function renderTestStub(name) {
  return `"""Lifecycle stub for ${name}: runs the payload through the REAL wrapper-core run lifecycle in --dry-run (no side effects).

Needs Node 18+ and Microsoft Edge; skipped otherwise. Point WRAPPER_CORE_DIR at a wrapper-core checkout if it is not a sibling.
"""
import os
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, os.environ.get("WRAPPER_CORE_DIR", str(ROOT.parent / "wrapper-core")))

from wrapper_core.testing import run_payload  # noqa: E402


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_dry_run_completes_without_side_effects():
    r = run_payload(ROOT, {}, dry_run=True)
    assert r.record["status"] == "success", r.record.get("error")
`;
}

function renderDescriptor(name) {
  return `name: ${name}
version: 0.1.0
runtime: node
entrypoint: main.js
args: []
requires: {wrapper_core: ">=0.1.0,<1.0.0", execution_framework: ">=0.1.0,<1.0.0"}
timeout_s: 900
`;
}

/**
 * convert(codegenText, {name, url, now}) -> {files, steps, params, warnings, variant}
 * `url` defaults to the first goto in the recording.
 */
function convert(codegenText, opts = {}) {
  const name = opts.name || 'automation-recorded';
  if (!/^automation-[a-z0-9]+(?:-[a-z0-9]+)*$/.test(name)) throw new Error(`name must look like automation-some-name (got ${JSON.stringify(name)})`);
  const { body, variant } = extractBody(codegenText);
  const statements = splitStatements(body);
  if (!statements.length) throw new Error('no recorded actions found in the codegen output');
  const model = buildModel(statements, { url: opts.url });
  const firstGoto = model.steps.find((s) => s.kind === 'goto');
  const url = opts.url || (firstGoto && firstGoto.desc) || '';
  const stamp = (opts.now ? new Date(opts.now) : new Date()).toISOString();
  const main = renderMain(model, { name, url, stamp });
  const files = {
    'main.js': main.code,
    'automation.yaml': renderDescriptor(name),
    'README.md': renderReadme({ name, url, table: main.table, warnings: model.warnings, params: main.params }),
    'params.suggested.yaml': renderParamsYaml(main.params),
    'tests/test_lifecycle.py': renderTestStub(name),
  };
  return { files, steps: main.table, params: main.params, warnings: model.warnings, variant, total: main.total };
}

/** Write the converted files under outDir. Refuses a non-empty directory unless force. Returns the written paths. */
function writeProject(files, outDir, { fs = nodeFs, force = false } = {}) {
  if (fs.existsSync(outDir)) {
    const existing = fs.readdirSync(outDir);
    if (existing.length && !force) throw new Error(`${outDir} already exists and is not empty (use --force to overwrite)`);
  }
  const written = [];
  for (const [rel, text] of Object.entries(files)) {
    const target = path.join(outDir, rel);
    fs.mkdirSync(path.dirname(target), { recursive: true });
    fs.writeFileSync(target, text, 'utf8');
    written.push(target);
  }
  return written;
}

module.exports = {
  convert, writeProject, extractBody, splitStatements, parseStatement, parseChain, scanLiterals, mask, detectRepeats, buildModel,
  looksSensitiveValue, tokenizeLits, jsStr, unquote, stringLiterals, describeSeg, CREDENTIAL_THROW, SENSITIVE_NAME,
  commentText, parseLiteralArgs, emitLiteral, optionsOk, sanitizeActionArgs, canonLocSegs,
};
