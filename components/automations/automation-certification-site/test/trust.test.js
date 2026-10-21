'use strict';
// Host-trust rules for the certification automation (pure functions in lib/login.js).
const test = require('node:test');
const assert = require('node:assert/strict');
const {
  DEFAULT_URL, DEFAULT_ALLOWED_HOSTS, parseAllowedHosts, hostAllowed, isMicrosoftLoginHost, isTrustedHost, isAllowedPortal, redirectAllowed, postClickOk, classifyPage,
} = require('../lib/login');

const PORTAL = 'develop.deloitte.com';
const HOSTS = parseAllowedHosts('');

test('defaults: the portal URL itself is allowed; the allow-list is exactly develop.deloitte.com', () => {
  assert.deepEqual([...DEFAULT_ALLOWED_HOSTS], [PORTAL]);
  assert.deepEqual(HOSTS, [PORTAL]);
  const r = isAllowedPortal(DEFAULT_URL, { allowedHosts: HOSTS });
  assert.equal(r.ok, true);
  assert.equal(r.host, PORTAL);
});

test('isAllowedPortal accepts the portal in harmless variants (case, default port, path, query, fragment)', () => {
  for (const u of ['https://develop.deloitte.com/', 'HTTPS://DEVELOP.DELOITTE.COM/x', 'https://Develop.Deloitte.com:443/a?b=c#d', 'https://develop.deloitte.com']) {
    assert.equal(isAllowedPortal(u, { allowedHosts: HOSTS }).ok, true, u);
  }
});

test('isAllowedPortal refuses look-alikes, userinfo tricks, other schemes, odd ports, IPs and malformed input', () => {
  const bad = [
    'https://develop.deloitte.com.evil.com/', 'https://evil.com/develop.deloitte.com', 'https://evil-develop.deloitte.com/', 'https://xdevelop.deloitte.com/',
    'https://deloitte.com/', 'https://www.deloitte.com/', 'https://sub.develop.deloitte.com/', 'https://develop.deloitte.com.attacker.example/',
    'https://develop.deloitte.com@evil.com/', 'https://develop.deloitte.com:443@evil.com/', 'https://user:pw@develop.deloitte.com/', 'https://develop.deloitte.com%40evil.com/',
    'https://develop.deloitte.com\\@evil.com/', 'https://develop.deloitte.com\\.evil.com/', 'https://develop.deloitte.com/\\..\\x',
    'https://develop.deloitte.com./', 'https://develop.deloitte.com.:443/',
    'https://develop.deloitte.com:8443/', 'https://develop.deloitte.com:444/', 'https://develop.deloitte.com:80/',
    'http://develop.deloitte.com/', 'ftp://develop.deloitte.com/', 'javascript:alert(1)', 'JaVaScRiPt://develop.deloitte.com/%0aalert(1)', 'data:text/html,<script>1</script>',
    'file:///c:/x', 'about:blank', 'blob:https://develop.deloitte.com/x', 'view-source:https://develop.deloitte.com/', 'chrome://settings',
    'https://xn--develop-deloitte-com-xxxx.com/', 'https://develop.deloitte.xn--com-pxa/', 'https://develop.deloitte.c\u043em/', 'https://d\u0435velop.deloitte.com/',
    'https://127.0.0.1/', 'https://localhost/', 'https://2130706433/', 'https://[::1]/', 'https://0x7f.1/',
    'https://develop.deloitte.com/\n', 'https://develop.deloitte.com/ x', ' https://develop.deloitte.com/', 'https://develop.deloitte.com/\u0000', 'https://develop.deloitte.com/\t.evil.com',
    '', '   ', 'develop.deloitte.com', '//develop.deloitte.com/', 'https://', 'https:///develop.deloitte.com', undefined, null, `https://develop.deloitte.com/${'a'.repeat(3000)}`,
  ];
  for (const u of bad) {
    const r = isAllowedPortal(u, { allowedHosts: HOSTS });
    assert.equal(r.ok, false, JSON.stringify(u));
    assert.ok(r.reason, JSON.stringify(u));
  }
});

test('allowed_hosts extends the allow-list with exact hosts only', () => {
  const hosts = parseAllowedHosts('Auth.Deloitte.com, sso.example.org:8443  extra.example.net');
  assert.deepEqual(hosts, [PORTAL, 'auth.deloitte.com', 'sso.example.org:8443', 'extra.example.net']);
  assert.equal(isAllowedPortal('https://auth.deloitte.com/x', { allowedHosts: hosts }).ok, true);
  assert.equal(isAllowedPortal('https://sso.example.org:8443/x', { allowedHosts: hosts }).ok, true);
  assert.equal(isAllowedPortal('https://sso.example.org/x', { allowedHosts: hosts }).ok, false, 'port is part of the identity');
  assert.deepEqual(parseAllowedHosts(['a.example.com']), [PORTAL, 'a.example.com']);
  assert.deepEqual(parseAllowedHosts(undefined), [PORTAL]);
  for (const bad of ['*.deloitte.com', 'https://evil.com', 'evil.com/path', 'evil.com.', '.evil.com', 'a b@c.com', 'user@evil.com', 'evil_host.com', '127.0.0.1', '127.0.0.1:8099', 'localhost', 'localhost:3000', 'intranet', '[::1]', '10.0.0.1', '2130706433', 'x'.repeat(10) + ':99999x']) {
    assert.throws(() => parseAllowedHosts(bad), /invalid allowed_hosts entry/, bad);
  }
});

