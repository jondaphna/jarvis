/**
 * Is the person looking at this page the owner?
 *
 * Used by the root layout, which is the one place every page goes through.
 * Gating there rather than per page means a page added later is protected by
 * default - the alternative is a list of protected routes that someone
 * eventually forgets to add to.
 *
 * This is the *view*. The API routes do their own checking, including the
 * origin checks this deliberately skips: a navigation has no meaningful
 * origin, and a dashboard that renders is not a dashboard that can do
 * anything. Nothing here is the security boundary.
 */
import { cookies } from 'next/headers';
import { COOKIE, validSession } from './auth';
import { authConfig } from './owner';

export async function ownerSignedIn(): Promise<boolean> {
  try {
    const config = await authConfig();
    const value = (await cookies()).get(COOKIE)?.value;
    return typeof value === 'string' && validSession(value, config);
  } catch {
    // No secret file and none creatable. Showing the sign-in screen is the
    // honest answer: the form will say what went wrong when it is used.
    return false;
  }
}
