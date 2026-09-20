/**
 * The owner secret, and the configuration built from it.
 *
 * Mostly this file is about the two ways a secret file goes wrong: two
 * processes creating it at once, and it being readable by somebody who is not
 * the owner. The control token had the first of those (F32) and the fix there
 * was the same shape, so the same test exists here on purpose.
 */
import { promises as fs } from 'node:fs';
import path from 'node:path';
import { describe, expect, it } from 'vitest';
import { configure, issueSession, validSession } from '@/lib/auth';
import { SECRET_FILE, authConfig, ownerSecret, secretFingerprint, secretPath } from '@/lib/owner';
import { jarvisHome, useTempHome } from './helpers';

useTempHome();

describe('the secret file', () => {
  it('is created on first use and reused afterwards', async () => {
    const first = await ownerSecret();
    const second = await ownerSecret();

    expect(first).toBe(second);
    expect(await fs.readFile(secretPath(), 'utf8')).toBe(first);
  });

  it('is 32 random bytes, so guessing it is not an attack', async () => {
    const secret = await ownerSecret();

    expect(secret).toMatch(/^[A-Za-z0-9_-]{43}$/);
    expect(Buffer.from(secret, 'base64url')).toHaveLength(32);
  });

  it('is readable only by the account that owns it', async () => {
    await ownerSecret();
    const mode = (await fs.stat(secretPath())).mode & 0o777;

    // Windows does not carry these bits; everywhere else this is the point.
    if (process.platform !== 'win32') expect(mode).toBe(0o600);
  });

  it('is the same secret for everything that starts at once', async () => {
    const secrets = await Promise.all(Array.from({ length: 8 }, () => ownerSecret()));

    expect(new Set(secrets).size).toBe(1);
  });

  it('waits for a file another process is still writing rather than deleting it', async () => {
    const file = path.join(jarvisHome(), SECRET_FILE);
    await fs.writeFile(file, '', 'utf8');

    const reader = ownerSecret();
    const winner = 'w'.repeat(43);
    setTimeout(() => void fs.writeFile(file, winner, 'utf8'), 30);

    expect(await reader).toBe(winner);
  });

  it('is not the control token', async () => {
    await fs.writeFile(path.join(jarvisHome(), 'control.token'), 'control-token', 'utf8');

    expect(await ownerSecret()).not.toBe('control-token');
  });
});

describe('the configuration built from it', () => {
  it('signs sessions that validate', async () => {
    const config = await authConfig();

    expect(validSession(issueSession(config), config)).toBe(true);
  });

  it('refuses a secret that is not 32 random bytes', () => {
    for (const bad of [undefined, '', 'short', 'x'.repeat(43) + '!', 'a'.repeat(100)]) {
      expect(() => configure(bad as string | undefined)).toThrow();
    }
  });

  it('refuses an origin that is not loopback', () => {
    const secret = 'a'.repeat(43);
    for (const bad of [
      'http://192.168.1.10:3000',
      'http://jarvis.example',
      'http://0.0.0.0:3000',
      'http://user:pass@127.0.0.1:3000',
      'http://127.0.0.1:3000/somewhere',
      'ftp://127.0.0.1:3000',
    ]) {
      expect(() => configure(secret, bad)).toThrow();
    }
  });

  it('accepts the loopback origins the launcher might use', () => {
    const secret = 'a'.repeat(43);
    for (const good of ['http://127.0.0.1:3000', 'http://localhost:3000', 'http://[::1]:3000']) {
      expect(configure(secret, good).origin).toBe(new URL(good).origin);
    }
  });
});

describe('the fingerprint', () => {
  it('identifies a secret without revealing it', async () => {
    const secret = await ownerSecret();
    const fingerprint = secretFingerprint(secret);

    expect(fingerprint).toMatch(/^[0-9a-f]{8}$/);
    expect(secret).not.toContain(fingerprint);
    expect(secretFingerprint(secret)).toBe(fingerprint);
    expect(secretFingerprint('something else')).not.toBe(fingerprint);
  });
});
