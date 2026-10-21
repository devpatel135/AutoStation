'use strict';
/**
 * execution-framework - shared runtime for AUTOSTATION automations.
 *
 *   const fw = require(process.env.AUTOMAGIX_FRAMEWORK_DIR);
 *   fw.wrapper.entrypoint(async (fw) => { ... });
 *
 * Heavy modules (playwright-core, CDP, resilience, agent, MCP, adapters) load lazily so a pure-API automation
 * (`fw.wrapper` + `fw.http`) never pays for or requires a browser.
 */
const wrapper = require('./lib/wrapper');
const pkg = require('./package.json');

const fw = {
  version: pkg.version,
  wrapper: {
    ...wrapper,
    // main(fw) receives this framework object
    entrypoint: (main, io) => wrapper.entrypoint(main, fw, io),
  },
  schema: require('./lib/schema'),
  redact: require('./lib/redact'),
  http: require('./lib/http'),
};

function lazy(name, loader) {
  Object.defineProperty(fw, name, { enumerable: true, configurable: true, get() {
    const v = loader();
    Object.defineProperty(fw, name, { enumerable: true, value: v });
    return v;
  } });
}

lazy('browser', () => require('./lib/browser'));
lazy('cdp', () => require('./lib/cdp'));
lazy('resilience', () => require('./lib/resilience'));
lazy('agent', () => require('./lib/agent'));
lazy('mcp', () => require('./lib/mcp-server'));
lazy('adapters', () => require('./lib/adapters'));

module.exports = fw;
