import fs from 'node:fs';
import path from 'node:path';

export function alert(alertsFile, level, message, extra = {}) {
  fs.mkdirSync(path.dirname(alertsFile), { recursive: true });
  const line = JSON.stringify({ at: new Date().toISOString(), level, message, ...extra });
  fs.appendFileSync(alertsFile, `${line}\n`);
  process.stderr.write(`[${level}] ${message}\n`);
}

export function readAlerts(alertsFile) {
  if (!fs.existsSync(alertsFile)) return [];
  return fs.readFileSync(alertsFile, 'utf8').split('\n').filter(Boolean).map((l) => {
    try { return JSON.parse(l); } catch { return { raw: l }; }
  });
}
