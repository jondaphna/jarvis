import { NextResponse } from 'next/server';
import { authorized, requestOriginAllowed } from '@/lib/auth';
import { CONTROL_URL, controlToken } from '@/lib/control';
import { routeAllowed } from '@/lib/control-routes';
import { authConfig } from '@/lib/owner';

export const revalidate = 0;
export const dynamic = 'force-dynamic';

/**
 * Passes browser requests through to the local control API, adding the token
 * on the way. The token is read here, on the server, so the page never holds
 * a credential that can write API keys into the vault.
 *
 * Until the September 2026 re-audit (F01) that was the whole description, and
 * it was the problem: the token was attached to *whatever arrived*. Anything
 * that could make a local HTTP request inherited the dashboard's authority -
 * read the vault, rewrite the permission matrix, stop the services. Binding
 * Next to loopback had narrowed the audience without authenticating anyone.
 *
 * Four checks now happen before anything upstream is touched, in this order,
 * because each is cheaper than the next:
 *
 * 1. **Origin and host.** Someone else's page talking to our port is not us.
 * 2. **A signed owner session.** Anonymous gets 401, not the token.
 * 3. **A known path and method.** The proxy used to forward any path under
 *    `/api/` with any of three methods, so it was a general-purpose gateway
 *    into the control API rather than the dashboard's own back end.
 * 4. **A bounded body.** `req.text()` on an unbounded stream is a way to make
 *    this process eat a gigabyte; the Python side caps at 256 KiB and this
 *    now refuses before reading rather than forwarding and finding out.
 *
 * And the upstream call has a deadline, so a control API that accepts the
 * connection and then says nothing cannot pin a handler open indefinitely.
 */

/** Matches the Python handler's cap, so the two agree about what is too big. */
const MAX_BODY_BYTES = 256 * 1024;

/** How long to wait on the local control API before giving up. */
const UPSTREAM_TIMEOUT_MS = 15_000;

async function proxy(req: Request, params: Promise<{ path: string[] }>) {
  const config = await authConfig();

  if (!requestOriginAllowed(req, config)) {
    return NextResponse.json(
      { error: 'That request did not come from this dashboard.' },
      { status: 403, headers: { 'Cache-Control': 'no-store' } }
    );
  }

  if (!authorized(req, config)) {
    return NextResponse.json(
      { error: 'Sign in to this dashboard first.' },
      { status: 401, headers: { 'Cache-Control': 'no-store' } }
    );
  }

  const { path } = await params;
  if (!routeAllowed(path, req.method)) {
    return NextResponse.json(
      { error: 'This dashboard does not use that endpoint.' },
      { status: 404, headers: { 'Cache-Control': 'no-store' } }
    );
  }

  const token = await controlToken();
  if (!token) {
    return NextResponse.json(
      {
        error:
          "Jarvis's control service isn't running. Start it with butler-api.bat " +
          '(or run butler-agent.bat, which starts it too).',
      },
      { status: 503 }
    );
  }

  const init: RequestInit = {
    method: req.method,
    headers: { 'X-Jarvis-Token': token, 'Content-Type': 'application/json' },
    cache: 'no-store',
  };

  if (req.method !== 'GET' && req.method !== 'DELETE') {
    const declared = Number(req.headers.get('content-length') ?? '');
    if (Number.isFinite(declared) && declared > MAX_BODY_BYTES) {
      return NextResponse.json(
        { error: 'That request is too large.' },
        { status: 413, headers: { 'Cache-Control': 'no-store' } }
      );
    }
    const body = await req.text();
    // Checked again after reading: `Content-Length` is what the caller said,
    // not what it sent, and a chunked request declares nothing at all.
    if (Buffer.byteLength(body) > MAX_BODY_BYTES) {
      return NextResponse.json(
        { error: 'That request is too large.' },
        { status: 413, headers: { 'Cache-Control': 'no-store' } }
      );
    }
    init.body = body;
  }

  const query = new URL(req.url).search;
  const target = `${CONTROL_URL}/api/${path.join('/')}${query}`;

  try {
    const upstream = await fetch(target, {
      ...init,
      signal: AbortSignal.timeout(UPSTREAM_TIMEOUT_MS),
    });
    const body = await upstream.text();
    return new NextResponse(body, {
      status: upstream.status,
      headers: { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' },
    });
  } catch (error) {
    if (error instanceof Error && error.name === 'TimeoutError') {
      return NextResponse.json(
        { error: "Jarvis's control service took too long to answer." },
        { status: 504, headers: { 'Cache-Control': 'no-store' } }
      );
    }
    return NextResponse.json(
      {
        error:
          "Couldn't reach Jarvis's control service on " +
          `${CONTROL_URL}. Start it with butler-api.bat.`,
      },
      { status: 503 }
    );
  }
}

export async function GET(req: Request, ctx: { params: Promise<{ path: string[] }> }) {
  return proxy(req, ctx.params);
}
export async function POST(req: Request, ctx: { params: Promise<{ path: string[] }> }) {
  return proxy(req, ctx.params);
}
export async function DELETE(req: Request, ctx: { params: Promise<{ path: string[] }> }) {
  return proxy(req, ctx.params);
}
