'use strict';

const { log_info, log_error, getTimestamp } = require('./logger');

const JOB_KEYWORDS = [
  // --- Regular expressions (Regex) for flexible patterns ---
  /דרוש[./\\]ה/i,         // Matches: דרוש/ה, דרוש.ה, דרוש\ה
  /מפתח[./\\]ת/i,        // Matches: מפתח/ת, מפתח.ת
  /hiring/i,            // Matches: hiring, we're hiring, we are hiring
  /looking for/i,       // Matches: looking for, looking for a...
  /open (position|role|vacancy)/i, // Matches: open position, open role, open vacancy

  // --- Hebrew words (searching & hiring) ---
  'דרוש',
  'דרושים',
  'משרה',
  'משרת',
  'גיוס',
  'מגייס',
  'מגייסים',
  'מגייסת',
  'מחפש',
  'מחפשת',
  'מחפשים',
  'מחפשות',


  // --- English words (lowercase - the function will normalize them) ---
  'cv',
  'software engineer',
  'developer',
  'seeking'
];


function isFromSourceGroup(messageFrom, sourceIds) {
  return sourceIds.includes(messageFrom);
}

function containsJobKeywords(text, keywords = JOB_KEYWORDS) {
  if (!text) return false;

  // Normalize text to lowercase (for English words that are not Regex)
  const lowerText = text.toLowerCase();

  return keywords.some((keyword) => {
    if (keyword instanceof RegExp) {
      // If it's a Regex pattern, test against the original text directly
      return keyword.test(text);
    }
    // If it's a regular string, perform a simple case-insensitive include check
    return lowerText.includes(keyword.toLowerCase());
  });
}

const { Client, LocalAuth } = require('whatsapp-web.js');
const qrcode = require('qrcode-terminal');
const axios = require('axios');

// Single access point for configuration (read on every call so it can be swapped later).
function getConfig() {
  return {
    sourceGroupIds: (process.env.SOURCE_GROUP_IDS || '')
      .split(',')
      .map((id) => id.trim())
      .filter(Boolean),
    targetGroupId: process.env.TARGET_GROUP_ID || '',
    outputs: (process.env.OUTPUTS || 'whatsapp')
      .split(',')
      .map((o) => o.trim().toLowerCase())
      .filter(Boolean),
  };
}

const ANALYZE_URL = process.env.ANALYZE_URL || 'http://python-backend:8000/analyze';
const parsedTimeout = parseInt(process.env.ANALYZE_TIMEOUT_MS, 10);
const ANALYZE_TIMEOUT_MS = parsedTimeout > 0 ? parsedTimeout : 180000;

async function handleIncomingMessage(client, message) {
  if (!isFromSourceGroup(message.from, getConfig().sourceGroupIds)) {
    log_info(`Ignoring message from ${message.from} (not in source group)`);
    return;
  }
  if (!containsJobKeywords(message.body)) {
    log_info(`Ignoring message with no job keywords: ${message.body}`);
    return;
  }

  try {
    let group = '';
    try {
      group = (await message.getChat?.())?.name || '';
    } catch (err) {
      log_error('Could not read group name: ' + err.message);
    }
    const { data } = await axios.post(ANALYZE_URL, { text: message.body, group }, { timeout: ANALYZE_TIMEOUT_MS });
    await processSingleResult(client, data, message);

  } catch (err) {
    if (err.code === 'ECONNABORTED' || err.code === 'ETIMEDOUT') {
      log_info(`Skipped posting - analysis timed out after ${ANALYZE_TIMEOUT_MS}ms: ${message.body.substring(0, 100)}`);
      return;
    }
    log_error('Failed to analyze/forward message: ' + err.message);
  }
}

// Python decides match/dedup and formats the text; Node only delivers to WhatsApp.
async function processSingleResult(client, result, message) {
  if (!result || !result.forward) {
    log_info(`Message not sent - no match or duplicate. Text: ${message.body.substring(0, 100)}`);
    return;
  }
  const { outputs, targetGroupId } = getConfig();
  if (!outputs.includes('whatsapp')) return;
  await client.sendMessage(targetGroupId, result.whatsapp_text);
}

function createClient() {
  return new Client({
    authStrategy: new LocalAuth(),
    puppeteer: {
      executablePath: '/usr/bin/chromium',
      args: ['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage'],
    },
  });
}

async function listGroups(client) {
  const chats = await client.getChats();
  for (const chat of chats.filter((c) => c.isGroup)) {
    log_info(`GROUP: ${chat.name} -> ${chat.id._serialized}`);
  }
}

function start() {
  const client = createClient();

  client.on('qr', (qr) => {
    log_info('QR code received, scan to authenticate');
    qrcode.generate(qr, { small: true });
  });

  client.on('ready', () => {
    log_info('WhatsApp client ready.');
    if (process.env.LIST_GROUPS === '1') {
      listGroups(client)
        .catch((err) => log_error("Failed to list groups: " + err.message))
        .finally(() => process.exit(0));
      return;
    }
    client.sendMessage(getConfig().targetGroupId, `************ ${getTimestamp()} Client on - Collecting data ************`);
  });

  client.on('auth_failure', (msg) => {
    log_error('Authentication failure: ' + msg);
  });

  client.on('disconnected', () => {
    log_info('WhatsApp client disconnected.');
    client.sendMessage(getConfig().targetGroupId, "************ Client Off ************");
  });

  client.on('message', (message) => {
    handleIncomingMessage(client, message).catch((err) => {
      log_error('Unhandled error in message handler: ' + err);
    });
  });

  log_info('Initializing WhatsApp client...');
  client.initialize().catch((err) => {
    log_error('Failed to initialize client: ' + err.message);
  });
}

module.exports = {
  isFromSourceGroup,
  containsJobKeywords,
  handleIncomingMessage,
  processSingleResult,
  getConfig,
};

if (require.main === module) {
  start();
}
