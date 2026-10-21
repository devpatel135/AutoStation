// Web/Node globals missing from bare V8, just enough for the execution-framework's pure-logic modules and their tests.
globalThis.global = globalThis;
globalThis.queueMicrotask = (f) => Promise.resolve().then(f);

// ---- virtual timers (pumped from Python) ----
(function () {
  let seq = 0; let now = 0; const timers = new Map();
  globalThis.setTimeout = (fn, ms, ...args) => { const id = ++seq; timers.set(id, { at: now + Math.max(0, Number(ms) || 0), id, fn, args }); return id; };
  globalThis.clearTimeout = (id) => { timers.delete(id); };
  globalThis.setInterval = () => { throw new Error('setInterval not supported in prelude'); };
  globalThis.__pump = () => {
    if (!timers.size) return false;
    let best = null;
    for (const t of timers.values()) if (!best || t.at < best.at || (t.at === best.at && t.id < best.id)) best = t;
    timers.delete(best.id); now = Math.max(now, best.at);
    best.fn(...best.args);
    return true;
  };
  globalThis.__pending = () => timers.size;
})();

// ---- console ----
globalThis.__logs = [];
globalThis.console = { log: (...a) => __logs.push(a.map(String).join(' ')), error: (...a) => __logs.push('ERR ' + a.map(String).join(' ')), warn: () => {}, info: () => {} };

// ---- TextEncoder / TextDecoder (utf-8) ----
globalThis.TextEncoder = class { encode(s) { const out = []; s = String(s); for (let i = 0; i < s.length; i++) { let c = s.codePointAt(i); if (c > 0xffff) i++; if (c < 0x80) out.push(c); else if (c < 0x800) out.push(0xc0 | (c >> 6), 0x80 | (c & 63)); else if (c < 0x10000) out.push(0xe0 | (c >> 12), 0x80 | ((c >> 6) & 63), 0x80 | (c & 63)); else out.push(0xf0 | (c >> 18), 0x80 | ((c >> 12) & 63), 0x80 | ((c >> 6) & 63), 0x80 | (c & 63)); } return new Uint8Array(out); } };
globalThis.TextDecoder = class { decode(b) { b = b || []; let s = ''; for (let i = 0; i < b.length;) { const c = b[i++]; let cp; if (c < 0x80) cp = c; else if (c < 0xe0) cp = ((c & 31) << 6) | (b[i++] & 63); else if (c < 0xf0) { cp = ((c & 15) << 12) | ((b[i++] & 63) << 6) | (b[i++] & 63); } else { cp = ((c & 7) << 18) | ((b[i++] & 63) << 12) | ((b[i++] & 63) << 6) | (b[i++] & 63); } s += String.fromCodePoint(cp); } return s; } };

// ---- atob / btoa ----
(function () {
  const A = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/';
  globalThis.btoa = (s) => { let o = ''; for (let i = 0; i < s.length; i += 3) { const a = s.charCodeAt(i); const b = s.charCodeAt(i + 1); const c = s.charCodeAt(i + 2); o += A[a >> 2] + A[((a & 3) << 4) | ((b || 0) >> 4)] + (i + 1 < s.length ? A[((b & 15) << 2) | ((c || 0) >> 6)] : '=') + (i + 2 < s.length ? A[c & 63] : '='); } return o; };
  globalThis.atob = (s) => { s = String(s).replace(/=+$/, ''); let bits = 0; let n = 0; let o = ''; for (const ch of s) { bits = (bits << 6) | A.indexOf(ch); n += 6; if (n >= 8) { n -= 8; o += String.fromCharCode((bits >> n) & 255); } } return o; };
})();

// ---- AbortController / fetch ----
globalThis.AbortController = class { constructor() { const ls = []; this.signal = { aborted: false, addEventListener: (t, f) => { if (t === 'abort') ls.push(f); }, removeEventListener() {} }; this._ls = ls; } abort() { this.signal.aborted = true; this._ls.forEach((f) => f({ type: 'abort' })); } };
globalThis.fetch = async () => { const e = new TypeError('fetch failed'); e.cause = { code: 'ECONNREFUSED' }; throw e; };

