// Quick bridge health check: verifies the shared LocalAuth session still
// authenticates without a QR scan. Exits 0 on ready, 1 if a QR is required
// (session dead), 2 on timeout/error. Sends nothing.
import pkg from 'whatsapp-web.js';
const { Client, LocalAuth } = pkg;
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const __dirname = dirname(fileURLToPath(import.meta.url));

const client = new Client({
  authStrategy: new LocalAuth({ dataPath: join(__dirname, '.wwebjs_auth') }),
  puppeteer: { headless: true, args: ['--no-sandbox', '--disable-setuid-sandbox'] },
});

const timer = setTimeout(() => {
  console.error('TIMEOUT: bridge did not become ready in 120s.');
  process.exit(2);
}, 120000);

client.on('qr', () => {
  clearTimeout(timer);
  console.error('NOT AUTHENTICATED: session expired, a fresh QR scan is needed (run node index.js).');
  client.destroy().finally(() => process.exit(1));
});

client.on('ready', async () => {
  clearTimeout(timer);
  const me = client.info?.wid?.user || 'unknown';
  console.log(`BRIDGE READY: authenticated as +${me}, no QR needed.`);
  await client.destroy();
  process.exit(0);
});

client.initialize();
