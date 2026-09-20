/**
 * The voice token route.
 *
 * This one shipped from the LiveKit starter as a development endpoint that
 * threw in production. In a reachable development setup it minted room
 * credentials for anyone who asked, and it took the room configuration from
 * the request body, so the caller chose which agent got dispatched.
 */
import { decodeJwt } from 'jose';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { POST } from '@/app/api/token/route';
import { ownerCookie, request, useTempHome } from './helpers';

useTempHome();

beforeEach(() => {
  vi.stubEnv('LIVEKIT_URL', 'wss://example.livekit.cloud');
  vi.stubEnv('LIVEKIT_API_KEY', 'APItestkey');
  vi.stubEnv('LIVEKIT_API_SECRET', 'a'.repeat(32));
});

function tokenRequest(overrides: Parameters<typeof request>[0] = {}): Request {
  return request({ method: 'POST', path: '/api/token', body: '{}', ...overrides });
}

describe('who can ask for voice credentials', () => {
  it('refuses an anonymous caller', async () => {
    const response = await POST(tokenRequest());

    expect(response.status).toBe(401);
  });

  it('refuses another site', async () => {
    const response = await POST(
      tokenRequest({ origin: 'http://evil.example', cookie: await ownerCookie() })
    );

    expect(response.status).toBe(403);
  });

  it('refuses a request aimed at another host', async () => {
    const response = await POST(
      tokenRequest({ host: 'jarvis.attacker.example', cookie: await ownerCookie() })
    );

    expect(response.status).toBe(403);
  });

  it('serves the signed-in owner, in production as well as development', async () => {
    vi.stubEnv('NODE_ENV', 'production');

    const response = await POST(tokenRequest({ cookie: await ownerCookie() }));

    expect(response.status).toBe(200);
  });
});

describe('the credentials it hands out', () => {
  it('mints a token for a room the server chose', async () => {
    const response = await POST(tokenRequest({ cookie: await ownerCookie() }));
    const body = await response.json();

    expect(body.serverUrl).toBe('wss://example.livekit.cloud');
    expect(body.roomName).toMatch(/^jarvis_[0-9a-f-]{36}$/);

    const claims = decodeJwt(body.participantToken) as Record<string, unknown>;
    expect((claims.video as Record<string, unknown>).room).toBe(body.roomName);
    expect(claims.sub).toMatch(/^owner_[0-9a-f-]{36}$/);
  });

  it('gives a different room every time, rather than one of ten thousand', async () => {
    const cookie = await ownerCookie();
    const rooms = new Set<string>();

    for (let i = 0; i < 25; i += 1) {
      const response = await POST(tokenRequest({ cookie }));
      rooms.add((await response.json()).roomName);
    }

    expect(rooms.size).toBe(25);
  });

  it('expires, so a leaked token is not a standing invitation', async () => {
    const response = await POST(tokenRequest({ cookie: await ownerCookie() }));
    const claims = decodeJwt((await response.json()).participantToken);
    const lifetime = (claims.exp as number) - Math.floor(Date.now() / 1000);

    expect(lifetime).toBeGreaterThan(0);
    expect(lifetime).toBeLessThanOrEqual(15 * 60);
  });

  it('ignores a room configuration the caller tried to choose', async () => {
    const response = await POST(
      tokenRequest({
        cookie: await ownerCookie(),
        body: JSON.stringify({
          room_config: { agents: [{ agent_name: 'something-else' }] },
          room_name: 'a-room-i-picked',
        }),
      })
    );
    const body = await response.json();
    const claims = decodeJwt(body.participantToken) as Record<string, unknown>;

    expect(body.roomName).not.toBe('a-room-i-picked');
    expect(JSON.stringify(claims)).not.toContain('something-else');
  });

  it('is never cached', async () => {
    const response = await POST(tokenRequest({ cookie: await ownerCookie() }));

    expect(response.headers.get('cache-control')).toBe('no-store');
  });
});

describe('before the keys are set up', () => {
  it('says what to do instead of failing obscurely', async () => {
    vi.stubEnv('LIVEKIT_API_SECRET', '');

    const response = await POST(tokenRequest({ cookie: await ownerCookie() }));

    expect(response.status).toBe(503);
    expect((await response.json()).error).toContain('butler-setup');
  });

  it('checks the caller first, so a stranger cannot probe the setup', async () => {
    vi.stubEnv('LIVEKIT_API_SECRET', '');

    const response = await POST(tokenRequest());

    expect(response.status).toBe(401);
  });
});