test('test mode (AMX_ALLOW_TEST_HOSTS=1) is the only way to use http and loopback hosts (CI smoke configuration)', () => {
  const hosts = parseAllowedHosts('127.0.0.1:8099', { allowTestHosts: true });
  assert.deepEqual(hosts, [PORTAL, '127.0.0.1:8099']);
  assert.equal(isAllowedPortal('http://127.0.0.1:8099/portal.html', { allowedHosts: hosts, allowTestHosts: true }).ok, true);
  assert.equal(isAllowedPortal('http://127.0.0.1:8099/portal.html', { allowedHosts: hosts, allowTestHosts: false }).ok, false);
  assert.equal(isAllowedPortal('https://127.0.0.1:8099/portal.html', { allowedHosts: hosts, allowTestHosts: false }).ok, false, 'IP hosts need test mode even over https');
  assert.equal(isAllowedPortal('http://127.0.0.1:9999/portal.html', { allowedHosts: hosts, allowTestHosts: true }).ok, false, 'still exact host:port');
  assert.equal(isAllowedPortal('http://evil.example/', { allowedHosts: hosts, allowTestHosts: true }).ok, false);
  assert.equal(isAllowedPortal('javascript:alert(1)', { allowedHosts: hosts, allowTestHosts: true }).ok, false);
});

test('hostAllowed is exact, case-insensitive and refuses trailing dots / empties', () => {
  assert.equal(hostAllowed('develop.deloitte.com', HOSTS), true);
  assert.equal(hostAllowed('DEVELOP.deloitte.COM', HOSTS), true);
  for (const h of ['develop.deloitte.com.', 'develop.deloitte.com:8443', 'evil.develop.deloitte.com', 'develop.deloitte.com.evil.com', '', undefined, null, 'localhost']) assert.equal(hostAllowed(h, HOSTS), false, String(h));
  assert.equal(hostAllowed('develop.deloitte.com', []), false);
});

test('isMicrosoftLoginHost / isTrustedHost: only real Microsoft sign-in hosts, portal and allow-listed hosts', () => {
  for (const h of ['login.microsoftonline.com', 'LOGIN.MICROSOFTONLINE.COM', 'login.live.com', 'login.microsoft.com', 'device.login.microsoftonline.com']) assert.equal(isMicrosoftLoginHost(h), true, h);
  for (const h of ['login.microsoftonline.com.evil.net', 'evil-login.microsoftonline.com.example.org', 'login.microsoftonline.com:8443', 'login.microsoftonline.com.', 'xlogin.microsoftonline.com', 'microsoftonline.com', 'login.microsoftonline.co', '', undefined]) {
    assert.equal(isMicrosoftLoginHost(h), false, String(h));
  }
  assert.equal(isMicrosoftLoginHost('localhost:8099'), false);
  assert.equal(isMicrosoftLoginHost('localhost:8099', { pickerHost: 'localhost:8099' }), true);
  assert.equal(isTrustedHost('develop.deloitte.com', { allowedHosts: HOSTS }), true);
  assert.equal(isTrustedHost('login.microsoftonline.com', { allowedHosts: HOSTS }), true);
  assert.equal(isTrustedHost('evil.example', { allowedHosts: HOSTS }), false);
});

const ms = (qs) => `https://login.microsoftonline.com/common/oauth2/v2.0/authorize?${qs}`;
const enc = encodeURIComponent;
const ctx = { portalHost: PORTAL, allowedHosts: HOSTS };

