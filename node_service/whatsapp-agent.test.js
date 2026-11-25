'use strict';

process.env.SOURCE_GROUP_IDS = 'src1@g.us,src2@g.us';
process.env.TARGET_GROUP_ID = 'target@g.us';

const test = require('node:test');
const assert = require('node:assert/strict');

const {
  isFromSourceGroup,
  containsJobKeywords,
} = require('./whatsapp-agent');

test('isFromSourceGroup returns true for a listed source group', () => {
  assert.equal(isFromSourceGroup('123@g.us', ['123@g.us', '456@g.us']), true);
});

test('isFromSourceGroup returns false for an unlisted group', () => {
  assert.equal(isFromSourceGroup('999@g.us', ['123@g.us', '456@g.us']), false);
});

test('containsJobKeywords detects Hebrew job keywords', () => {
  assert.equal(containsJobKeywords('דרוש מפתח Python'), true);
  assert.equal(containsJobKeywords('משרה פנויה'), true);
  assert.equal(containsJobKeywords('נא לשלוח CV'), true);
});

test('containsJobKeywords returns false when no keyword is present', () => {
  assert.equal(containsJobKeywords('שיחת חולין רגילה'), false);
});

const axios = require('axios');
const {
  handleIncomingMessage,
  getConfig,
} = require('./whatsapp-agent');

// By default Python is "unreachable", so getConfig falls back to the env set above.
axios.get = async () => { throw new Error('no python'); };
const firstSourceGroupId = 'src1@g.us';
const TARGET_GROUP_ID = 'target@g.us';

test('handleIncomingMessage ignores messages from non-source groups', async (t) => {
  const postMock = t.mock.method(axios, 'post', async () => ({ data: {} }));
  const sentMessages = [];
  const fakeClient = { sendMessage: async (to, text) => sentMessages.push({ to, text }) };

  await handleIncomingMessage(fakeClient, { from: 'unknown@g.us', body: 'דרוש מפתח Python' });

  assert.equal(postMock.mock.callCount(), 0);
  assert.equal(sentMessages.length, 0);
});

test('handleIncomingMessage ignores messages without job keywords', async (t) => {
  const postMock = t.mock.method(axios, 'post', async () => ({ data: {} }));
  const sentMessages = [];
  const fakeClient = { sendMessage: async (to, text) => sentMessages.push({ to, text }) };

  await handleIncomingMessage(fakeClient, { from: firstSourceGroupId, body: 'שיחת חולין' });

  assert.equal(postMock.mock.callCount(), 0);
  assert.equal(sentMessages.length, 0);
});

test('handleIncomingMessage forwards matched job postings to the target group', async (t) => {
  t.mock.method(axios, 'post', async () => ({
    data: { forward: true, whatsapp_text: 'מפתח Python' },
  }));
  const sentMessages = [];
  const fakeClient = { sendMessage: async (to, text) => sentMessages.push({ to, text }) };

  await handleIncomingMessage(fakeClient, {
    from: firstSourceGroupId,
    body: 'דרוש מפתח Python ב-Acme',
  });

  assert.equal(sentMessages.length, 1);
  assert.equal(sentMessages[0].to, TARGET_GROUP_ID);
  assert.match(sentMessages[0].text, /מפתח Python/);
});

test('each posting is forwarded with its own text, timeout is passed to axios', async (t) => {
  const postMock = t.mock.method(axios, 'post', async (url, body) => ({
    data: { forward: true, whatsapp_text: `msg ${body.text}` },
  }));
  const sentMessages = [];
  const fakeClient = { sendMessage: async (to, text) => sentMessages.push({ to, text }) };

  await Promise.all(['דרוש מפתח ALPHA', 'דרוש מפתח BETA'].map((body) =>
    handleIncomingMessage(fakeClient, { from: firstSourceGroupId, body })
  ));

  assert.equal(sentMessages.length, 2);
  assert.ok(sentMessages.some((m) => m.text.includes('ALPHA') && !m.text.includes('BETA')));
  assert.ok(sentMessages.some((m) => m.text.includes('BETA') && !m.text.includes('ALPHA')));
  assert.ok(postMock.mock.calls.every((c) => c.arguments[2].timeout >= 1000));
});

test('a timed-out analysis is skipped without forwarding', async (t) => {
  t.mock.method(axios, 'post', async () => {
    throw Object.assign(new Error('timeout of 180000ms exceeded'), { code: 'ECONNABORTED' });
  });
  const sentMessages = [];
  const fakeClient = { sendMessage: async (to, text) => sentMessages.push({ to, text }) };

  await assert.doesNotReject(
    handleIncomingMessage(fakeClient, { from: firstSourceGroupId, body: 'דרוש מפתח' })
  );
  assert.equal(sentMessages.length, 0);
});

test('does not send when Python says not to forward (below threshold / duplicate)', async (t) => {
  t.mock.method(axios, 'post', async () => ({ data: { odds: 90, forward: false } }));
  const sent = [];
  const fakeClient = { sendMessage: async (to, text) => sent.push({ to, text }) };
  await handleIncomingMessage(fakeClient, { from: firstSourceGroupId, body: 'דרוש מפתח' });
  assert.equal(sent.length, 0);
});

