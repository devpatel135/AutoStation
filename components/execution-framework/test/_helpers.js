'use strict';
/** Shared test helpers (no tests in here). */
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const events = require('../lib/events');

/** Create a temp run dir, point AUTOMAGIX_RUN_DIR at it, and restore everything after the test. */
function withRunDir(t) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'amx-fw-'));
  const saved = { run: process.env.AUTOMAGIX_RUN_DIR, home: process.env.AUTOMAGIX_HOME };
  process.env.AUTOMAGIX_RUN_DIR = path.join(dir, 'run');
  process.env.AUTOMAGIX_HOME = path.join(dir, 'home');
  events.resetCache();
  t.after(() => {
    if (saved.run === undefined) delete process.env.AUTOMAGIX_RUN_DIR; else process.env.AUTOMAGIX_RUN_DIR = saved.run;
    if (saved.home === undefined) delete process.env.AUTOMAGIX_HOME; else process.env.AUTOMAGIX_HOME = saved.home;
    events.resetCache();
    fs.rmSync(dir, { recursive: true, force: true });
  });
  return { dir, runDir: process.env.AUTOMAGIX_RUN_DIR, home: process.env.AUTOMAGIX_HOME, ctx: () => events.current() };
}

/** Collects logEvent calls: {name, data, opts}. */
function eventSink() {
  const list = [];
  const logEvent = (name, data, opts) => { list.push({ name, data, opts }); };
  logEvent.list = list;
  logEvent.named = (n) => list.filter((e) => e.name === n);
  return logEvent;
}

const neverSleep = () => Promise.resolve();

module.exports = { withRunDir, eventSink, neverSleep };
