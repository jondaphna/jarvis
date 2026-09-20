import { NextResponse } from 'next/server';
import { AccessToken, type AccessTokenOptions, type VideoGrant } from 'livekit-server-sdk';
import { randomUUID } from 'node:crypto';
import { RoomConfiguration } from '@livekit/protocol';
import { authorized, requestOriginAllowed } from '@/lib/auth';
import { authConfig } from '@/lib/owner';

type ConnectionDetails = {
  serverUrl: string;
  roomName: string;
  participantName: string;
  participantToken: string;
};

/**
 * Read at call time rather than captured at import.
 *
 * A module-level `const` freezes whatever the environment held the first time
 * this file was loaded, which is a way for a key added by `butler-setup.bat`
 * to be ignored until the next restart for no visible reason.
 */
function livekitCredentials(): { url?: string; key?: string; secret?: string } {
  return {
    url: process.env.LIVEKIT_URL,
    key: process.env.LIVEKIT_API_KEY,
    secret: process.env.LIVEKIT_API_SECRET,
  };
}

export const revalidate = 0;
export const dynamic = 'force-dynamic';

/**
 * Credentials for joining the voice room.
 *
 * This arrived from the LiveKit starter as a development endpoint that threw
 * in production - honest about being unauthenticated, and therefore not a
 * working pairing flow either. The September 2026 re-audit (F08) noted the
 * two things that made it worse than it looked: in a reachable development
 * setup anyone could ask for room credentials, and the room configuration
 * came from the request body, so a caller chose which agent was dispatched.
 *
 * It takes the same owner session as the rest of the dashboard now, which
 * also means it works in production rather than throwing. Three other things
 * changed and each is a small one:
 *
 * - **The server picks the room.** Identifiers are `randomUUID()` rather than
 *   `Math.random() * 10_000`, which collided about as often as you would
 *   expect from ten thousand values, and a collision is two people in one
 *   room.
 * - **The client does not configure dispatch.** `room_config` used to be
 *   taken from the body. Whatever the browser sent decided what ran.
 * - **Short-lived and uncached.** Fifteen minutes, `no-store`.
 */
export async function POST(req: Request) {
  const config = await authConfig();

  if (!requestOriginAllowed(req, config, true)) {
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

  const { url, key, secret } = livekitCredentials();
  if (!url || !key || !secret) {
    return NextResponse.json(
      {
        error:
          'The LiveKit keys are not set up yet. Run butler-setup.bat, or fill in ' +
          'LIVEKIT_URL, LIVEKIT_API_KEY and LIVEKIT_API_SECRET.',
      },
      { status: 503, headers: { 'Cache-Control': 'no-store' } }
    );
  }

  try {
    const participantName = 'user';
    const participantIdentity = `owner_${randomUUID()}`;
    const roomName = `jarvis_${randomUUID()}`;

    const participantToken = await createParticipantToken(
      { identity: participantIdentity, name: participantName },
      roomName,
      // Chosen here, not by the caller. A browser that can name the agent to
      // dispatch is a browser that can run something other than JARVIS.
      new RoomConfiguration(),
      key,
      secret
    );

    const data: ConnectionDetails = {
      serverUrl: url,
      roomName,
      participantName,
      participantToken,
    };
    return NextResponse.json(data, { headers: new Headers({ 'Cache-Control': 'no-store' }) });
  } catch (error) {
    console.error(error);
    return NextResponse.json(
      { error: "Couldn't mint a voice token." },
      { status: 500, headers: { 'Cache-Control': 'no-store' } }
    );
  }
}

function createParticipantToken(
  userInfo: AccessTokenOptions,
  roomName: string,
  roomConfig: RoomConfiguration | undefined,
  apiKey: string,
  apiSecret: string
): Promise<string> {
  const at = new AccessToken(apiKey, apiSecret, {
    ...userInfo,
    ttl: '15m',
  });
  const grant: VideoGrant = {
    room: roomName,
    roomJoin: true,
    canPublish: true,
    canPublishData: true,
    canSubscribe: true,
  };
  at.addGrant(grant);

  if (roomConfig) {
    at.roomConfig = roomConfig;
  }

  return at.toJwt();
}