test('redirectAllowed: the sign-in must have been started for the portal (or an allow-listed host)', () => {
  const ok = [
    ms(`client_id=abc&redirect_uri=${enc('https://develop.deloitte.com/ai-season-of-certifications')}&response_type=code`),
    ms(`redirect_uri=${enc('https://DEVELOP.deloitte.com/')}`),
    ms(`CLIENT_ID=abc&Redirect_URI=${enc('https://develop.deloitte.com/x?y=1#z')}`),
    'https://login.microsoftonline.com/common/login', // later page of the same sign-in: nothing to verify
    'https://login.microsoftonline.com/common/oauth2/authorize?sso_reload=true',
    'https://login.microsoftonline.com/common/wsfed?wa=wsignin1.0&wreply=' + enc('https://develop.deloitte.com/signin'),
  ];
  for (const u of ok) assert.equal(redirectAllowed(u, ctx).ok, true, u);
  const bad = [
    ms(`client_id=abc&redirect_uri=${enc('https://evil.example/cb')}`),
    ms(`client_id=abc&redirect_uri=${enc('https://develop.deloitte.com.evil.example/cb')}`),
    ms(`client_id=abc&redirect_uri=${enc('https://develop.deloitte.com@evil.example/cb')}`),
    ms(`client_id=abc&redirect_uri=${enc('https://develop.deloitte.com:8443/cb')}`),
    ms(`client_id=abc&redirect_uri=${enc('http://develop.deloitte.com/cb')}`),
    ms(`client_id=abc&redirect_uri=${enc('https://user:pw@develop.deloitte.com/cb')}`),
    ms(`client_id=abc&redirect_uri=${enc('javascript:alert(1)')}`),
    ms(`client_id=abc&redirect_uri=${enc('//develop.deloitte.com/cb')}`),
    ms(`client_id=abc&redirect_uri=${enc('/cb')}`),
    ms(`client_id=abc&redirect_uri=${enc(enc('https://develop.deloitte.com/cb'))}`), // double-encoded: not a URL after one decode
    ms('client_id=abc&redirect_uri='),
    ms(`redirect_uri=${enc('https://develop.deloitte.com/cb')}&redirect_uri=${enc('https://evil.example/cb')}`), // second one wins on some servers
    ms(`redirect_uri=${enc('https://evil.example/cb')}&redirect_uri=${enc('https://develop.deloitte.com/cb')}`),
    ms(`client_id=abc&wreply=${enc('https://evil.example/')}`),
    ms('client_id=abc&response_type=code'), // client_id with no redirect_uri: cannot be verified
    `https://login.microsoftonline.com/common/login#redirect_uri=${enc('https://evil.example/')}`,
    'not a url',
    '',
  ];
  for (const u of bad) {
    const r = redirectAllowed(u, ctx);
    assert.equal(r.ok, false, u);
    assert.ok(r.reason, u);
  }
  assert.equal(redirectAllowed(ms(`redirect_uri=${enc('https://evil.example/cb')}`), ctx).redirectHost, 'evil.example');
});

test('redirectAllowed honours extra allow-listed hosts and test-mode http', () => {
  const hosts = parseAllowedHosts('auth.deloitte.com');
  const u = ms(`client_id=abc&redirect_uri=${enc('https://auth.deloitte.com/cb')}`);
  assert.equal(redirectAllowed(u, { portalHost: PORTAL, allowedHosts: hosts }).ok, true);
  assert.equal(redirectAllowed(u, { portalHost: PORTAL, allowedHosts: HOSTS }).ok, false);
  const local = ms(`client_id=abc&redirect_uri=${enc('http://127.0.0.1:8099/portal.html')}`);
  const th = parseAllowedHosts('127.0.0.1:8099', { allowTestHosts: true });
  assert.equal(redirectAllowed(local, { portalHost: '127.0.0.1:8099', allowedHosts: th, allowTestHosts: true }).ok, true);
  assert.equal(redirectAllowed(local, { portalHost: '127.0.0.1:8099', allowedHosts: th, allowTestHosts: false }).ok, false);
});

test('postClickOk: after the click only Microsoft sign-in, the portal and allow-listed hosts are acceptable', () => {
  for (const u of ['https://develop.deloitte.com/ai-season-of-certifications', 'https://login.microsoftonline.com/common/login', 'about:blank']) assert.equal(postClickOk(u, { allowedHosts: HOSTS }), true, u);
  for (const u of ['https://evil.example/', 'https://develop.deloitte.com.evil.example/', 'https://login.microsoftonline.com.evil.example/', 'https://develop.deloitte.com@evil.example/', 'javascript:alert(1)', 'data:text/html,x', '', 'not a url', undefined, 'chrome-error://chromewebdata/']) {
    assert.equal(postClickOk(u, { allowedHosts: HOSTS }), false, String(u));
  }
  assert.equal(postClickOk('http://localhost:8099/login.html', { allowedHosts: HOSTS, pickerHost: 'localhost:8099' }), true);
  assert.equal(postClickOk('http://localhost:8099/login.html', { allowedHosts: HOSTS }), false);
});

test('CI smoke configuration still classifies and passes every gate end to end (pure walk-through)', () => {
  const allowTestHosts = true;
  const allowedHosts = parseAllowedHosts('127.0.0.1:8099', { allowTestHosts });
  const portalUrl = 'http://127.0.0.1:8099/portal.html';
  const pc = isAllowedPortal(portalUrl, { allowedHosts, allowTestHosts });
  assert.equal(pc.ok, true);
  const pickerHost = 'localhost:8099';
  const picker = { url: 'http://localhost:8099/login.html', hasTile: true, text: '' };
  assert.equal(classifyPage(picker, portalUrl, { pickerHost }), 'account-picker');
  assert.equal(redirectAllowed(picker.url, { portalHost: pc.host, allowedHosts, allowTestHosts }).ok, true);
  assert.equal(postClickOk('http://127.0.0.1:8099/portal.html#signed-in=devpatel@deloitte.com', { allowedHosts, pickerHost }), true);
  // the same inputs without test mode are refused up front
  assert.equal(isAllowedPortal(portalUrl, { allowedHosts: HOSTS, allowTestHosts: false }).ok, false);
  assert.throws(() => parseAllowedHosts('127.0.0.1:8099', { allowTestHosts: false }), /invalid allowed_hosts entry/);
});
