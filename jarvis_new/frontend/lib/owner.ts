/**
 * The owner's secret, and the configuration built from it.
 *
 * One file in the JARVIS folder, beside the control token, created on first
 * use. Knowing it is what makes you the owner of this dashboard.
 *
 * It is deliberately *not* the control token. The control token authenticates
 * this server to the Python side; this secret authenticates a person to this
 * server. Reusing one for both would mean anything that could read the token
 * file - which is what the proxy does on every request - could also mint
 * owner sessions, which is the hole being closed rather than a saving.
 */
import { createHash, randomBytes } from 'node:crypto';
import { constants, promises as fs } from 'node:fs';
import path from 'node:path';
import { type AuthConfig, configure } from './auth';
import { jarvisRoot } from './control';

export const SECRET_FILE = 'dashboard.secret';

export function secretPath(): string {
  return path.join(jarvisRoot(), SECRET_FILE);
}

/** Where the dashboard believes it is being served from. */
export function dashboardOrigin(): string {
  return process.env.JARVIS_DASHBOARD_ORIGIN ?? 'http://127.0.0.1:3000';
}

/**
 * Read the owner secret, creating it if this is the first run.
 *
 * `wx` rather than "does it exist? no? write one": two processes starting
 * together both find no file, both write, and each goes on believing its own
 * secret is the one - the same race the control token had. Whoever loses
 * reads the winner's.
 */
export async function ownerSecret(): Promise<string> {
  const file = secretPath();

  for (let attempt = 0; attempt < 5; attempt += 1) {
    try {
      const existing = (await fs.readFile(file, 'utf8')).trim();
      if (existing) return existing;
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error;
    }

    const fresh = randomBytes(32).toString('base64url');
    try {
      await fs.mkdir(path.dirname(file), { recursive: true });
      // wx: created by exactly one caller, with the mode set as it is made
      // rather than a moment afterwards.
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
      if ((error as NodeJS.ErrnoException).code !== 'EEXIST') throw error;
      // Somebody else won. Give them a moment to finish writing, then read
      // theirs. Deleting an empty file here would race the process writing
      // it, which is how two processes end up with different secrets.
      await new Promise((resolve) => setTimeout(resolve, 20 * (attempt + 1)));
    }
  }

  throw new Error(
    `Could not establish the dashboard secret at ${file}. Check its permissions, ` +
      'or delete it and start JARVIS again.'
  );
}

let cached: AuthConfig | null = null;

/**
 * The auth configuration, read once per server process.
 *
 * Cached because it is wanted on every request and it cannot change without a
 * restart: the secret file is written once, and the origin comes from the
 * environment. `resetConfigForTests` exists so tests can point it somewhere
 * else without a process of their own.
 */
export async function authConfig(): Promise<AuthConfig> {
  if (cached) return cached;
  cached = configure(await ownerSecret(), dashboardOrigin());
  return cached;
}

export function resetConfigForTests(): void {
  cached = null;
}

/**
 * A short fingerprint of the secret, safe to print.
 *
 * The launcher shows this so somebody can tell "the dashboard and I disagree
 * about the secret" from "the dashboard is broken", without the secret itself
 * ending up in a terminal buffer, a screenshot or a log.
 */
export function secretFingerprint(secret: string): string {
  return createHash('sha256').update(secret).digest('hex').slice(0, 8);
}