// ---- URL / URLSearchParams (subset) ----
(function () {
  const DEFAULT_PORTS = { 'http:': '80', 'https:': '443', 'ftp:': '21', 'ws:': '80', 'wss:': '443' };
  class URLSearchParams {
    constructor(init, owner) { this._p = []; this._owner = owner; if (init) { String(init).replace(/^\?/, '').split('&').filter(Boolean).forEach((kv) => { const i = kv.indexOf('='); this._p.push([decodeURIComponent(i < 0 ? kv : kv.slice(0, i)), i < 0 ? '' : decodeURIComponent(kv.slice(i + 1))]); }); } }
    _sync() { if (this._owner) this._owner._search = this._p.length ? '?' + this.toString() : ''; }
    set(k, v) { k = String(k); const i = this._p.findIndex((x) => x[0] === k); if (i < 0) this._p.push([k, String(v)]); else { this._p[i][1] = String(v); this._p = this._p.filter((x, j) => x[0] !== k || j === i); } this._sync(); }
    append(k, v) { this._p.push([String(k), String(v)]); this._sync(); }
    get(k) { const x = this._p.find((y) => y[0] === k); return x ? x[1] : null; }
    has(k) { return this._p.some((y) => y[0] === k); }
    keys() { return this._p.map((x) => x[0])[Symbol.iterator](); }
    values() { return this._p.map((x) => x[1])[Symbol.iterator](); }
    entries() { return this._p.map((x) => [x[0], x[1]])[Symbol.iterator](); }
    [Symbol.iterator]() { return this.entries(); }
    toString() { return this._p.map(([k, v]) => encodeURIComponent(k) + '=' + encodeURIComponent(v)).join('&'); }
  }
  const RX = /^([a-zA-Z][a-zA-Z0-9+.-]*):(?:\/\/([^\/?#]*))?([^?#]*)(\?[^#]*)?(#.*)?$/;
  function normPath(p) { const out = []; for (const s of p.split('/')) { if (s === '.') continue; if (s === '..') { if (out.length > 1) out.pop(); continue; } out.push(s); } return out.join('/'); }
  class URL {
    constructor(input, base) {
      input = String(input);
      let m = RX.exec(input);
      if (!m) {
        if (base === undefined) throw new TypeError('Invalid URL');
        const b = base instanceof URL ? base : new URL(base);
        let rest = input; let hash = ''; let search = '';
        const hi = rest.indexOf('#'); if (hi >= 0) { hash = rest.slice(hi); rest = rest.slice(0, hi); }
        const qi = rest.indexOf('?'); if (qi >= 0) { search = rest.slice(qi); rest = rest.slice(0, qi); }
        let path;
        if (rest.startsWith('/')) path = rest;
        else if (rest === '') { path = b.pathname; if (!search && qi < 0) search = b._search; }
        else path = b.pathname.slice(0, b.pathname.lastIndexOf('/') + 1) + rest;
        this._protocol = b.protocol; this._host = b._host; this._pathname = normPath(path) || '/'; this._search = search === '?' ? '' : search; this._hash = hash;
      } else {
        this._protocol = m[1].toLowerCase() + ':';
        let host = (m[2] || '').toLowerCase();
        const at = host.lastIndexOf('@'); if (at >= 0) host = host.slice(at + 1);
        if (m[2] !== undefined && host === '' && this._protocol !== 'file:') throw new TypeError('Invalid URL');
        const pm = /^(.*?)(?::(\d+))?$/.exec(host);
        host = pm[1] + (pm[2] && pm[2] !== DEFAULT_PORTS[this._protocol] ? ':' + pm[2] : '');
        this._host = host;
        const special = this._protocol in DEFAULT_PORTS || this._protocol === 'file:';
        this._pathname = special ? (normPath(m[3] || '/') || '/') : (m[3] || '');
        this._search = m[4] && m[4] !== '?' ? m[4] : ''; this._hash = m[5] && m[5] !== '#' ? m[5] : '';
      }
      this._sp = new URLSearchParams(this._search, this);
    }
    get protocol() { return this._protocol; }
    get host() { return this._host; }
    get hostname() { return this._host.replace(/:\d+$/, ''); }
    get port() { const m = /:(\d+)$/.exec(this._host); return m ? m[1] : ''; }
    get pathname() { return this._pathname; }
    get search() { return this._search; }
    get hash() { return this._hash; }
    get searchParams() { return this._sp; }
    get origin() { return this._protocol in DEFAULT_PORTS ? this._protocol + '//' + this._host : 'null'; }
    get href() { return this._protocol + (this._host || this._protocol === 'file:' ? '//' + this._host : '') + this._pathname + this._search + this._hash; }
    toString() { return this.href; }
  }
  globalThis.URL = URL; globalThis.URLSearchParams = URLSearchParams;
})();
