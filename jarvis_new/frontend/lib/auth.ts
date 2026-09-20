/**
 * Who is allowed to talk to the dashboard.
 *
 * The September 2026 re-audit's F01: binding Next to `127.0.0.1` removed the
 * previous LAN listener, but it authenticated nobody. The proxy read the
 * backend control token and attached it to whatever arrived, without checking
 * the caller, the session, the origin, or even which paths and methods were
 * supposed to exist. Anything that could make a local HTTP request had the
 * dashboard's full authority: read the key vault, rewrite the permission
 * matrix, stop the services. Loopback is a smaller audience, not an empty one.
 *
 * So there is a session now, and it is built out of the pieces that are
 * actually load-bearing for a single-owner app on one machine:
 *
 * - **One owner, one secret.** A 32-byte random secret in the JARVIS folder,
 *   readable only by the account that owns it. Knowing it is what makes you
 *   the owner; the launcher knows it, so signing in is normally invisible.
 * - **A signed session cookie, not the secret.** The cookie carries an issue
 *   time, an expiry and a nonce, signed with the secret. The secret itself
 *   never goes to the browser and never leaves the server.
 * - **Host and Origin, exactly.** A request whose `Host` is not the
 *   configured one is not ours, and a mutation whose `Origin` is not exactly
 *   ours is somebody else's page talking to our port. Both are checked before
 *   anything upstream is touched.
 *
 * What this is not: a multi-user system. There is one owner, one secret and
 * one session shape. Adding a second person means adding accounts first, not
 * handing out a second copy of this.
 */
import { createHash, createHmac, randomBytes, timingSafeEqual } from 'node:crypto';

/** The session cookie's name. */
export const COOKIE = 'jarvis_session';

/** How long a session lasts, in seconds. */
export const TTL = 3600;

/** The longest cookie header worth parsing, as a cheap denial-of-service bound. */
const MAX_COOKIE_BYTES = 4096;

/** The longest offered secret worth comparing. */
const MAX_SECRET_BYTES = 128;

export type AuthConfig = Readonly<{
  secret: string;
  /** The origin the launcher advertises, used when one has to be named. */
  origin: string;
  /** Every loopback spelling of that origin, all equivalent on this machine. */
  origins: readonly string[];
  /** The `Host` values those origins produce. */
  hosts: readonly string[];
}>;

/** The three ways this machine spells its own address. */
const LOOPBACK = ['127.0.0.1', 'localhost', '[::1]'] as const;

/**
 * Validate the secret and the origin once, at startup, and hand back the
 * frozen pair everything else takes.
 *
 * The origin is restricted to loopback on purpose. This module's security
 * argument is "the only thing that can reach this port is this machine"; an
 * origin on a routable interface quietly removes that argument while looking
 * like configuration.
 */
export function configure(
  secret: string | undefined,
  origin = 'http://127.0.0.1:3000'
): AuthConfig {
  if (!/^[A-Za-z0-9_-]{43}$/.test(secret ?? '')) {
    throw new Error('The dashboard secret must be 32 random bytes as base64url.');
  }
  const parsed = new URL(origin);
  const hostname = parsed.hostname === '::1' ? '[::1]' : parsed.hostname;
  if (
    !LOOPBACK.includes(hostname as (typeof LOOPBACK)[number]) ||
    !['http:', 'https:'].includes(parsed.protocol) ||
    parsed.username ||
    parsed.password ||
    parsed.pathname !== '/' ||
    parsed.search ||
    parsed.hash
  ) {
    throw new Error('The dashboard origin must be an exact loopback origin.');
  }

  // All three names reach this process and nothing else, so a page served
  // from one of them is the same page. Accepting only the one the launcher
  // happens to use is how `butler-settings.bat` opening `localhost:3000` ends
  // up refused by a dashboard configured for `127.0.0.1:3000`. What is *not*
  // accepted is any other name: a domain an attacker points at 127.0.0.1
  // arrives with its own `Host`, and that is the rebinding this refuses.
  const port = parsed.port ? `:${parsed.port}` : '';
  const hosts = LOOPBACK.map((name) => `${name}${port}`);
  const origins = hosts.map((host) => `${parsed.protocol}//${host}`);

  return Object.freeze({
    secret: secret as string,
    origin: parsed.origin,
    origins: Object.freeze(origins),
    hosts: Object.freeze(hosts),
  });
}

function digest(value: string): Buffer {
  return createHash('sha256').update(value).digest();
}

