import pkg from 'whatsapp-web.js';
const { Client, LocalAuth } = pkg;
import qrcode from 'qrcode-terminal';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { readFileSync, writeFileSync, existsSync } from 'node:fs';
import 'dotenv/config';

const __dirname = dirname(fileURLToPath(import.meta.url));
const PROJECT_DIR = join(__dirname, '..');
const SESSION_FILE = join(__dirname, '.sessions.json');

const ALLOWED = (process.env.WHATSAPP_ALLOWED_NUMBERS || '')
  .split(',')
  .map((s) => s.trim().replace(/\D/g, ''))
  .filter(Boolean);

const CLAUDE_BIN = process.env.CLAUDE_BIN || 'claude';
const EXTRA_ARGS = (process.env.CLAUDE_EXTRA_ARGS || '')
  .split(' ')
  .filter(Boolean);
const CHUNK_SIZE = Number(process.env.WHATSAPP_CHUNK_SIZE || 3500);
const TIMEOUT_MS = Number(process.env.CLAUDE_TIMEOUT_MS || 15 * 60 * 1000);

const sessions = loadSessions();
const queues = new Map();

function loadSessions() {
  if (!existsSync(SESSION_FILE)) return {};
  try {
    return JSON.parse(readFileSync(SESSION_FILE, 'utf8'));
  } catch {
    return {};
  }
}

function saveSessions() {
  writeFileSync(SESSION_FILE, JSON.stringify(sessions, null, 2));
}

const client = new Client({
  authStrategy: new LocalAuth({ dataPath: join(__dirname, '.wwebjs_auth') }),
  puppeteer: {
    headless: true,
    args: ['--no-sandbox', '--disable-setuid-sandbox'],
  },
});

client.on('qr', (qr) => {
  console.log('\nScan with WhatsApp → Settings → Linked Devices → Link a Device:\n');
  qrcode.generate(qr, { small: true });
});
client.on('authenticated', () => console.log('WhatsApp authenticated.'));
client.on('ready', () => {
  console.log(`Bridge ready. Project dir: ${PROJECT_DIR}`);
  if (!ALLOWED.length) {
    console.warn('WARNING: WHATSAPP_ALLOWED_NUMBERS is empty — bridge will respond to ANY DM.');
  } else {
    console.log(`Allowed numbers: ${ALLOWED.join(', ')}`);
  }
});
client.on('disconnected', (reason) => console.log(`Disconnected: ${reason}`));

client.on('message', (msg) => {
  if (msg.fromMe) return;
  if (msg.from.endsWith('@g.us')) return;
  const number = msg.from.replace(/\D/g, '');
  if (ALLOWED.length && !ALLOWED.includes(number)) {
    console.log(`Ignoring ${number} (not in WHATSAPP_ALLOWED_NUMBERS)`);
    return;
  }
  enqueue(msg.from, () => handle(msg));
});

function enqueue(key, fn) {
  const prev = queues.get(key) || Promise.resolve();
  const next = prev.then(fn, fn).catch((err) => console.error(`[${key}]`, err));
  queues.set(key, next);
  next.finally(() => {
    if (queues.get(key) === next) queues.delete(key);
  });
}

async function handle(msg) {
  const text = (msg.body || '').trim();
  if (!text) return;

  if (text === '/reset' || text === '/new') {
    delete sessions[msg.from];
    saveSessions();
    await msg.reply('Conversation reset. Next message starts a fresh Claude session.');
    return;
  }

  console.log(`> [${msg.from}] ${text.slice(0, 200)}${text.length > 200 ? '…' : ''}`);
  try {
    const reply = await runClaude(msg.from, text);
    await sendChunked(msg, reply);
    console.log(`< [${msg.from}] (${reply.length} chars)`);
  } catch (err) {
    console.error(err);
    await msg.reply(`Bridge error: ${err.message}`);
  }
}

function runClaude(jid, prompt) {
  return new Promise((resolve, reject) => {
    const args = ['-p', prompt, '--output-format', 'json', ...EXTRA_ARGS];
    if (sessions[jid]) args.push('--resume', sessions[jid]);

    const child = spawn(CLAUDE_BIN, args, {
      cwd: PROJECT_DIR,
      env: process.env,
    });

    let out = '';
    let err = '';
    const timer = setTimeout(() => {
      child.kill('SIGTERM');
      reject(new Error(`claude timed out after ${TIMEOUT_MS}ms`));
    }, TIMEOUT_MS);

    child.stdout.on('data', (d) => (out += d));
    child.stderr.on('data', (d) => (err += d));
    child.on('error', (e) => {
      clearTimeout(timer);
      reject(e);
    });
    child.on('close', (code) => {
      clearTimeout(timer);
      if (code !== 0) {
        return reject(
          new Error(`claude exited ${code}: ${(err || out).slice(0, 400)}`)
        );
      }
      try {
        const json = JSON.parse(out);
        if (json.session_id) {
          sessions[jid] = json.session_id;
          saveSessions();
        }
        if (json.is_error) {
          return reject(new Error(json.result || 'claude reported is_error'));
        }
        resolve(json.result ?? out);
      } catch {
        resolve(out.trim() || '(no output)');
      }
    });
  });
}

async function sendChunked(msg, text) {
  const body = text || '(empty response)';
  for (let i = 0; i < body.length; i += CHUNK_SIZE) {
    await msg.reply(body.slice(i, i + CHUNK_SIZE));
  }
}

process.on('SIGINT', async () => {
  console.log('\nShutting down…');
  try {
    await client.destroy();
  } finally {
    process.exit(0);
  }
});

client.initialize();
