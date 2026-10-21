'use strict';
/**
 * Site adapters: a tiny plugin interface so site-specific know-how lives in one place and automations stay thin.
 *
 *   adapter = { name: 'my-portal', description?: string, ...anyMethods }
 *   adapters.register('my-portal', adapter);   // usually in your automation's own startup
 *   const a = adapters.get('my-portal');
 *
 * Shipped adapters: generic-table, generic-form, download, sso-detect. To add your own, export an object with a `name`
 * and whatever async methods your automations need (they receive a Playwright `page` or a resilientPage proxy), then
 * call `register`. Shared adapters belong in this folder and in `registerBuiltins` below.
 */

const registry = new Map();

function register(name, adapter) {
  if (!name || typeof name !== 'string') throw new Error('adapter name must be a non-empty string');
  if (!adapter || typeof adapter !== 'object') throw new Error('adapter must be an object');
  if (registry.has(name)) throw new Error(`adapter ${name} is already registered`);
  registry.set(name, { ...adapter, name });
  return registry.get(name);
}

function get(name) {
  const a = registry.get(name);
  if (!a) throw new Error(`unknown adapter ${name}; registered: ${[...registry.keys()].join(', ') || '(none)'}`);
  return a;
}

function has(name) {
  return registry.has(name);
}

function list() {
  return [...registry.values()].map((a) => ({ name: a.name, description: a.description || '' }));
}

function unregister(name) {
  return registry.delete(name);
}

function registerBuiltins() {
  for (const mod of ['generic-table', 'generic-form', 'download', 'sso-detect']) {
    const adapter = require(`./${mod}`);
    if (!registry.has(adapter.name)) register(adapter.name, adapter);
  }
}

registerBuiltins();

module.exports = { register, get, has, list, unregister };
