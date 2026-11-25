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

const ANALYZE_URL = process.env.ANALYZE_URL || 'http://python-backend:8000/analyze';
const parsedTimeout = parseInt(process.env.ANALYZE_TIMEOUT_MS, 10);
const ANALYZE_TIMEOUT_MS = parsedTimeout > 0 ? parsedTimeout : 180000;

const STATE_URL = process.env.STATE_URL || ANALYZE_URL.replace(/\/analyze$/, '/state');
const CONFIG_URL = process.env.CONFIG_URL || ANALYZE_URL.replace(/\/analyze$/, '/api/config');
const CONFIG_TTL_MS = 30000;
const CONFIG_FALLBACK_TTL_MS = 5000;

function envConfig() {
  return {
    sourceGroupIds: (process.env.SOURCE_GROUP_IDS || '').split(',').map((id) => id.trim()).filter(Boolean),
    targetGroupId: process.env.TARGET_GROUP_ID || '',
    outputs: (process.env.OUTPUTS || 'whatsapp').split(',').map((o) => o.trim().toLowerCase()).filter(Boolean),
  };
}

// Single access point for configuration: Python's effective config (env + UI settings), cached
// briefly; falls back to env when Python is unreachable.
let configCache = { at: 0, value: null, ttl: CONFIG_TTL_MS };
async function getConfig() {
  if (configCache.value && Date.now() - configCache.at < configCache.ttl) return configCache.value;
  try {
    const { data } = await axios.get(CONFIG_URL, { timeout: 5000 });
    configCache = { at: Date.now(), value: data, ttl: CONFIG_TTL_MS };
  } catch (err) {
    log_error('Could not fetch config from Python, using env: ' + err.message);
    // keep the last good config (else env) briefly so a down Python doesn't add its timeout to every message
    configCache = { at: Date.now(), value: configCache.value || envConfig(), ttl: CONFIG_FALLBACK_TTL_MS };
  }
  return configCache.value;
}

const uiState = { status: 'initializing', qr: null, groups: [] };

// Push our state to Python for the web UI; never let a UI failure affect the agent.
let pushQueue = Promise.resolve();
function pushState(patch) {
  Object.assign(uiState, patch);
  const snapshot = { ...uiState };
  // Serialised so a slow earlier push can't land after (and overwrite) a newer one.
  pushQueue = pushQueue.then(async () => {
    try {
      await axios.post(STATE_URL, snapshot, { timeout: 5000 });
    } catch (err) {
      log_error('Could not push state to Python: ' + err.message);
    }
  });
  return pushQueue;
}

async function pushGroups(client) {
  const chats = await client.getChats();
  const groups = chats.filter((c) => c.isGroup).map((c) => ({ name: c.name, jid: c.id._serialized }));
  await pushState({ groups });
}

// Mirrors client lifecycle events into the UI state.
function registerStateEvents(client) {
  client.on('qr', (qr) => pushState({ status: 'qr', qr }));
  client.on('ready', () => {
    pushState({ status: 'ready', qr: null });
    pushGroups(client).catch((err) => log_error('Failed to push groups: ' + err.message));
  });
  client.on('auth_failure', () => pushState({ status: 'auth_failure', qr: null }));
  client.on('disconnected', () => pushState({ status: 'disconnected', qr: null }));
}

let analyzeQueue = Promise.resolve();

async function handleIncomingMessage(client, message) {
  if (!isFromSourceGroup(message.from, (await getConfig()).sourceGroupIds)) {
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
    // One posting at a time so the timeout only measures its own analysis (Python serialises on the LLM anyway).
    const run = analyzeQueue.then(async () => {
      const { data } = await axios.post(ANALYZE_URL, { text: message.body, group }, { timeout: ANALYZE_TIMEOUT_MS });
      await processSingleResult(client, data, message);
    });
    analyzeQueue = run.catch(() => {});
    await run;

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
  const { outputs, targetGroupId } = await getConfig();
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
  registerStateEvents(client);
  pushState({ status: 'initializing', qr: null }); // clears stale UI state after a Node restart

  client.on('qr', (qr) => {
    log_info('QR code received, scan to authenticate');
    qrcode.generate(qr, { small: true });
  });

  client.on('ready', async () => {
    log_info('WhatsApp client ready.');
    if (process.env.LIST_GROUPS === '1') {
      listGroups(client)
        .catch((err) => log_error("Failed to list groups: " + err.message))
        .finally(() => process.exit(0));
      return;
    }
    client.sendMessage((await getConfig()).targetGroupId, `************ ${getTimestamp()} Client on - Collecting data ************`);
  });

  client.on('auth_failure', (msg) => {
    log_error('Authentication failure: ' + msg);
  });

  client.on('disconnected', async () => {
    log_info('WhatsApp client disconnected.');
    client.sendMessage((await getConfig()).targetGroupId, "************ Client Off ************");
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
  resetConfigCache: () => { configCache = { at: 0, value: null, ttl: CONFIG_TTL_MS }; },
  pushState,
  registerStateEvents,
};

if (require.main === module) {
  start();
}
