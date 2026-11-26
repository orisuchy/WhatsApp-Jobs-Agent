'use strict';

const { log_info, log_error, getTimestamp } = require('./logger');

// Cheap pre-filter: a message must look like a job posting in any profession before the LLM sees it.
// Keep it general (no job titles or fields) and lean towards letting a posting through: a false positive
// costs one LLM call, a false negative loses a job.
const JOB_KEYWORDS = [
  // --- Hebrew (substrings: 'דרוש' also covers דרושה/דרושים/דרושות, 'מגייס' covers מגייסים/מגייסת, ...) ---
  'דרוש',
  'משרה', 'משרת', 'משרות',
  'גיוס', 'מגייס',
  'מחפש',
  'תפקיד',
  'קורות חיים', 'קו"ח', 'קו״ח',
  'הגשת מועמדות', 'מועמד',
  'שנות ניסיון',
  'היקף משרה', 'חצי משרה',
  'הצטרף', // הצטרפו לצוות / להצטרף
  /[א-ת][./\\][הת](?![א-ת])/,  // gendered job titles: מפתח/ת, מנהל.ת, דרוש\ה (\b doesn't work on Hebrew in JS)

  // --- English (word boundaries, so "role" doesn't match "control") ---
  /\b(we('re| are)\s+)?hiring\b/i,
  /\brecruit(ing|er|ers|ment)?\b/i,
  /\b(job|jobs|position|positions|role|roles|vacanc(y|ies)|opening|openings|career|careers|internship)\b/i,
  /\blooking for\b/i,
  /\bseeking\b/i,
  /\bwanted\b/i,
  /\bjoin (our|the|my) team\b/i,
  /\b(cv|resume|résumé)\b/i,
  /\bapply\b|\bapplications?\b/i,
  /\b(full|part)[- ]time\b/i,
  /\b(junior|senior|entry[- ]level|mid[- ]level|intern)\b/i,
  /\byears? of experience\b|\b\d+\+?\s*years?\b/i,
  /\b(salary|requirements|qualifications|responsibilities)\b/i,
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

const uiState = { status: 'initializing', qr: null, groups: [], me: null };
let exitProcess = (code) => process.exit(code);  // swapped in tests

// Push our state to Python for the web UI; never let a UI failure affect the agent.
let pushQueue = Promise.resolve();
let stateClient = null; // set by registerStateEvents; sends the reminders Python hands back
function pushState(patch) {
  Object.assign(uiState, patch);
  const snapshot = { ...uiState };
  // Serialised so a slow earlier push can't land after (and overwrite) a newer one.
  pushQueue = pushQueue.then(async () => {
    try {
      const res = await axios.post(STATE_URL, snapshot, { timeout: 5000 });
      for (const m of (res && res.data && res.data.messages) || []) {
        try {
          await stateClient.sendMessage(m.to, m.text);
        } catch (err) {
          log_error('Could not send reminder: ' + err.message);
        }
      }
      if (res && res.data && res.data.refresh_groups && stateClient) {
        // "Reload groups" in the UI. Not awaited: pushGroups queues its own push behind this one.
        pushGroups(stateClient).catch((err) => log_error('Failed to push groups: ' + err.message));
      }
      if (res && res.data && res.data.restart) {
        // "Reconnect WhatsApp" in the UI: exit so Docker (restart: unless-stopped) starts a fresh client
        log_info('Restart requested from the web UI; exiting.');
        exitProcess(0);
      }
    } catch (err) {
      log_error('Could not push state to Python: ' + err.message);
    }
  });
  return pushQueue;
}

async function pushGroups(client) {
  const chats = await client.getChats();
  const groups = chats.filter((c) => c.isGroup).map((c) => ({ name: c.name, jid: c.id._serialized }));
  log_info(`Group list loaded: ${groups.length} groups`);
  await pushState({ groups });
}

// Mirrors client lifecycle events into the UI state.
function registerStateEvents(client) {
  stateClient = client;
  client.on('qr', (qr) => pushState({ status: 'qr', qr }));
  client.on('ready', () => {
    pushState({ status: 'ready', qr: null, me: client.info?.wid?._serialized || null });
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

// WWEBJS_WEB_VERSION pins WhatsApp Web to a known-good build (archived by wppconnect-team/wa-version),
// for when a new WhatsApp Web release breaks whatsapp-web.js. Unset = library default.
function clientOptions(webVersion = process.env.WWEBJS_WEB_VERSION) {
  const opts = {
    authStrategy: new LocalAuth(),
    puppeteer: {
      executablePath: '/usr/bin/chromium',
      args: ['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage'],
    },
  };
  if (webVersion) {
    opts.webVersion = webVersion;
    opts.webVersionCache = {
      type: 'remote',
      remotePath: `https://raw.githubusercontent.com/wppconnect-team/wa-version/main/html/${webVersion}.html`,
    };
  }
  return opts;
}

function createClient() {
  return new Client(clientOptions());
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
  setInterval(() => pushState({}), 10000); // re-sync the UI after a Python restart; also picks up restart requests

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

  client.on('disconnected', async (reason) => {
    // whatsapp-web.js doesn't reconnect: exit and let Docker (restart: unless-stopped) start a fresh client
    log_error(`WhatsApp client disconnected (${reason}); restarting.`);
    await pushState({});
    process.exit(1);
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
  clientOptions,
  setExit: (fn) => { exitProcess = fn; },
};

if (require.main === module) {
  start();
}
