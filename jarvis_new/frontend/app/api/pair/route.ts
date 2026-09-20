import { NextResponse } from 'next/server';
import {
  clearedCookie,
  issueSession,
  pairingSecretMatches,
  requestOriginAllowed,
  sessionCookie,
} from '@/lib/auth';
import { authConfig } from '@/lib/owner';
import { rateLimited } from '@/lib/pair-limit';

export const revalidate = 0;
export const dynamic = 'force-dynamic';

const MAX_BODY_BYTES = 4096;

/**
 * Signing in, and signing out.
 *
 * POST with the owner secret and you get a session cookie; DELETE and it goes
 * away. Normally nobody does this by hand: the launcher opens the dashboard
 * with the secret in the URL, the page posts it here and strips it out of the
 * address bar, and the whole exchange is invisible.
 *
 * Attempts are rate limited per process. The secret is 32 random bytes, so
 * guessing it is not a realistic attack on its own - the limit is there
 * because an unbounded POST that does an HMAC is a way to keep a single-core
 * machine busy, and because a login endpoint with no ceiling is the kind of
 * thing that becomes a problem once something else changes.
 */

export async function POST(req: Request) {
  const config = await authConfig();

  if (!requestOriginAllowed(req, config, true)) {
    return NextResponse.json({ error: 'Wrong origin for this dashboard.' }, { status: 403 });
  }
  if (rateLimited()) {
    return NextResponse.json(
      { error: 'Too many sign-in attempts. Wait a minute and try again.' },
      { status: 429, headers: { 'Retry-After': '60' } }
    );
  }

  const raw = await req.text();
  if (raw.length > MAX_BODY_BYTES) {
    return NextResponse.json({ error: 'That request is too large.' }, { status: 413 });
  }

  let offered: unknown;
  try {
    const body = JSON.parse(raw || '{}');
    offered = body?.secret;
  } catch {
    return NextResponse.json({ error: 'That request is not JSON.' }, { status: 400 });
  }

  if (!pairingSecretMatches(offered, config)) {
    // Deliberately the same message and status whatever was wrong with it.
    return NextResponse.json({ error: "That isn't the dashboard key." }, { status: 401 });
  }

  return NextResponse.json(
    { ok: true },
    {
      status: 200,
      headers: {
        'Set-Cookie': sessionCookie(issueSession(config), config),
        'Cache-Control': 'no-store',
      },
    }
  );
}

export async function DELETE(req: Request) {
  const config = await authConfig();
  if (!requestOriginAllowed(req, config, true)) {
    return NextResponse.json({ error: 'Wrong origin for this dashboard.' }, { status: 403 });
  }
  return NextResponse.json(
    { ok: true },
    { status: 200, headers: { 'Set-Cookie': clearedCookie(config), 'Cache-Control': 'no-store' } }
  );
}