test('does not send to WhatsApp when whatsapp is not in OUTPUTS', async (t) => {
  t.mock.method(axios, 'post', async () => ({ data: { forward: true, whatsapp_text: 'hi' } }));
  const sent = [];
  const fakeClient = { sendMessage: async (to, text) => sent.push({ to, text }) };
  require('./whatsapp-agent').resetConfigCache();
  process.env.OUTPUTS = 'file';
  try {
    await handleIncomingMessage(fakeClient, { from: firstSourceGroupId, body: 'דרוש מפתח' });
  } finally {
    delete process.env.OUTPUTS;
    require('./whatsapp-agent').resetConfigCache();
  }
  assert.equal(sent.length, 0);
});

test('sends the ready text and passes the group name to Python', async (t) => {
  const postMock = t.mock.method(axios, 'post', async () => ({ data: { forward: true, whatsapp_text: 'READY' } }));
  const sent = [];
  const fakeClient = { sendMessage: async (to, text) => sent.push({ to, text }) };
  await handleIncomingMessage(fakeClient, {
    from: firstSourceGroupId,
    body: 'דרוש מפתח',
    getChat: async () => ({ name: 'Runtime Group' }),
  });
  assert.deepEqual(sent, [{ to: TARGET_GROUP_ID, text: 'READY' }]);
  assert.equal(postMock.mock.calls[0].arguments[1].group, 'Runtime Group');
});

test('pushes qr then ready state (with groups) to Python', async (t) => {
  const { registerStateEvents } = require('./whatsapp-agent');
  const { EventEmitter } = require('node:events');
  const bodies = [];
  t.mock.method(axios, 'post', async (url, body) => {
    assert.match(url, /\/state$/);
    bodies.push(JSON.parse(JSON.stringify(body)));
    return { data: {} };
  });
  const client = new EventEmitter();
  client.getChats = async () => [
    { isGroup: true, name: 'G1', id: { _serialized: 'g1@g.us' } },
    { isGroup: false, name: 'Person', id: { _serialized: 'p@c.us' } },
  ];
  registerStateEvents(client);

  client.emit('qr', 'QR-STRING');
  client.emit('ready');
  await new Promise((r) => setTimeout(r, 20));

  assert.deepEqual(bodies[0], { status: 'qr', qr: 'QR-STRING', groups: [] });
  assert.equal(bodies[1].status, 'ready');
  assert.equal(bodies[1].qr, null);
  assert.deepEqual(bodies.at(-1).groups, [{ name: 'G1', jid: 'g1@g.us' }]);
});

test('a failing state push does not throw', async (t) => {
  const { pushState } = require('./whatsapp-agent');
  t.mock.method(axios, 'post', async () => { throw new Error('down'); });
  await pushState({ status: 'disconnected' });
});

test('state pushes are delivered in order even when an earlier one is slow', async (t) => {
  const { pushState } = require('./whatsapp-agent');
  const order = [];
  t.mock.method(axios, 'post', async (url, body) => {
    const status = body.status;
    await new Promise((r) => setTimeout(r, status === 'qr' ? 30 : 0));
    order.push(status);
    return { data: {} };
  });
  pushState({ status: 'qr' });
  await pushState({ status: 'ready' });
  assert.deepEqual(order, ['qr', 'ready']);
});

test('getConfig falls back to env when Python is unreachable', async () => {
  require('./whatsapp-agent').resetConfigCache();
  const cfg = await getConfig();
  assert.deepEqual(cfg.sourceGroupIds, ['src1@g.us', 'src2@g.us']);
  assert.equal(cfg.targetGroupId, 'target@g.us');
});

test('getConfig fetches Python config, caches it, and picks up changes after the TTL', async (t) => {
  const { resetConfigCache } = require('./whatsapp-agent');
  let groups = ['new1@g.us'];
  const getMock = t.mock.method(axios, 'get', async () => ({
    data: { sourceGroupIds: groups, targetGroupId: 'T', outputs: ['whatsapp'] },
  }));
  t.mock.timers.enable({ apis: ['Date'], now: 1000 });
  resetConfigCache();
  assert.deepEqual((await getConfig()).sourceGroupIds, ['new1@g.us']);
  groups = ['new2@g.us'];
  assert.deepEqual((await getConfig()).sourceGroupIds, ['new1@g.us']); // cached
  assert.equal(getMock.mock.callCount(), 1);
  t.mock.timers.tick(31000);
  assert.deepEqual((await getConfig()).sourceGroupIds, ['new2@g.us']); // TTL expired
  resetConfigCache();
});

test('near-simultaneous postings are analysed one at a time and both delivered', async (t) => {
  let active = 0, maxActive = 0;
  t.mock.method(axios, 'post', async (url, body) => {
    active++; maxActive = Math.max(maxActive, active);
    await new Promise((r) => setTimeout(r, 20));
    active--;
    return { data: { forward: true, whatsapp_text: `msg ${body.text}` } };
  });
  const sentMessages = [];
  const fakeClient = { sendMessage: async (to, text) => sentMessages.push({ to, text }) };

  await Promise.all(['דרוש מפתח ONE', 'דרוש מפתח TWO'].map((body) =>
    handleIncomingMessage(fakeClient, { from: firstSourceGroupId, body })
  ));

  assert.equal(maxActive, 1);
  assert.equal(sentMessages.length, 2);
});

test('clientOptions pins WhatsApp Web only when WWEBJS_WEB_VERSION is set', () => {
  const { clientOptions } = require('./whatsapp-agent');
  assert.equal(clientOptions('').webVersionCache, undefined);
  const o = clientOptions('2.3000.1');
  assert.equal(o.webVersion, '2.3000.1');
  assert.equal(o.webVersionCache.type, 'remote');
  assert.match(o.webVersionCache.remotePath, /wa-version\/main\/html\/2\.3000\.1\.html$/);
});
