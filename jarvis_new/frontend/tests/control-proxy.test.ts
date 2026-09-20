/**
 * The control proxy, tested through the route Next actually calls.
 *
 * Before F01 this handler read the backend control token and attached it to
 * whatever arrived. A unit test of the auth helper would not have caught that
 * - the helper was never called. So every test here imports the real `GET`,
 * `POST` and `DELETE` and asserts on two things: the status the caller sees,
 * and whether `fetch` was reached at all. The second is the one that matters.
 * A 401 that has already forwarded the request is not a refusal.
 */
import { beforeEach, describe, expect, it } from 'vitest';
import { DELETE, GET, POST } from '@/app/api/control/[...path]/route';
import { COOKIE } from '@/lib/auth';
import { routeAllowed } from '@/lib/control-routes';
import {
  context,
  expiredCookie,
  ownerCookie,
  request,
  useTempHome,
  writeControlToken,
} from './helpers';

const holder = useTempHome();

beforeEach(async () => {
  await writeControlToken();
});

describe('a request with no session', () => {
  it('is refused, and nothing upstream is touched', async () => {
    const response = await GET(request({ path: '/api/control/state' }), context(['state']));

    expect(response.status).toBe(401);
    expect(holder.fetchMock).not.toHaveBeenCalled();
  });

  it('is refused for writes too', async () => {
    const response = await POST(
      request({ method: 'POST', path: '/api/control/settings', body: '{"a":1}' }),
      context(['settings'])
    );

    expect(response.status).toBe(401);
    expect(holder.fetchMock).not.toHaveBeenCalled();
  });

  it('never leaks the control token in the refusal', async () => {
    const response = await GET(request(), context(['state']));
    const text = await response.text();

    expect(response.status).toBe(401);
    expect(text).not.toContain('test-control-token');
  });
});

describe('a request from somebody else', () => {
  it('is refused when the origin is a different site', async () => {
    const response = await POST(
      request({
        method: 'POST',
        origin: 'http://evil.example',
        body: '{}',
        cookie: await ownerCookie(),
      }),
      context(['settings'])
    );

    expect(response.status).toBe(403);
    expect(holder.fetchMock).not.toHaveBeenCalled();
  });

  it('is refused when the origin is a lookalike port', async () => {
    const response = await POST(
      request({
        method: 'POST',
        origin: 'http://127.0.0.1:3001',
        body: '{}',
        cookie: await ownerCookie(),
      }),
      context(['settings'])
    );

    expect(response.status).toBe(403);
    expect(holder.fetchMock).not.toHaveBeenCalled();
  });

  it('is refused when the host is not ours, session or no session', async () => {
    const response = await GET(
      request({ host: 'jarvis.attacker.example', cookie: await ownerCookie() }),
      context(['state'])
    );

    expect(response.status).toBe(403);
    expect(holder.fetchMock).not.toHaveBeenCalled();
  });

  it('is refused when the browser says the request is cross-site', async () => {
    const response = await GET(
      request({ cookie: await ownerCookie(), headers: { 'sec-fetch-site': 'cross-site' } }),
      context(['state'])
    );

    expect(response.status).toBe(403);
    expect(holder.fetchMock).not.toHaveBeenCalled();
  });

  it('refuses a read that carries a foreign origin, not just a write', async () => {
    const response = await GET(
      request({ origin: 'http://evil.example', cookie: await ownerCookie() }),
      context(['state'])
    );

    expect(response.status).toBe(403);
    expect(holder.fetchMock).not.toHaveBeenCalled();
  });
});

describe('the names this machine calls itself', () => {
  it.each([
    ['127.0.0.1:3000', 'http://127.0.0.1:3000'],
    ['localhost:3000', 'http://localhost:3000'],
    ['[::1]:3000', 'http://[::1]:3000'],
  ])('lets the owner in through %s', async (host, origin) => {
    const response = await POST(
      request({
        method: 'POST',
        path: '/api/control/settings',
        body: '{}',
        host,
        origin,
        cookie: await ownerCookie(),
      }),
      context(['settings'])
    );

    expect(response.status).toBe(200);
  });

  it('does not accept a host that merely resolves here', async () => {
    const response = await GET(
      request({ host: 'dashboard.attacker.example', cookie: await ownerCookie() }),
      context(['state'])
    );

    expect(response.status).toBe(403);
    expect(holder.fetchMock).not.toHaveBeenCalled();
  });

  it('does not let one loopback name vouch for another port', async () => {
    const response = await POST(
      request({
        method: 'POST',
        path: '/api/control/settings',
        body: '{}',
        host: 'localhost:3000',
        origin: 'http://localhost:4000',
        cookie: await ownerCookie(),
      }),
      context(['settings'])
    );

    expect(response.status).toBe(403);
  });
});

