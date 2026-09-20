/**
 * The launcher's key script and the server's own reader have to agree.
 *
 * They are separate files because a `.bat` cannot import TypeScript, and two
 * copies of anything drift. These tests are what stops the drift being
 * discovered as "the dashboard asks me to sign in every time".
 */
import { promises as fs } from 'node:fs';
import path from 'node:path';
import { describe, expect, it } from 'vitest';
import { pairingSecretMatches } from '@/lib/auth';
import { authConfig, ownerSecret, secretPath } from '@/lib/owner';
import { SECRET_FILE, dashboardKey, jarvisRoot } from '@/scripts/dashboard-key.mjs';
import { jarvisHome, useTempHome } from './helpers';

useTempHome();

describe('the launcher script and the server', () => {
  it('look in the same place', () => {
    expect(jarvisRoot()).toBe(jarvisHome());
    expect(path.join(jarvisRoot(), SECRET_FILE)).toBe(secretPath());
  });

  it('accept the key the other one created, whichever went first', async () => {
    const fromScript = await dashboardKey();
    const config = await authConfig();

    expect(await ownerSecret()).toBe(fromScript);
    expect(pairingSecretMatches(fromScript, config)).toBe(true);
  });

  it('agree when the server goes first instead', async () => {
    const fromServer = await ownerSecret();

    expect(await dashboardKey()).toBe(fromServer);
  });

  it('produce a key the dashboard will accept as a secret', async () => {
    expect(await dashboardKey()).toMatch(/^[A-Za-z0-9_-]{43}$/);
  });

  it('do not both create one when they start together', async () => {
    const keys = await Promise.all([dashboardKey(), ownerSecret(), dashboardKey(), ownerSecret()]);

    expect(new Set(keys).size).toBe(1);
  });

  it('write it readable only by its owner', async () => {
    await dashboardKey();
    const mode = (await fs.stat(secretPath())).mode & 0o777;

    if (process.platform !== 'win32') expect(mode).toBe(0o600);
  });
});
