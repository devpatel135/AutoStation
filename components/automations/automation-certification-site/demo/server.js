'use strict';
/** Tiny static server for the demo portal pages: `node demo/server.js [port]` (default 8099). */
const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');

const root = path.join(__dirname, 'portal');
const port = Number(process.argv[2] || process.env.PORT || 8099);

const server = http.createServer((req, res) => {
  const name = path.basename(new URL(req.url, 'http://localhost').pathname) || 'portal.html';
  const file = path.join(root, name);
  if (!file.startsWith(root) || !fs.existsSync(file)) {
    res.writeHead(404, { 'Content-Type': 'text/plain' });
    res.end('not found');
    return;
  }
  res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
  fs.createReadStream(file).pipe(res);
});

if (require.main === module) {
  server.listen(port, '127.0.0.1', () => console.log(`demo portal on http://127.0.0.1:${port}/ (portal.html, portal.html?v2)`));
  // `localhost` often resolves to ::1 first (Node 18+/Windows): serve the fake account picker there too, loopback only.
  const v6 = http.createServer(server.listeners('request')[0]);
  v6.on('error', () => { /* no IPv6 loopback on this host: localhost falls back to 127.0.0.1 */ });
  v6.listen(port, '::1');
}
module.exports = server;