describe('a session that is not one of ours', () => {
  it('refuses a stale one', async () => {
    const response = await GET(request({ cookie: await expiredCookie() }), context(['state']));

    expect(response.status).toBe(401);
    expect(holder.fetchMock).not.toHaveBeenCalled();
  });

  it('refuses one signed with a different secret', async () => {
    const cookie = await ownerCookie();
    const [body] = cookie.slice(COOKIE.length + 1).split('.');
    const forged = `${COOKIE}=${body}.${'A'.repeat(43)}`;

    const response = await GET(request({ cookie: forged }), context(['state']));

    expect(response.status).toBe(401);
    expect(holder.fetchMock).not.toHaveBeenCalled();
  });

  it('refuses an unsigned body, however well formed', async () => {
    const claim = Buffer.from(
      JSON.stringify({ v: 1, iat: 0, exp: 9_999_999_999, nonce: 'a'.repeat(22) })
    ).toString('base64url');

    const response = await GET(request({ cookie: `${COOKIE}=${claim}.` }), context(['state']));

    expect(response.status).toBe(401);
    expect(holder.fetchMock).not.toHaveBeenCalled();
  });

  it('refuses when two session cookies arrive at once', async () => {
    const good = await ownerCookie();
    const response = await GET(
      request({ cookie: `${good}; ${COOKIE}=nonsense` }),
      context(['state'])
    );

    expect(response.status).toBe(401);
    expect(holder.fetchMock).not.toHaveBeenCalled();
  });
});

describe('the owner', () => {
  it('gets through, and the token is added on the way', async () => {
    const response = await GET(request({ cookie: await ownerCookie() }), context(['state']));

    expect(response.status).toBe(200);
    expect(holder.fetchMock).toHaveBeenCalledTimes(1);

    const [url, init] = holder.fetchMock.mock.calls[0];
    expect(url).toBe('http://127.0.0.1:8765/api/state');
    expect((init.headers as Record<string, string>)['X-Jarvis-Token']).toBe('test-control-token');
  });

  it('keeps the query string', async () => {
    await GET(
      request({ path: '/api/control/runs?limit=5', cookie: await ownerCookie() }),
      context(['runs'])
    );

    expect(holder.fetchMock.mock.calls[0][0]).toBe('http://127.0.0.1:8765/api/runs?limit=5');
  });

  it('can write, and the body goes through unchanged', async () => {
    const body = JSON.stringify({ permissions: { content: true } });
    const response = await POST(
      request({ method: 'POST', path: '/api/control/settings', body, cookie: await ownerCookie() }),
      context(['settings'])
    );

    expect(response.status).toBe(200);
    expect(holder.fetchMock.mock.calls[0][1].body).toBe(body);
  });
});

describe('the endpoints this dashboard uses', () => {
  it('turns away a path the dashboard does not call', async () => {
    const response = await GET(
      request({ cookie: await ownerCookie() }),
      context(['etc', 'passwd'])
    );

    expect(response.status).toBe(404);
    expect(holder.fetchMock).not.toHaveBeenCalled();
  });

  it('turns away a method the endpoint does not take', async () => {
    const response = await DELETE(
      request({ method: 'DELETE', cookie: await ownerCookie() }),
      context(['state'])
    );

    expect(response.status).toBe(404);
    expect(holder.fetchMock).not.toHaveBeenCalled();
  });

  it('refuses a traversal segment rather than normalising it', () => {
    expect(routeAllowed(['state', '..', '..', 'etc'], 'GET')).toBe(false);
    expect(routeAllowed(['state', ''], 'GET')).toBe(false);
    expect(routeAllowed([], 'GET')).toBe(false);
  });

  it('checks the caller before the path, so a stranger learns nothing about our routes', async () => {
    const unknown = await GET(request(), context(['no-such-endpoint']));
    const known = await GET(request(), context(['state']));

    expect(unknown.status).toBe(401);
    expect(known.status).toBe(401);
  });
});

describe('a body that is too large', () => {
  it('is refused on what it declares, without reading it', async () => {
    const response = await POST(
      request({
        method: 'POST',
        path: '/api/control/settings',
        body: '{}',
        cookie: await ownerCookie(),
        headers: { 'content-length': String(512 * 1024) },
      }),
      context(['settings'])
    );

    expect(response.status).toBe(413);
    expect(holder.fetchMock).not.toHaveBeenCalled();
  });

  it('is refused on what it actually sent, when it declared nothing', async () => {
    const response = await POST(
      request({
        method: 'POST',
        path: '/api/control/settings',
        body: JSON.stringify({ padding: 'x'.repeat(300 * 1024) }),
        cookie: await ownerCookie(),
      }),
      context(['settings'])
    );

    expect(response.status).toBe(413);
    expect(holder.fetchMock).not.toHaveBeenCalled();
  });
});

describe('when the control service is not there', () => {
  it('says so rather than hanging', async () => {
    holder.fetchMock.mockRejectedValueOnce(new TypeError('fetch failed'));

    const response = await GET(request({ cookie: await ownerCookie() }), context(['state']));

    expect(response.status).toBe(503);
  });

  it('gives up on a service that accepts and then says nothing', async () => {
    const timeout = new Error('timed out');
    timeout.name = 'TimeoutError';
    holder.fetchMock.mockRejectedValueOnce(timeout);

    const response = await GET(request({ cookie: await ownerCookie() }), context(['state']));

    expect(response.status).toBe(504);
  });
});
