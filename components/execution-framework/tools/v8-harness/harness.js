// Minimal Node-compat harness: runs the execution-framework's CommonJS modules and node:test suites inside a real JS engine
// (Edge headless) using in-memory shims for fs/path/os/crypto/assert/test. Network/server/stream-dependent tests are reported as SKIP-ENV.
(async () => {
  const SRC = globalThis.__SRC__;
  const ROOT = '/fw';
  const report = [];
  const only = SRC.__only || null;

  const unsupported = (what) => { const e = new Error('shim:unsupported ' + what); e.shimUnsupported = true; return e; };

  // ---------- sha256 (sync) ----------
  function sha256Bytes(bytes) {
    const rotr = (x, n) => (x >>> n) | (x << (32 - n));
    const primes = [];
    for (let c = 2; primes.length < 64; c++) { let ok = true; for (const p of primes) { if (p * p > c) break; if (c % p === 0) { ok = false; break; } } if (ok) primes.push(c); }
    const frac = (x) => Math.floor((x - Math.floor(x)) * 4294967296);
    const K = primes.map((p) => frac(Math.cbrt(p)));
    let h = primes.slice(0, 8).map((p) => frac(Math.sqrt(p)));
    const l = bytes.length;
    const total = l + 1 + ((56 - ((l + 1) % 64) + 64) % 64) + 8;
    const buf = new Uint8Array(total); buf.set(bytes); buf[l] = 0x80;
    const dv = new DataView(buf.buffer);
    dv.setUint32(total - 8, Math.floor((l * 8) / 4294967296)); dv.setUint32(total - 4, (l * 8) >>> 0);
    const w = new Uint32Array(64);
    for (let off = 0; off < total; off += 64) {
      for (let i = 0; i < 16; i++) w[i] = dv.getUint32(off + i * 4);
      for (let i = 16; i < 64; i++) {
        const s0 = rotr(w[i - 15], 7) ^ rotr(w[i - 15], 18) ^ (w[i - 15] >>> 3);
        const s1 = rotr(w[i - 2], 17) ^ rotr(w[i - 2], 19) ^ (w[i - 2] >>> 10);
        w[i] = (w[i - 16] + s0 + w[i - 7] + s1) >>> 0;
      }
      let [a, b, c, d, e, f, g, hh] = h;
      for (let i = 0; i < 64; i++) {
        const S1 = rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25);
        const ch = (e & f) ^ (~e & g);
        const t1 = (hh + S1 + ch + K[i] + w[i]) >>> 0;
        const S0 = rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22);
        const maj = (a & b) ^ (a & c) ^ (b & c);
        const t2 = (S0 + maj) >>> 0;
        hh = g; g = f; f = e; e = (d + t1) >>> 0; d = c; c = b; b = a; a = (t1 + t2) >>> 0;
      }
      const v = [a, b, c, d, e, f, g, hh];
      h = h.map((x, i) => (x + v[i]) >>> 0);
    }
    return h.map((x) => x.toString(16).padStart(8, '0')).join('');
  }
  const enc = new TextEncoder();
  const dec = new TextDecoder();
  if (sha256Bytes(enc.encode('abc')) !== 'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad') {
    globalThis.__REPORT__ = [{ file: 'harness', name: 'sha256 self-check', status: 'fail', error: 'sha256 shim wrong' }]; globalThis.__done = true;
    return;
  }

  // ---------- Buffer ----------
  class Buf extends Uint8Array {
    static from(x, e) {
      if (typeof x === 'string') {
        if (e === 'base64') { const s = atob(x); const b = new Buf(s.length); for (let i = 0; i < s.length; i++) b[i] = s.charCodeAt(i); return b; }
        if (e === 'hex') { const b = new Buf(x.length / 2); for (let i = 0; i < b.length; i++) b[i] = parseInt(x.substr(i * 2, 2), 16); return b; }
        const u = enc.encode(x); const b = new Buf(u.length); b.set(u); return b;
      }
      const b = new Buf(x.length); b.set(x); return b;
    }
    static byteLength(s) { return enc.encode(String(s)).length; }
    static concat(list) { const n = list.reduce((a, c) => a + c.length, 0); const b = new Buf(n); let o = 0; for (const c of list) { b.set(c, o); o += c.length; } return b; }
    toString(e) {
      if (e === 'base64') { let s = ''; for (let i = 0; i < this.length; i++) s += String.fromCharCode(this[i]); return btoa(s); }
      if (e === 'hex') return Array.from(this).map((x) => x.toString(16).padStart(2, '0')).join('');
      return dec.decode(this);
    }
  }

  // ---------- assert ----------
  class AssertionError extends Error { constructor(m) { super(m); this.name = 'AssertionError'; } }
  const isObj = (v) => v !== null && typeof v === 'object';
  function deepEq(a, b) {
    if (Object.is(a, b)) return true;
    if (!isObj(a) || !isObj(b)) return false;
    if (Object.getPrototypeOf(a) !== Object.getPrototypeOf(b)) return false;
    if (Array.isArray(a)) { if (a.length !== b.length) return false; return a.every((x, i) => deepEq(x, b[i])); }
    const ka = Object.keys(a); const kb = Object.keys(b);
    if (ka.length !== kb.length) return false;
    return ka.every((k) => Object.prototype.hasOwnProperty.call(b, k) && deepEq(a[k], b[k]));
  }
  const show = (v) => { try { return typeof v === 'string' ? JSON.stringify(v) : JSON.stringify(v, (k, x) => (x instanceof Error ? String(x) : x)); } catch (_) { return String(v); } };
  function matchesExpected(err, expected, label) {
    if (expected === undefined) return;
    if (typeof expected === 'function' && expected.prototype !== undefined && (expected.prototype instanceof Error || expected === Error)) {
      if (!(err instanceof expected)) throw new AssertionError(`${label}: expected instance of ${expected.name}, got ${err && err.name}: ${err && err.message}`);
      return;
    }
    if (expected instanceof RegExp) {
      if (!expected.test(String(err))) throw new AssertionError(`${label}: ${expected} did not match ${show(String(err))}`);
      return;
    }
    if (typeof expected === 'function') {
      if (expected(err) !== true) throw new AssertionError(`${label}: validation function did not return true for ${show(String(err))}`);
      return;
    }
    if (isObj(expected)) {
      for (const k of Object.keys(expected)) {
        const ok = expected[k] instanceof RegExp ? expected[k].test(String(err[k])) : deepEq(err[k], expected[k]);
        if (!ok) throw new AssertionError(`${label}: property ${k} mismatch: ${show(err[k])}`);
      }
    }
  }
  const assert = {
    AssertionError,
    ok(v, m) { if (!v) throw new AssertionError(m || `expected truthy, got ${show(v)}`); },
    equal(a, b, m) { if (!Object.is(a, b)) throw new AssertionError(m || `expected ${show(b)}, got ${show(a)}`); },
    notEqual(a, b, m) { if (Object.is(a, b)) throw new AssertionError(m || `expected values to differ: ${show(a)}`); },
    deepEqual(a, b, m) { if (!deepEq(a, b)) throw new AssertionError(m || `deepEqual failed:\n actual:   ${show(a)}\n expected: ${show(b)}`); },
    notDeepEqual(a, b, m) { if (deepEq(a, b)) throw new AssertionError(m || 'expected values to differ'); },
    match(s, rx, m) { if (typeof s !== 'string' || !rx.test(s)) throw new AssertionError(m || `${show(s)} did not match ${rx}`); },
    doesNotMatch(s, rx, m) { if (typeof s === 'string' && rx.test(s)) throw new AssertionError(m || `${show(s)} unexpectedly matched ${rx}`); },
    fail(m) { throw new AssertionError(m || 'assert.fail'); },
    throws(fn, expected, m) {
      let threw = false; let err;
      try { fn(); } catch (e) { threw = true; err = e; }
      if (!threw) throw new AssertionError(m || 'expected function to throw');
      matchesExpected(err, expected, 'throws');
    },
    doesNotThrow(fn) { try { fn(); } catch (e) { throw new AssertionError('unexpected throw: ' + e); } },
    async rejects(p, expected, m) {
      let threw = false; let err;
      try { await (typeof p === 'function' ? p() : p); } catch (e) { threw = true; err = e; }
      if (!threw) throw new AssertionError(m || 'expected promise to reject');
      matchesExpected(err, expected, 'rejects');
    },
  };
  assert.strictEqual = assert.equal; assert.deepStrictEqual = assert.deepEqual; assert.strict = assert;

  // ---------- path ----------
  const normSegs = (p) => { const out = []; for (const s of String(p).replace(/\\/g, '/').split('/')) { if (s === '' || s === '.') continue; if (s === '..') out.pop(); else out.push(s); } return out; };
  const pathShim = {
    sep: '/',
    isAbsolute: (p) => /^([\\/]|[A-Za-z]:)/.test(String(p)),
    join: (...parts) => { const j = parts.filter((x) => x !== '').join('/'); const lead = /^[\\/]/.test(j) ? '/' : ''; return lead + normSegs(j).join('/') || '.'; },
    resolve: (...parts) => { let cur = ''; for (const p of parts) cur = pathShim.isAbsolute(p) ? String(p) : cur + '/' + p; return '/' + normSegs(cur).join('/'); },
    dirname: (p) => { const s = String(p).replace(/\\/g, '/'); const i = s.lastIndexOf('/'); return i <= 0 ? (i === 0 ? '/' : '.') : s.slice(0, i); },
    basename: (p, ext) => { let b = String(p).replace(/\\/g, '/').split('/').pop(); if (ext && b.endsWith(ext)) b = b.slice(0, -ext.length); return b; },
  };

  class Emitter {
    constructor() { this._l = {}; }
    on(e, f) { (this._l[e] = this._l[e] || []).push(f); if (e === 'data' && this._buf) { const b = this._buf; this._buf = null; b.forEach((c) => this.emit('data', c)); if (this._ended) setTimeout(() => this.emit('end'), 0); } return this; }
    once(e, f) { const g = (...a) => { this._l[e] = (this._l[e] || []).filter((x) => x !== g); f(...a); }; return this.on(e, g); }
    emit(e, ...a) { (this._l[e] || []).slice().forEach((f) => f(...a)); }
    addEventListener(e, f) { this.on(e, f); }
  }
  class PassThrough extends Emitter {
    write(c) { const b = typeof c === 'string' ? Buf.from(c) : c; if (this._l.data && this._l.data.length) this.emit('data', b); else (this._buf = this._buf || []).push(b); return true; }
    end() { this._ended = true; if (!this._buf) setTimeout(() => this.emit('end'), 0); }
  }
  function makeReadline() {
    return { createInterface: ({ input }) => {
      const rl = new Emitter(); let buf = '';
      input.on('data', (c) => { buf += String(c); const lines = buf.split(/\r?\n/); buf = lines.pop(); lines.forEach((l) => rl.emit('line', l)); });
      input.on('end', () => { if (buf) rl.emit('line', buf); rl.emit('close'); });
      return rl;
    } };
  }
  // in-memory HTTP: servers registered by port; client side via http.request and fetch
  function makeHttp(ctxRef) {
    const servers = new Map(); let nextPort = 41000;
    const mkReq = (method, url, headers, body) => { const r = new Emitter(); r.method = method; r.url = url; r.headers = headers; setTimeout(() => { if (body) r.emit('data', Buf.from(body)); r.emit('end'); }, 0); return r; };
    function dispatch(port, method, path, headers, body, onResponse, onError) {
      const srv = servers.get(String(port));
      if (!srv) { setTimeout(() => onError(Object.assign(new Error(`connect ECONNREFUSED 127.0.0.1:${port}`), { code: 'ECONNREFUSED' })), 0); return; }
      const lower = {}; for (const [k, v] of Object.entries(headers || {})) lower[k.toLowerCase()] = String(v);
      const res = { statusCode: 200, _h: {}, writeHead(c, h) { this.statusCode = c; this._h = h || {}; }, end(b) { onResponse(this.statusCode, this._h, b === undefined ? '' : String(b)); } };
      srv.handler(mkReq(method, path, lower, body), res);
    }
    const http = {
      createServer: (handler) => { const srv = { handler, port: null, listen(port, host, cb) { this.port = String(nextPort++); servers.set(this.port, this); setTimeout(() => cb && cb(), 0); return this; }, address() { return { port: Number(this.port) }; }, close(cb) { servers.delete(this.port); setTimeout(() => cb && cb(), 0); } }; return srv; },
      request: (opts, cb) => {
        const req = new Emitter(); let body = ''; let timeoutCb = null; let destroyed = false;
        req.write = (b) => { body += b; };
        req.setTimeout = (ms, f) => { timeoutCb = f; };
        req.destroy = (err) => { destroyed = true; setTimeout(() => req.emit('error', err), 0); };
        req.end = () => dispatch(opts.port, opts.method, opts.path, opts.headers, body, (status, headers, text) => {
          if (destroyed) return;
          const res = new Emitter(); res.statusCode = status; res.headers = Object.fromEntries(Object.entries(headers).map(([k, v]) => [k.toLowerCase(), v]));
          cb(res); setTimeout(() => { res.emit('data', Buf.from(text)); res.emit('end'); }, 0);
        }, (err) => { if (!destroyed) req.emit('error', err); });
        return req;
      },
    };
    ctxRef.fetch = async (url, init = {}) => {
      const u = new URL(url);
      return new Promise((resolve, reject) => {
        dispatch(u.port, init.method || 'GET', u.pathname + u.search, init.headers, init.body || '', (status, headers, text) => resolve({ status, ok: status < 400, text: async () => text, json: async () => JSON.parse(text) }), (err) => { const e = new TypeError('fetch failed'); e.cause = err; reject(e); });
        if (init.signal) init.signal.addEventListener('abort', () => reject(Object.assign(new Error('aborted'), { name: 'AbortError' })));
      });
    };
    return http;
  }

  function makeContext(fileEntry) {
    // ---------- process, memfs, modules (fresh per test file = per Node process) ----------
    const process = { env: {}, pid: 4242, platform: 'win32', execPath: 'node', exitCode: undefined, cwd: () => '/work', stderr: { write() { return true; } }, stdout: { write() { return true; } }, argv: [] };
    const files = new Map(); const dirs = new Set(['/', '/work', '/tmp']);
    const abs = (p) => { const s = String(p); return '/' + normSegs(/^([\\/]|[A-Za-z]:)/.test(s) ? s : '/work/' + s).join('/'); };
    const enoent = (p, op) => Object.assign(new Error(`ENOENT: no such file or directory, ${op} '${p}'`), { code: 'ENOENT' });
    const parent = (p) => pathShim.dirname(p);
    const mk = (p) => { let cur = ''; for (const s of normSegs(p)) { cur += '/' + s; dirs.add(cur); } };
    const fs = {
      existsSync: (p) => files.has(abs(p)) || dirs.has(abs(p)),
      mkdirSync: (p, o) => { const a = abs(p); if (o && o.recursive) mk(a); else { if (!dirs.has(parent(a))) throw enoent(p, 'mkdir'); dirs.add(a); } },
      writeFileSync: (p, d) => { const a = abs(p); if (!dirs.has(parent(a))) throw enoent(p, 'open'); files.set(a, typeof d === 'string' ? d : dec.decode(d)); },
      appendFileSync: (p, d) => { const a = abs(p); if (!dirs.has(parent(a))) throw enoent(p, 'open'); files.set(a, (files.get(a) || '') + (typeof d === 'string' ? d : dec.decode(d))); },
      readFileSync: (p, e) => { const a = abs(p); if (!files.has(a)) throw enoent(p, 'open'); return e ? files.get(a) : Buf.from(files.get(a)); },
      renameSync: (a, b) => { const x = abs(a); if (!files.has(x)) throw enoent(a, 'rename'); files.set(abs(b), files.get(x)); files.delete(x); },
      unlinkSync: (p) => { if (!files.delete(abs(p))) throw enoent(p, 'unlink'); },
      rmSync: (p, o) => { const a = abs(p); const had = files.delete(a) || dirs.delete(a); for (const f of [...files.keys()]) if (f.startsWith(a + '/')) { files.delete(f); } for (const d of [...dirs]) if (d.startsWith(a + '/')) dirs.delete(d); if (!had && !(o && o.force)) throw enoent(p, 'rm'); },
      mkdtempSync: (prefix) => { const a = abs(prefix + Math.random().toString(36).slice(2, 8)); mk(a); return a; },
      readdirSync: (p) => { const a = abs(p); if (!dirs.has(a)) throw enoent(p, 'scandir'); const names = new Set(); const pre = a === '/' ? '/' : a + '/'; for (const f of files.keys()) if (f.startsWith(pre)) names.add(f.slice(pre.length).split('/')[0]); for (const d of dirs) if (d.startsWith(pre) && d !== a) names.add(d.slice(pre.length).split('/')[0]); return [...names].sort(); },
      statSync: (p) => { const a = abs(p); if (files.has(a)) return { size: enc.encode(files.get(a)).length, isDirectory: () => false, isFile: () => true }; if (dirs.has(a)) return { size: 0, isDirectory: () => true, isFile: () => false }; throw enoent(p, 'stat'); },
      createReadStream: (p) => { const a = abs(p); if (!files.has(a)) throw enoent(p, 'open'); const st = new Emitter(); setTimeout(() => { st.emit('data', Buf.from(files.get(a))); st.emit('end'); }, 0); return st; },
    };
    for (const [rel, text] of Object.entries(SRC.files)) { const a = ROOT + '/' + rel; mk(pathShim.dirname(a)); files.set(a, text); }

    // ---------- node:test ----------
    const tests = [];
    const testFn = (name, a, b) => { const fn = typeof a === 'function' ? a : b; const opts = typeof a === 'object' ? a : {}; tests.push({ name, fn, skip: Boolean(opts && opts.skip) }); };
    testFn.skip = (name) => tests.push({ name, fn: null, skip: true });
    const globalsForCtx = {};
    const httpShim = makeHttp(globalsForCtx);
    globalThis.fetch = (...a) => globalsForCtx.fetch(...a);
    const builtins = {
      'node:test': testFn, 'test': testFn,
      'node:assert/strict': assert, 'assert': assert, 'node:assert': assert,
      'node:fs': fs, 'fs': fs, 'node:path': pathShim, 'path': pathShim,
      'node:os': { tmpdir: () => '/tmp', homedir: () => '/home/u' }, 'os': { tmpdir: () => '/tmp', homedir: () => '/home/u' },
      'node:crypto': { createHash: (alg) => { if (alg !== 'sha256') throw unsupported('hash ' + alg); let data = ''; const h = { update(s) { data += (typeof s === 'string' ? s : dec.decode(s)); return h; }, digest(e) { return sha256Bytes(enc.encode(data)); } }; return h; } },
      'node:child_process': { spawnSync: () => ({ status: 1 }), spawn: () => { throw unsupported('child_process.spawn'); } },
      'node:http': httpShim,
      'node:https': httpShim,
      'node:readline': makeReadline(),
      'node:stream': { PassThrough },
      'node:url': { fileURLToPath: (u) => String(u).replace('file://', '') },
      'node:module': { createRequire: () => (id) => { throw unsupported('createRequire ' + id); } },
      'playwright-core': new Proxy({}, { get() { throw unsupported('playwright-core'); } }),
    };
    const cache = new Map();
    function resolveRel(from, id) {
      const base = pathShim.resolve(pathShim.dirname(from), id);
      for (const c of [base, base + '.js', base + '.cjs', base + '/index.js', base + '.json']) if (files.has(c)) return c;
      throw new Error(`Cannot find module '${id}' from ${from}`);
    }
    function loadModule(vpath) {
      if (cache.has(vpath)) return cache.get(vpath).exports;
      const src = files.get(vpath);
      if (vpath.endsWith('.json')) { const m = { exports: JSON.parse(src) }; cache.set(vpath, m); return m.exports; }
      const module = { exports: {} };
      cache.set(vpath, module);
      const req = (id) => {
        if (id in builtins) return builtins[id];
        if (id.startsWith('.')) return loadModule(resolveRel(vpath, id));
        throw new Error(`Cannot find module '${id}' (shim)`);
      };
      let code = src.replace(/^#!.*/, '');
      if (globalThis.__NO_ICU__) code = code.replace(/\\p\{L\}\\p\{N\}/g, 'A-Za-z0-9');
      const fn = new Function('exports', 'require', 'module', '__filename', '__dirname', 'process', 'Buffer', 'setImmediate', code + '\n//# sourceURL=' + vpath);
      fn(module.exports, req, module, vpath, pathShim.dirname(vpath), process, Buf, (f, ...a) => setTimeout(f, 0, ...a));
      return module.exports;
    }
    return { loadModule, tests, process };
  }

  const names = Object.keys(SRC.files).filter((f) => /^test\/.*\.test\.js$/.test(f)).sort().filter((f) => !only || only.includes(f));
  for (const rel of names) {
    const row = (name, status, error) => report.push({ file: rel, name, status, error: error || '' });
    let c;
    try {
      c = makeContext(rel);
      c.loadModule(ROOT + '/' + rel);
    } catch (e) {
      row('(load file)', e && e.shimUnsupported ? 'skip-env' : 'fail', String((e && e.stack) || e).split('\n').slice(0, 6).join(' | '));
      continue;
    }
    for (const t of c.tests) {
      if (t.skip || !t.fn) { row(t.name, 'skip', ''); continue; }
      const afters = [];
      const ctx = { name: t.name, after: (f) => afters.push(f), diagnostic() {} };
      try {
        await Promise.race([Promise.resolve().then(() => t.fn(ctx)), new Promise((_, rej) => setTimeout(() => rej(new Error('test timed out (8s virtual)')), 8000))]);
        row(t.name, 'pass');
      } catch (e) {
        const unsup = (e && e.shimUnsupported) || /shim:unsupported/.test(String(e && e.message));
        row(t.name, unsup ? 'skip-env' : 'fail', String((e && e.stack) || e).split('\n').slice(0, 5).join(' | '));
      }
      for (const f of afters.reverse()) { try { await f(); } catch (_) { /* ignore */ } }
    }
  }
  globalThis.__REPORT__ = report; globalThis.__done = true;
})().catch((e) => { globalThis.__REPORT__ = [{ file: 'harness', name: 'harness crash', status: 'fail', error: String((e && e.stack) || e) }]; globalThis.__done = true; });
