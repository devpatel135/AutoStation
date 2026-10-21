'use strict';
/** iframe-helper: find a frame by URL pattern / name with a polling wait (frames attach late), then work inside it. */

const realSleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/** Pure: does frame (anything with url()/name()) match {urlPattern: RegExp|string, name: string}? */
function frameMatches(frame, { urlPattern, name } = {}) {
  let url = '';
  let fname = '';
  try { url = String(frame.url()); } catch (_) { url = ''; }
  try { fname = String(frame.name()); } catch (_) { fname = ''; }
  if (name !== undefined && fname !== name) return false;
  if (urlPattern === undefined) return name !== undefined;
  if (urlPattern instanceof RegExp) return urlPattern.test(url);
  return url.includes(String(urlPattern));
}

/** Non-waiting lookup. Main frame is excluded. Returns the first match or null. */
function findFrame(page, match) {
  const raw = page.raw || page;
  const main = typeof raw.mainFrame === 'function' ? raw.mainFrame() : null;
  return raw.frames().find((f) => f !== main && frameMatches(f, match)) || null;
}

/** Wait (polling) until a matching frame exists. Rejects with TimeoutError naming what was awaited. */
async function waitForFrame(page, match, { timeoutMs = 10000, pollMs = 200, sleep = realSleep, now = Date.now } = {}) {
  const deadline = now() + timeoutMs;
  for (;;) {
    const f = findFrame(page, match);
    if (f) return f;
    if (now() >= deadline) {
      const e = new Error(`iframe-helper: no frame matching ${JSON.stringify({ urlPattern: String(match.urlPattern), name: match.name })} within ${timeoutMs} ms`);
      e.name = 'TimeoutError';
      throw e;
    }
    await sleep(pollMs);
  }
}

/** Run fn(frame) once the frame exists and has finished loading its DOM. */
async function withFrame(page, match, fn, opts = {}) {
  const frame = await waitForFrame(page, match, opts);
  if (typeof frame.waitForLoadState === 'function') {
    try { await frame.waitForLoadState('domcontentloaded', { timeout: opts.timeoutMs || 10000 }); } catch (_) { /* a frame that never fires load is still usable */ }
  }
  return fn(frame);
}

module.exports = {
  name: 'iframe-helper',
  description: 'Find a frame by URL pattern/name with a polling wait; run work inside it',
  frameMatches, findFrame, waitForFrame, withFrame,
};
