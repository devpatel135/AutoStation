'use strict';
/** Tiny static server for the demo portal pages: `node demo/server.js [port]` (default 8099). */
const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');

const root = path.join(__dirname, 'portal');
const port = Number(process.argv[2] || process.env.PORT || 8099);

const server = http.createServer((req, res) => {
  const name = path.basename(new URL(req.url, 'http://localhost').pathname) || 'v1.html';
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
  server.listen(port, '127.0.0.1', () => console.log(`demo portal on http://127.0.0.1:${port}/ (v1.html, v2.html)`));
}
module.exports = server;
