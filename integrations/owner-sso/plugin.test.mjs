import test from 'node:test';
import assert from 'node:assert/strict';
import net from 'node:net';
import {EventEmitter, once} from 'node:events';
import fs from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {apply} from './plugin.mjs';

test('private socket issues the native cookie, revokes a valid WebSocket and closes on unload', {skip: process.platform !== 'linux'}, async () => {
  const root = await fs.mkdtemp(join(tmpdir(), 'owner-sso-test-'));
  const path = join(root, 'sso.sock');
  const browser = new EventEmitter();
  let dispose, exchanges = 0;
  const origin = 'https://assistant.home.arpa:9443';
  const ctx = {webServer: {server: browser}, effect(init) { dispose = init(); }, connection: {
    authenticatedUrl(value) { assert.equal(value, origin + '/'); return origin + '/?fixture=1'; },
    authorizeIndex(req, res) {
      assert.equal(req.headers.host, 'assistant.home.arpa:9443'); exchanges++;
      res.writeHead(303, {'set-cookie': 'dsh-auth-local=v1.fixture; HttpOnly'});
    }
  }};
  async function call(msg) {
    const client = net.createConnection(path);
    await once(client, 'connect');
    client.write(JSON.stringify(msg) + '\n');
    let result = '';
    for await (const part of client) result += part;
    return JSON.parse(result);
  }
  try {
    await apply(ctx, {origin, socketPath:path});
    assert.equal((await fs.stat(path)).mode & 0o777, 0o600);
    assert.equal((await call({op:'cookie'})).cookie,'dsh-auth-local=v1.fixture');
    assert.equal((await call({op:'cookie'})).cookie,'dsh-auth-local=v1.fixture');
    assert.equal(exchanges, 1);
    const id = 'a'.repeat(64), ws = new EventEmitter();
    let destroyed = false;
    ws.destroy = () => {destroyed = true; ws.emit('close');};
    browser.emit('upgrade', {headers:{'x-xixi-owner-session':id,'x-xixi-owner-expires':Math.floor(Date.now()/1000)+10}}, ws);
    assert.equal(destroyed, false);
    assert.deepEqual(await call({op:'revoke',session:id}),{revoked:true});
    assert.equal(destroyed, true);
    assert.ok((await call({op:'invalid'})).error);
    destroyed = false;
    browser.emit('upgrade', {headers:{}}, ws);
    assert.equal(destroyed, true);
  } finally {
    if (dispose) await dispose();
    assert.equal(browser.listenerCount('upgrade'),0);
    await fs.rm(root,{recursive:true,force:true});
  }
});
