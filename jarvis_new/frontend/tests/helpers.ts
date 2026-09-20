/**
 * Shared scaffolding for the dashboard's server-side tests.
 *
 * Every test gets its own JARVIS folder, so the secret one test creates is
 * never the secret another test reads, and nothing here touches a real
 * installation's files.
 */
import { promises as fs } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { afterEach, beforeEach, vi } from 'vitest';
import { COOKIE, TTL, configure, issueSession } from '@/lib/auth';
import { ownerSecret, resetConfigForTests } from '@/lib/owner';

export const ORIGIN = 'http://127.0.0.1:3000';
export const HOST = '127.0.0.1:3000';

let home = '';

/** The JARVIS folder this test is using. */
export function jarvisHome(): string {
  return home;
}

/** The owner secret for this test's home, creating it if nothing has yet. */
export async function secretOnDisk(): Promise<string> {
  return ownerSecret();
}

/** Put a control token where the proxy will find it. */
export async function writeControlToken(value = 'test-control-token'): Promise<void> {
  await fs.writeFile(path.join(home, 'control.token'), value, 'utf8');
}

/**
 * A fresh temporary home, a clean config cache and a stubbed `fetch` for
 * every test.
 *
 * The stub matters as much as the isolation: the F01 acceptance is that a
 * rejected request makes *no* upstream call, and the only way to assert that
 * is to own the one function that would make it.
 */
export function useTempHome(): { fetchMock: ReturnType<typeof vi.fn> } {
  const holder = { fetchMock: vi.fn() };

  beforeEach(async () => {
    home = await fs.mkdtemp(path.join(os.tmpdir(), 'jarvis-dash-'));
    process.env.JARVIS_HOME = home;
    process.env.JARVIS_DASHBOARD_ORIGIN = ORIGIN;
    resetConfigForTests();
    holder.fetchMock = vi.fn(
      async () =>
        new Response('{"ok":true}', {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        })
    );
    vi.stubGlobal('fetch', holder.fetchMock);
  });

  afterEach(async () => {
    vi.unstubAllGlobals();
    resetConfigForTests();
    delete process.env.JARVIS_HOME;
    delete process.env.JARVIS_DASHBOARD_ORIGIN;
    await fs.rm(home, { recursive: true, force: true });
  });

  return holder;
}

type RequestOptions = {
  method?: string;
  path?: string;
  body?: string;
  cookie?: string | null;
  origin?: string | null;
  host?: string | null;
  headers?: Record<string, string>;
};

/** Build a request the way a browser on this machine would send one. */
export function request(options: RequestOptions = {}): Request {
  const method = options.method ?? 'GET';
  const headers: Record<string, string> = { ...(options.headers ?? {}) };

  const host = options.host === undefined ? HOST : options.host;
  if (host !== null) headers.host = host;

  const origin = options.origin === undefined ? (method === 'GET' ? null : ORIGIN) : options.origin;
  if (origin !== null) headers.origin = origin;

  if (options.cookie) headers.cookie = options.cookie;
  if (options.body !== undefined) headers['content-type'] = 'application/json';

  return new Request(`${ORIGIN}${options.path ?? '/api/control/state'}`, {
    method,
    headers,
    body: options.body,
  });
}

/** A cookie header carrying a session this dashboard would accept. */
export async function ownerCookie(offset = 0): Promise<string> {
  const config = configure(await secretOnDisk(), ORIGIN);
  const now = Math.floor(Date.now() / 1000) + offset;
  return `${COOKIE}=${issueSession(config, now)}`;
}

/** A session that was valid once and is not any more. */
export async function expiredCookie(): Promise<string> {
  return ownerCookie(-(TTL + 60));
}

/** Route context in the shape Next hands to a catch-all handler. */
export function context(path: string[]): { params: Promise<{ path: string[] }> } {
  return { params: Promise.resolve({ path }) };
}
