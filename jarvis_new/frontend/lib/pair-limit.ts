/**
 * How often this process will check a sign-in attempt.
 *
 * The secret is 32 random bytes, so guessing it is not a realistic attack on
 * its own. The limit is here because an unbounded POST that does an HMAC is a
 * way to keep a single-core machine busy, and because a login endpoint with
 * no ceiling is the kind of thing that becomes a problem once something else
 * changes.
 *
 * Per process and in memory, which is the right scope for a dashboard that is
 * one Next server on one machine. A restart clears it; a restart also means
 * somebody with the machine, who has the secret file anyway.
 */
const WINDOW_MS = 60_000;
const MAX_ATTEMPTS = 10;

let windowStarted = 0;
let attempts = 0;

export function rateLimited(now = Date.now()): boolean {
  if (now - windowStarted > WINDOW_MS) {
    windowStarted = now;
    attempts = 0;
  }
  attempts += 1;
  return attempts > MAX_ATTEMPTS;
}

export function resetRateLimitForTests(): void {
  windowStarted = 0;
  attempts = 0;
}
