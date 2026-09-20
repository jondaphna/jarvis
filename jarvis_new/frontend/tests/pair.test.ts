/**
 * Signing in and signing out.
 *
 * The interesting test in here is the round trip: sign in, take the cookie
 * the route actually set, and use it on the proxy. That is the only test that
 * proves the two halves agree, and it is the one that would have caught a
 * session shape the proxy mints but will not accept.
 */
import { beforeEach, describe, expect, it } from 'vitest';
import { GET as controlGET } from '@/app/api/control/[...path]/route';
import { DELETE, POST } from '@/app/api/pair/route';
import { COOKIE } from '@/lib/auth';
import { resetRateLimitForTests } from '@/lib/pair-limit';
import { context, request, secretOnDisk, useTempHome, writeControlToken } from './helpers';

const holder = useTempHome();

beforeEach(async () => {
  resetRateLimitForTests();
  await writeControlToken();
});

function pairRequest(body: unknown, overrides: Parameters<typeof request>[0] = {}): Request {
  return request({
    method: 'POST',
    path: '/api/pair',
    body: typeof body === 'string' ? body : JSON.stringify(body),
    ...overrides,
  });
}

describe('signing in', () => {
  it('sets a session when the secret is right', async () => {
    const response = await POST(pairRequest({ secret: await secretOnDisk() }));

    expect(response.status).toBe(200);
    expect(response.headers.get('set-cookie')).toContain(`${COOKIE}=`);
  });

  it('sets the cookie so a page script cannot read it or send it elsewhere', async () => {
    const response = await POST(pairRequest({ secret: await secretOnDisk() }));
    const cookie = response.headers.get('set-cookie') ?? '';

    expect(cookie).toContain('HttpOnly');
    expect(cookie).toContain('SameSite=Strict');
    expect(cookie).toContain('Max-Age=3600');
  });

  it('issues a session the proxy then accepts', async () => {
    const signIn = await POST(pairRequest({ secret: await secretOnDisk() }));
    const cookie = (signIn.headers.get('set-cookie') ?? '').split(';')[0];

    const response = await controlGET(request({ cookie }), context(['state']));

    expect(response.status).toBe(200);
    expect(holder.fetchMock).toHaveBeenCalledTimes(1);
  });
});

describe('not signing in', () => {
  it('refuses the wrong secret', async () => {
    const response = await POST(pairRequest({ secret: 'not-the-secret' }));

    expect(response.status).toBe(401);
    expect(response.headers.get('set-cookie')).toBeNull();
  });

  it('refuses a secret that is not a string', async () => {
    for (const secret of [null, 42, { toString: () => 'x' }, ['x']]) {
      const response = await POST(pairRequest({ secret }));
      expect(response.status).toBe(401);
    }
  });

  it('never says what the real secret is', async () => {
    const real = await secretOnDisk();
    const response = await POST(pairRequest({ secret: 'wrong' }));

    expect(await response.text()).not.toContain(real);
  });

  it('answers the same way whether the secret is missing, short or long', async () => {
    const bodies = [{}, { secret: '' }, { secret: 'a' }, { secret: 'a'.repeat(500) }];
    const seen = new Set<string>();

    for (const body of bodies) {
      const response = await POST(pairRequest(body));
      seen.add(`${response.status} ${await response.text()}`);
    }

    expect(seen.size).toBe(1);
  });

  it('refuses a sign-in offered by another site', async () => {
    const response = await POST(
      pairRequest({ secret: await secretOnDisk() }, { origin: 'http://evil.example' })
    );

    expect(response.status).toBe(403);
    expect(response.headers.get('set-cookie')).toBeNull();
  });

  it('refuses a sign-in with no origin at all', async () => {
    const response = await POST(pairRequest({ secret: await secretOnDisk() }, { origin: null }));

    expect(response.status).toBe(403);
  });

  it('refuses something that is not JSON', async () => {
    const response = await POST(pairRequest('this is not json'));

    expect(response.status).toBe(400);
  });

  it('stops guessing after ten tries in a minute', async () => {
    for (let attempt = 0; attempt < 10; attempt += 1) {
      const response = await POST(pairRequest({ secret: 'wrong' }));
      expect(response.status).toBe(401);
    }

    const blocked = await POST(pairRequest({ secret: await secretOnDisk() }));

    expect(blocked.status).toBe(429);
    expect(blocked.headers.get('set-cookie')).toBeNull();
  });
});

describe('signing out', () => {
  it('clears the cookie', async () => {
    const response = await DELETE(request({ method: 'DELETE', path: '/api/pair' }));

    expect(response.status).toBe(200);
    expect(response.headers.get('set-cookie')).toContain('Max-Age=0');
  });

  it('leaves a session that the proxy no longer accepts', async () => {
    const cleared = await DELETE(request({ method: 'DELETE', path: '/api/pair' }));
    const cookie = (cleared.headers.get('set-cookie') ?? '').split(';')[0];

    const response = await controlGET(request({ cookie }), context(['state']));

    expect(response.status).toBe(401);
    expect(holder.fetchMock).not.toHaveBeenCalled();
  });

  it('cannot be triggered by another site', async () => {
    const response = await DELETE(
      request({ method: 'DELETE', path: '/api/pair', origin: 'http://evil.example' })
    );

    expect(response.status).toBe(403);
  });
});
