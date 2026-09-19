import { NextResponse } from 'next/server';
import { CONTROL_URL, controlToken } from '@/lib/control';

export const revalidate = 0;
export const dynamic = 'force-dynamic';

/**
 * Passes browser requests through to the local control API, adding the token
 * on the way. The token is read here, on the server, so the page never holds
 * a credential that can write API keys into the vault.
 */
async function proxy(req: Request, params: Promise<{ path: string[] }>) {
  const { path } = await params;
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

  const query = new URL(req.url).search;
  const target = `${CONTROL_URL}/api/${path.join('/')}${query}`;

  const init: RequestInit = {
    method: req.method,
    headers: { 'X-Jarvis-Token': token, 'Content-Type': 'application/json' },
    cache: 'no-store',
  };
  if (req.method !== 'GET' && req.method !== 'DELETE') {
    init.body = await req.text();
  }

  try {
    const upstream = await fetch(target, init);
    const body = await upstream.text();
    return new NextResponse(body, {
      status: upstream.status,
      headers: { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' },
    });
  } catch {
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
