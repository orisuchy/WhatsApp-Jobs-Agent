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
  // sourceIds can be either an array (for backward compatibility with tests) or an object
  if (Array.isArray(sourceIds)) {
    return sourceIds.includes(messageFrom);
  }
  return messageFrom in sourceIds;
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

function formatForwardMessage({ odds, reason, company, summary, originalText, groupName }) {
  const groupLine = groupName ? `*קבוצה:* ${groupName}` : '';
  const lines = [
    `משרה חדשה! ${odds} התאמה:`,
    '',
  ];
  if (groupLine) lines.push(groupLine);
  lines.push(
    `*סיכום:* ${summary}`,
    `*חברה:* ${company}`,
    `*סיבת התאמה:* ${reason}`,
    '',
    '--- המודעה המקורית ---',
    originalText
  );
  return lines.join('\n');
}

const { Client, LocalAuth } = require('whatsapp-web.js');
const qrcode = require('qrcode-terminal');
const axios = require('axios');

const SOURCE_GROUP_IDS = {
  // Configured via .env (see .env.example) — ticket 01.
};

const TARGET_GROUP_ID = process.env.TARGET_GROUP_ID || '';

const ANALYZE_URL = process.env.ANALYZE_URL || 'http://python-backend:8000/analyze';

function parseOdds(oddsValue) {
  if (typeof oddsValue === 'number') {
    return oddsValue;
  }
  if (typeof oddsValue === 'string') {
    const parsed = parseInt(oddsValue, 10);
    return isNaN(parsed) ? 0 : parsed;
  }
  return 0;
}

async function handleIncomingMessage(client, message) {
  if (!isFromSourceGroup(message.from, SOURCE_GROUP_IDS)) {
    log_info(`Ignoring message from ${message.from} (not in source group)`);
    return;
  }
  if (!containsJobKeywords(message.body)) {
    log_info(`Ignoring message with no job keywords: ${message.body}`);
    return;
  }

  try {
    const { data } = await axios.post(ANALYZE_URL, { text: message.body });
    if (!data) {
      log_info(`Message not sent - Job doesn't match`);
      return;
    }

    // Handle batch response (when queue is full)
    if (data.batch_results) {
      log_info(`Queue was full. Processing batch of ${data.count} items.`);
      for (const result of data.batch_results) {
        await processSingleResult(client, result, message);
      }
      return;
    }

    // Handle queued response (when queue is not full yet)
    if (data.queued) {
      log_info(`Message added to queue (${data.queue_size}/${data.batch_size}). Awaiting batch processing.`);
      return;
    }

    // Handle immediate single result (fallback for old format or flush endpoint)
    await processSingleResult(client, data, message);

  } catch (err) {
    log_error('Failed to analyze/forward message: ' + err.message);
  }
}

async function processSingleResult(client, result, message) {
  const odds = parseOdds(result.odds);
  const oddsThreshold = 70;
  if (odds <= oddsThreshold) {
    log_info(`Message not sent - odds below ${oddsThreshold}% threshold. Odds: ${odds}%, Reason: ${result.reason || 'N/A'}, Text: ${message.body.substring(0, 100)}`);
    return;
  }
  const groupName = typeof SOURCE_GROUP_IDS === 'object' && !Array.isArray(SOURCE_GROUP_IDS)
    ? SOURCE_GROUP_IDS[message.from]
    : undefined;
  const forwardText = formatForwardMessage({
    odds: result.odds,
    reason: result.reason,
    company: result.company,
    summary: result.summary,
    originalText: message.body,
    groupName,
  });
  await client.sendMessage(TARGET_GROUP_ID, forwardText);
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

function start() {
  const client = createClient();

  client.on('qr', (qr) => {
    log_info('QR code received, scan to authenticate');
    qrcode.generate(qr, { small: true });
  });

  client.on('ready', () => {
    log_info('WhatsApp client ready.');
    client.sendMessage(TARGET_GROUP_ID, `************ ${getTimestamp()} Client on - Collecting data ************`);
  });

  client.on('auth_failure', (msg) => {
    log_error('Authentication failure: ' + msg);
  });

  client.on('disconnected', () => {
    log_info('WhatsApp client disconnected.');
    client.sendMessage(TARGET_GROUP_ID, "************ Client Off ************");
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
  formatForwardMessage,
  handleIncomingMessage,
  processSingleResult,
  parseOdds,
  SOURCE_GROUP_IDS,
  TARGET_GROUP_ID,
};

if (require.main === module) {
  start();
}
