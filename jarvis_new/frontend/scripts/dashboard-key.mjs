#!/usr/bin/env node
/**
 * Prints the dashboard key, or the URL that carries it.
 *
 * The launchers use this. `butler-settings.bat` opens the console with the
 * key already in the address, the page signs in and strips it out, and the
 * owner never sees a login screen - which is the whole point, because a
 * single-user tool that asks for a password every morning gets its password
 * written on a sticky note.
 *
 * It is a script rather than a call into the app because a `.bat` file cannot
 * import TypeScript, and because the key has to exist *before* the browser
 * opens. `lib/owner.ts` creates the same file the same way if the server gets
 * there first; `tests/dashboard-key.test.ts` holds the two to that.
 *
 *   node scripts/dashboard-key.mjs          -> http://localhost:3000/dashboard?key=…
 *   node scripts/dashboard-key.mjs --key    -> the key alone
 *   node scripts/dashboard-key.mjs --path /  -> a different page
 */
import { randomBytes } from 'node:crypto';
import { constants, promises as fs } from 'node:fs';
import os from 'node:os';
import path from 'node:path';

export const SECRET_FILE = 'dashboard.secret';

/** Mirrors `lib/control.ts` and `jarvis/paths.py`. All three have to agree. */
export function jarvisRoot() {
  const override = process.env.JARVIS_HOME;
  if (override) return override;
  if (process.platform === 'win32') {
    const base = process.env.APPDATA ?? path.join(os.homedir(), 'AppData', 'Roaming');
    return path.join(base, 'JARVIS');
  }
  if (process.platform === 'darwin') {
    return path.join(os.homedir(), 'Library', 'Application Support', 'JARVIS');
  }
  const base = process.env.XDG_DATA_HOME ?? path.join(os.homedir(), '.local', 'share');
  return path.join(base, 'jarvis');
}

/**
 * Read the key, creating it if this is the first run.
 *
 * `O_EXCL` for the same reason `lib/owner.ts` uses it: the launcher and the
 * server can both arrive first, and "check then write" lets each end up with
 * a different key. An empty file is waited for, never deleted - deleting it
 * races whoever is legitimately writing it.
 */
export async function dashboardKey() {
  const file = path.join(jarvisRoot(), SECRET_FILE);

  for (let attempt = 0; attempt < 5; attempt += 1) {
    try {
      const existing = (await fs.readFile(file, 'utf8')).trim();
      if (existing) return existing;
    } catch (error) {
      if (error.code !== 'ENOENT') throw error;
    }

    const fresh = randomBytes(32).toString('base64url');
    try {
      await fs.mkdir(path.dirname(file), { recursive: true });
      const handle = await fs.open(
        file,
        constants.O_CREAT | constants.O_EXCL | constants.O_WRONLY,
        0o600
      );
      try {
        await handle.writeFile(fresh, 'utf8');
        await handle.sync();
      } finally {
        await handle.close();
      }
      return fresh;
    } catch (error) {
      if (error.code !== 'EEXIST') throw error;
      await new Promise((resolve) => setTimeout(resolve, 20 * (attempt + 1)));
    }
  }

  throw new Error(`Could not establish the dashboard key at ${file}.`);
}

async function main(argv) {
  const key = await dashboardKey();
  if (argv.includes('--key')) {
    process.stdout.write(key);
    return;
  }
  const at = argv.indexOf('--path');
  const page = at === -1 ? '/dashboard' : (argv[at + 1] ?? '/dashboard');
  const base = process.env.JARVIS_DASHBOARD_URL ?? 'http://localhost:3000';
  process.stdout.write(`${base}${page}?key=${encodeURIComponent(key)}`);
}

// Only when run directly, so the test can import the functions above.
if (process.argv[1] && import.meta.url === new URL(`file://${process.argv[1]}`).href) {
  main(process.argv.slice(2)).catch((error) => {
    process.stderr.write(`${error.message}\n`);
    process.exit(1);
  });
}
