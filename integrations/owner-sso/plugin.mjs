/** Optional DSH Web single sign-on through a private same-user Unix socket. */
import net from 'node:net';
import fs from 'node:fs/promises';
import {dirname, isAbsolute} from 'node:path';

export const name = 'xixi-owner-gateway';
export const inject = ['connection', 'webServer'];
const sessionId = /^[a-f0-9]{64}$/;
const maxAge = 7 * 24 * 60 * 60 * 1000;

export async function apply(ctx, config = {}) {
  const path = config.socketPath;
  const origin = new URL(config.origin);
  if (origin.protocol !== 'https:' || origin.href !== origin.origin + '/' || !isAbsolute(path))
    throw Error('Expected a dedicated HTTPS origin and an absolute socketPath');
  const dir = await fs.lstat(dirname(path));
  if (!dir.isDirectory() || dir.uid !== process.getuid() || (dir.mode & 0o077))
    throw Error('Use a private 0700 socket directory owned by the service user');

  const sessions = new Map();
  const clients = new Set();
  let cached, attached;
  function guard(req, ws) {
    const id = req.headers['x-xixi-owner-session'];
    const expires = Number(req.headers['x-xixi-owner-expires']) * 1000;
    const ttl = expires - Date.now();
    if (!sessionId.test(id ?? '') || !Number.isFinite(ttl) || ttl <= 0 || ttl > maxAge + 5000) {
      ws.destroy();
      return;
    }
    if (!sessions.has(id)) sessions.set(id, new Set());
    sessions.get(id).add(ws);
    const timer = setTimeout(() => ws.destroy(), ttl).unref();
    ws.once('close', () => {
      clearTimeout(timer);
      sessions.get(id)?.delete(ws);
      if (!sessions.get(id)?.size) sessions.delete(id);
    });
  }
  function attach() {
    const server = ctx.webServer.server;
    if (server === attached) return;
    attached?.removeListener('upgrade', guard);
    attached = server;
    attached?.prependListener('upgrade', guard);
  }
  function cookie() {
    if (cached?.until > Date.now()) return cached.value;
    const url = new URL(ctx.connection.authenticatedUrl(origin.href));
    let value;
    ctx.connection.authorizeIndex({method:'GET', url:url.pathname + url.search, headers:{host:origin.host}},
      {writeHead(code, headers) { if (code === 303) value = headers['set-cookie']?.split(';', 1)[0]; }, end() {}});
    if (typeof value !== 'string' || !/^dsh-auth-[A-Za-z0-9_-]+=v1\.[A-Za-z0-9_.-]+$/.test(value)) throw Error('DSH native sign-in failed');
    cached = {value, until: Date.now() + 60000};
    return value;
  }
  const server = net.createServer(socket => {
    clients.add(socket);
    socket.on('close', () => clients.delete(socket));
    socket.on('error', () => {});
    socket.setTimeout(2000, () => socket.destroy());
    let buffer = '', done = false;
    socket.on('data', chunk => {
      if (done) return;
      buffer += chunk;
      if (buffer.length > 1024) return socket.destroy();
      if (!buffer.includes('\n')) return;
      done = true;
      try {
        const msg = JSON.parse(buffer);
        if (msg.op === 'cookie' && Object.keys(msg).length === 1) {
          socket.end(JSON.stringify({cookie: cookie()}) + '\n');
        } else if (msg.op === 'revoke' && Object.keys(msg).length === 2 && sessionId.test(msg.session)) {
          for (const ws of sessions.get(msg.session) ?? []) ws.destroy();
          socket.end('{"revoked":true}\n');
        } else throw Error('Unsupported request');
      } catch { socket.end('{"error":"sso_unavailable"}\n'); }
    });
  });
  // Bind exclusively. A stale or active socket requires the operator to inspect ownership.
  await new Promise((ok, no) => { server.once('error', no); server.listen(path, ok); });
  try {
    await fs.chmod(path, 0o600);
    attach();
    const watcher = setInterval(attach, 250).unref();
    ctx.effect(() => async () => {
      clearInterval(watcher);
      attached?.removeListener('upgrade', guard);
      for (const sockets of sessions.values()) for (const ws of sockets) ws.destroy();
      for (const socket of clients) socket.destroy();
      await new Promise(resolve => server.close(resolve));
    });
  } catch (error) {
    await new Promise(resolve => server.close(resolve));
    throw error;
  }
}
export default {name, inject, apply};
