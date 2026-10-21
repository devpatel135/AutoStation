'use strict';
/**
 * Headless / API-first path: a small HTTP client over node:http(s) with optional client-certificate (PFX) auth.
 * Prefer this over the browser whenever the target exposes an API: faster, no DOM brittleness.
 *
 *  - Non-2xx responses throw errors already classified into the SPEC categories (401/403 AUTH_EXPIRED,
 *    502/503/504/429/5xx SYSTEM_UNAVAILABLE, 400/404/409/422 DATA_ERROR) so wrapper.runWithRetry does the right thing.
 *  - Transport failures (ECONNRESET, ETIMEDOUT, DNS...) throw NetworkError.
 *  - Built-in retry applies only to idempotent methods unless retry.idempotentOnly === false.
 *  - Corporate trust store: run Node with `--use-system-ca` (Node 22.15+/23.8+) or env NODE_USE_SYSTEM_CA=1, or pass caPath.
 *  - Client cert: scripts/export-cert.ps1 exports the user's cert-store certificate to a temp PFX + random passphrase.
 */
const fs = require('node:fs');
const http = require('node:http');
const https = require('node:https');
const E = require('./errors');

const IDEMPOTENT = new Set(['GET', 'HEAD', 'PUT', 'DELETE', 'OPTIONS']);
const MAX_BODY = 50 * 1024 * 1024;

class HttpError extends E.AutomationError {
  constructor(status, method, pathname, snippet) {
    super(`HTTP ${status} ${method} ${pathname}`);
    this.status = status;
    this.isHttp = true;
    this.category = E.classifyHttpStatus(status);
    this.snippet = snippet; // for debugging by the developer; deliberately NOT in the message
  }
}

function defaultTransport({ pfx, passphrase, ca }) {
  return function transport(opts, body) {
    return new Promise((resolve, reject) => {
      const lib = opts.protocol === 'http:' ? http : https;
      const req = lib.request({
        protocol: opts.protocol, hostname: opts.hostname, port: opts.port, path: opts.path, method: opts.method,
        headers: opts.headers, agent: false, ...(opts.protocol === 'https:' ? { pfx, passphrase, ca } : {}),
      }, (res) => {
        const chunks = [];
        let size = 0;
        res.on('data', (c) => {
          size += c.length;
          if (size > MAX_BODY) { req.destroy(Object.assign(new Error('response too large'), { code: 'EMSGSIZE' })); return; }
          chunks.push(c);
        });
        res.on('end', () => resolve({ status: res.statusCode, headers: res.headers, body: Buffer.concat(chunks).toString('utf8') }));
        res.on('error', reject);
      });
      req.setTimeout(opts.timeoutMs, () => req.destroy(Object.assign(new Error(`timed out after ${opts.timeoutMs} ms`), { code: 'ETIMEDOUT' })));
      req.on('error', reject);
      if (body) req.write(body);
      req.end();
    });
  };
}

/** Pure: build the request target; refuses absolute URLs on a different origin (no SSRF-by-parameter). */
function buildUrl(baseUrl, pathOrUrl, query) {
  const base = new URL(baseUrl);
  const u = new URL(pathOrUrl, base);
  if (u.origin !== base.origin) throw new E.DataError(`cross-origin request blocked: ${u.origin}`);
  for (const [k, v] of Object.entries(query || {})) if (v !== undefined && v !== null) u.searchParams.set(k, String(v));
  return u;
}

function client(opts = {}) {
  const {
    baseUrl, headers = {}, timeoutMs = 30000, pfxPath, passphrase, caPath,
  } = opts;
  if (!baseUrl) throw new Error('http.client: baseUrl is required');
  const retry = { retries: 0, waitMs: 500, multiplier: 2, retryOn: [502, 503, 504, 429], idempotentOnly: true, ...(opts.retry || {}) };
  const sleep = opts.sleep || ((ms) => new Promise((r) => setTimeout(r, ms)));
  let transport = opts.transport;
  const getTransport = () => {
    if (!transport) {
      transport = defaultTransport({
        pfx: pfxPath ? fs.readFileSync(pfxPath) : undefined, passphrase,
        ca: caPath ? fs.readFileSync(caPath) : undefined,
      });
    }
    return transport;
  };

  async function once(method, u, reqHeaders, bodyStr, tmo) {
    let res;
    try {
      res = await getTransport()({
        protocol: u.protocol, hostname: u.hostname, port: u.port || undefined, path: u.pathname + u.search, method,
        headers: reqHeaders, timeoutMs: tmo,
      }, bodyStr);
    } catch (err) {
      if (err instanceof E.AutomationError) throw err;
      const cat = E.classifyException(err);
      const wrapped = cat === E.Category.NETWORK_ERROR ? new E.NetworkError(`${method} ${u.pathname}: ${err.message}`) : err;
      wrapped.code = err.code;
      throw wrapped;
    }
    if (res.status >= 200 && res.status < 300) return res;
    throw new HttpError(res.status, method, u.pathname, String(res.body || '').slice(0, 200));
  }

  async function request(method, pathOrUrl, o = {}) {
    method = method.toUpperCase();
    const u = buildUrl(baseUrl, pathOrUrl, o.query);
    const h = { Accept: 'application/json', 'User-Agent': 'execution-framework/0.1.0', ...headers, ...(o.headers || {}) };
    let bodyStr = null;
    if (o.json !== undefined) {
      bodyStr = JSON.stringify(o.json);
      h['Content-Type'] = 'application/json';
    } else if (o.body !== undefined) {
      bodyStr = String(o.body);
    }
    if (bodyStr !== null) h['Content-Length'] = Buffer.byteLength(bodyStr);
    const canRetry = !retry.idempotentOnly || IDEMPOTENT.has(method);
    let attempt = 0;
    for (;;) {
      try {
        const res = await once(method, u, h, bodyStr, o.timeoutMs || timeoutMs);
        let json;
        try { json = res.body ? JSON.parse(res.body) : undefined; } catch (_) { json = undefined; }
        return { status: res.status, headers: res.headers, body: res.body, json };
      } catch (err) {
        const cat = E.classifyException(err);
        const retryable = (err instanceof HttpError && retry.retryOn.includes(err.status)) || cat === E.Category.NETWORK_ERROR;
        if (!canRetry || !retryable || attempt >= retry.retries) throw err;
        await sleep(retry.waitMs * (retry.multiplier ** attempt));
        attempt += 1;
      }
    }
  }

  const json = async (method, p, o) => {
    const r = await request(method, p, o);
    if (r.json === undefined && r.body) throw new E.DataError(`${method} ${p}: response is not JSON`);
    return r.json;
  };

  return {
    request,
    get: (p, o) => request('GET', p, o),
    post: (p, o) => request('POST', p, o),
    put: (p, o) => request('PUT', p, o),
    del: (p, o) => request('DELETE', p, o),
    getJson: (p, o) => json('GET', p, o),
    postJson: (p, body, o) => json('POST', p, { ...(o || {}), json: body }),
  };
}

module.exports = { client, HttpError, buildUrl, defaultTransport };
