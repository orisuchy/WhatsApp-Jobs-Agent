'use strict';

process.env.SOURCE_GROUP_IDS = 'src1@g.us,src2@g.us';
process.env.TARGET_GROUP_ID = 'target@g.us';

const test = require('node:test');
const assert = require('node:assert/strict');

const {
  isFromSourceGroup,
  containsJobKeywords,
  formatForwardMessage,
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

test('containsJobKeywords returns false for empty or undefined text', () => {
  assert.equal(containsJobKeywords(''), false);
  assert.equal(containsJobKeywords(undefined), false);
});

test('formatForwardMessage includes reason, summary and original text', () => {
  const formatted = formatForwardMessage({
    reason: 'התאמה טובה',
    summary: 'מפתח Python',
    originalText: 'דרוש מפתח Python ב-Acme',
  });
  assert.match(formatted, /התאמה טובה/);
  assert.match(formatted, /מפתח Python/);
  assert.match(formatted, /דרוש מפתח Python ב-Acme/);
});

test('formatForwardMessage includes group name when provided', () => {
  const formatted = formatForwardMessage({
    reason: 'התאמה טובה',
    summary: 'מפתח Python',
    originalText: 'דרוש מפתח Python ב-Acme',
    groupName: 'AI Agents - Jobs',
  });
  assert.match(formatted, /\*קבוצה:\* AI Agents - Jobs/);
  assert.match(formatted, /התאמה טובה/);
  assert.match(formatted, /מפתח Python/);
});

const axios = require('axios');
const {
  handleIncomingMessage,
  parseOdds,
  getConfig,
} = require('./whatsapp-agent');

const firstSourceGroupId = getConfig().sourceGroupIds[0];
const TARGET_GROUP_ID = getConfig().targetGroupId;

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
    data: { reason: 'התאמה טובה', summary: 'מפתח Python', odds: '75%' },
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

test('handleIncomingMessage swallows errors from a failing backend call', async (t) => {
  t.mock.method(axios, 'post', async () => {
    throw new Error('network down');
  });
  const sentMessages = [];
  const fakeClient = { sendMessage: async (to, text) => sentMessages.push({ to, text }) };

  await assert.doesNotReject(
    handleIncomingMessage(fakeClient, { from: firstSourceGroupId, body: 'משרה: מפתח' })
  );
  assert.equal(sentMessages.length, 0);
});

test('parseOdds converts string percentage to number', () => {
  assert.equal(parseOdds('75%'), 75);
  assert.equal(parseOdds('80'), 80);
});

test('parseOdds returns number as-is', () => {
  assert.equal(parseOdds(75), 75);
  assert.equal(parseOdds(100), 100);
});

test('parseOdds returns 0 for invalid input', () => {
  assert.equal(parseOdds('invalid'), 0);
  assert.equal(parseOdds(null), 0);
  assert.equal(parseOdds(undefined), 0);
});

test('handleIncomingMessage does not forward when odds are below 70%', async (t) => {
  t.mock.method(axios, 'post', async () => ({
    data: { reason: 'התאמה חלשה', summary: 'מפתח Python', odds: '65%' },
  }));
  const sentMessages = [];
  const fakeClient = { sendMessage: async (to, text) => sentMessages.push({ to, text }) };

  await handleIncomingMessage(fakeClient, {
    from: firstSourceGroupId,
    body: 'דרוש מפתח Python',
  });

  assert.equal(sentMessages.length, 0);
});

test('handleIncomingMessage forwards when odds are 70% or higher', async (t) => {
  t.mock.method(axios, 'post', async () => ({
    data: { reason: 'התאמה טובה', summary: 'מפתח Python', odds: '75%' },
  }));
  const sentMessages = [];
  const fakeClient = { sendMessage: async (to, text) => sentMessages.push({ to, text }) };

  await handleIncomingMessage(fakeClient, {
    from: firstSourceGroupId,
    body: 'דרוש מפתח Python',
  });

  assert.equal(sentMessages.length, 1);
  assert.equal(sentMessages[0].to, TARGET_GROUP_ID);
});

test('forwarded message uses the group name from the WhatsApp chat', async (t) => {
  t.mock.method(axios, 'post', async () => ({
    data: { reason: 'x', summary: 'y', odds: '90%' },
  }));
  const sentMessages = [];
  const fakeClient = { sendMessage: async (to, text) => sentMessages.push({ to, text }) };

  await handleIncomingMessage(fakeClient, {
    from: firstSourceGroupId,
    body: 'דרוש מפתח Python',
    getChat: async () => ({ name: 'Runtime Group' }),
  });

  assert.match(sentMessages[0].text, /\*קבוצה:\* Runtime Group/);
});
