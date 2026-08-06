// Re-link the WhatsApp session used by campaign sends (bridge/send_campaign.js).
//
// Only needed if the session expires or gets unlinked from your phone. Normal
// runs never ask for this: the session persists in .wwebjs_auth/session.
//
// Run:  node bridge/link_whatsapp.js
// Then scan with WhatsApp > Settings > Linked Devices > Link a Device.
//
// Nothing else may hold the profile while this runs. In particular the
// interactive chat bridge (index.js) must stay off, which is its default.
import pkg from 'whatsapp-web.js';
const { Client, LocalAuth } = pkg;
import qrcode from 'qrcode-terminal';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const __dirname = dirname(fileURLToPath(import.meta.url));

const client = new Client({
  authStrategy: new LocalAuth({ dataPath: join(__dirname, '.wwebjs_auth') }),
  puppeteer: { headless: true, args: ['--no-sandbox', '--disable-setuid-sandbox'] },
});

const timer = setTimeout(() => {
  console.error('TIMEOUT: no scan within 180s. Re-run to try again.');
  client.destroy().finally(() => process.exit(2));
}, 180000);

client.on('qr', (qr) => {
  console.log('\nScan with WhatsApp > Settings > Linked Devices > Link a Device:\n');
  qrcode.generate(qr, { small: true });
});

client.on('authenticated', () => console.log('Scanned. Finishing link...'));

client.on('ready', async () => {
  clearTimeout(timer);
  const me = client.info?.wid?.user || 'unknown';
  console.log(`WHATSAPP LINKED as +${me}. Campaign sends are ready.`);
  await client.destroy();
  process.exit(0);
});

client.initialize();