/**
 * Does this offered secret match the owner's?
 *
 * Both sides are hashed before comparison so the buffers are always the same
 * length - `timingSafeEqual` throws on a length mismatch, and a comparison
 * that throws for wrong-length input has told the caller the length.
 */
export function pairingSecretMatches(offered: unknown, config: AuthConfig): boolean {
  if (typeof offered !== 'string' || offered.length > MAX_SECRET_BYTES) return false;
  return timingSafeEqual(digest(offered), digest(config.secret));
}

/**
 * Is this request coming from our own page, on our own port?
 *
 * `Origin` is absent on some ordinary same-origin GETs, which is why reads
 * accept its absence and mutations do not. `Sec-Fetch-Site` is belt to that
 * braces where the browser sends it.
 */
export function requestOriginAllowed(
  request: Request,
  config: AuthConfig,
  mutation = request.method !== 'GET'
): boolean {
  const host = request.headers.get('host');
  if (host === null || !config.hosts.includes(host)) return false;
  if (request.headers.get('sec-fetch-site') === 'cross-site') return false;
  const offered = request.headers.get('origin');
  if (offered === null) return !mutation;
  return config.origins.includes(offered);
}

function sign(body: string, config: AuthConfig): string {
  return createHmac('sha256', Buffer.from(config.secret, 'base64url'))
    .update(body)
    .digest('base64url');
}

/** Mint a session for an owner who has just proved they know the secret. */
export function issueSession(config: AuthConfig, now = Math.floor(Date.now() / 1000)): string {
  const body = Buffer.from(
    JSON.stringify({
      v: 1,
      iat: now,
      exp: now + TTL,
      nonce: randomBytes(16).toString('base64url'),
    })
  ).toString('base64url');
  return `${body}.${sign(body, config)}`;
}

/**
 * Is this cookie value a session we issued, and still live?
 *
 * The signature is checked before the body is parsed, so unsigned input never
 * reaches `JSON.parse`. Every field is then checked for the shape we mint,
 * including that the lifetime is exactly ours: a body claiming a ten-year
 * expiry is not a session, whoever signed it.
 */
export function validSession(
  value: unknown,
  config: AuthConfig,
  now = Math.floor(Date.now() / 1000)
): boolean {
  if (typeof value !== 'string' || value.length > 512) return false;
  const pieces = value.split('.');
  if (
    pieces.length !== 2 ||
    !/^[A-Za-z0-9_-]+$/.test(pieces[0]) ||
    !/^[A-Za-z0-9_-]{43}$/.test(pieces[1])
  ) {
    return false;
  }
  if (!timingSafeEqual(digest(pieces[1]), digest(sign(pieces[0], config)))) return false;
  try {
    const body = JSON.parse(Buffer.from(pieces[0], 'base64url').toString('utf8'));
    return (
      body.v === 1 &&
      Number.isSafeInteger(body.iat) &&
      Number.isSafeInteger(body.exp) &&
      body.iat <= now &&
      body.exp > now &&
      body.exp - body.iat === TTL &&
      typeof body.nonce === 'string' &&
      /^[A-Za-z0-9_-]{22}$/.test(body.nonce)
    );
  } catch {
    return false;
  }
}

/** The one session cookie on this request, if there is exactly one. */
export function sessionFrom(request: Request): string | null {
  const raw = request.headers.get('cookie') ?? '';
  if (raw.length > MAX_COOKIE_BYTES) return null;
  const matches = raw
    .split(';')
    .map((item) => item.trim())
    .filter((item) => item.startsWith(`${COOKIE}=`));
  // Exactly one: two cookies of the same name is a sign somebody is trying to
  // confuse whichever one gets read first.
  return matches.length === 1 ? matches[0].slice(COOKIE.length + 1) : null;
}

/** Is this request from the signed-in owner, on an acceptable origin? */
export function authorized(request: Request, config: AuthConfig, now?: number): boolean {
  if (!requestOriginAllowed(request, config)) return false;
  const session = sessionFrom(request);
  return session !== null && validSession(session, config, now);
}

export function sessionCookie(value: string, config: AuthConfig): string {
  return (
    `${COOKIE}=${value}; HttpOnly; SameSite=Strict; Path=/; Max-Age=${TTL}` +
    (config.origin.startsWith('https:') ? '; Secure' : '')
  );
}

export function clearedCookie(config: AuthConfig): string {
  return (
    `${COOKIE}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0` +
    (config.origin.startsWith('https:') ? '; Secure' : '')
  );
}
