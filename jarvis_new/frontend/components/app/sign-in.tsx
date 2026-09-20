'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { KeyRound, LoaderCircle } from 'lucide-react';
import { Button } from '@/components/ui/button';

/**
 * The way in.
 *
 * Normally nobody reads this screen. The launcher opens the dashboard with
 * `?key=…`, this component posts that key to `/api/pair`, strips it out of
 * the address bar and reloads into the console - about a tenth of a second,
 * and no login. The form below is for the times that does not happen: a
 * bookmark, a second browser, a tab that has been open past its hour.
 *
 * Two deliberate choices:
 *
 * **The key goes in the URL, once.** It is the only thing a `.bat` file can
 * hand a browser. It is removed from the address bar before anything else
 * runs, and it goes no further than this machine's loopback interface - but
 * it does land in that browser's history, which is why the same key is not
 * also the control token and why `butler-settings.bat` is the intended way in
 * rather than a saved bookmark.
 *
 * **It never says what was wrong.** Right key or wrong key, the answer is the
 * same sentence, because a login that distinguishes "no such key" from "wrong
 * key" is a login that answers questions for whoever is asking.
 */
export function SignIn() {
  const [key, setKey] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [auto, setAuto] = useState(true);
  const tried = useRef(false);

  const pair = useCallback(async (secret: string) => {
    setBusy(true);
    setError('');
    try {
      const response = await fetch('/api/pair', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ secret }),
        cache: 'no-store',
      });
      if (response.ok) {
        // A full reload rather than a router refresh: the gate that sent us
        // here is in the root layout, and this is the one moment where
        // throwing the whole page away is exactly right. The rest of the
        // query survives - the orb's `?autostart=1` is how the call starts
        // by itself, and landing on a silent page instead would look broken.
        window.location.replace(`${window.location.pathname}${window.location.search}`);
        return true;
      }
      const body = await response.json().catch(() => ({}));
      setError(
        response.status === 429
          ? 'Too many tries. Wait a minute, then run butler-settings.bat again.'
          : ((body as { error?: string }).error ?? "That isn't the dashboard key.")
      );
      return false;
    } catch {
      setError("Couldn't reach the dashboard. Is it still running?");
      return false;
    } finally {
      setBusy(false);
    }
  }, []);

  useEffect(() => {
    if (tried.current) return;
    tried.current = true;

    const url = new URL(window.location.href);
    const offered = url.searchParams.get('key');
    if (!offered) {
      setAuto(false);
      return;
    }

    // Out of the address bar before the request goes out, so it is gone even
    // if the answer never comes back.
    url.searchParams.delete('key');
    window.history.replaceState(null, '', `${url.pathname}${url.search}${url.hash}`);

    void pair(offered).then((ok) => {
      if (!ok) setAuto(false);
    });
  }, [pair]);

  if (auto) {
    return (
      <main className="flex min-h-svh items-center justify-center p-6">
        <p className="text-muted-foreground flex items-center gap-2 text-sm">
          <LoaderCircle className="size-4 animate-spin" />
          Signing in…
        </p>
      </main>
    );
  }

  return (
    <main className="flex min-h-svh items-center justify-center p-6">
      <form
        className="w-full max-w-sm space-y-4"
        onSubmit={(event) => {
          event.preventDefault();
          if (!busy && key.trim()) void pair(key.trim());
        }}
      >
        <div className="space-y-1">
          <h1 className="flex items-center gap-2 text-lg font-medium">
            <KeyRound className="size-4" />
            Jarvis
          </h1>
          <p className="text-muted-foreground text-sm">
            Run <code className="font-mono">butler-settings.bat</code> to open the console signed
            in, or paste the key from <code className="font-mono">dashboard.secret</code>.
          </p>
        </div>

        <input
          type="password"
          value={key}
          autoFocus
          autoComplete="off"
          spellCheck={false}
          onChange={(event) => setKey(event.target.value)}
          placeholder="Dashboard key"
          aria-label="Dashboard key"
          className="border-input bg-background w-full rounded-md border px-3 py-2 font-mono text-sm"
        />

        {error && (
          <p role="alert" className="text-destructive text-sm">
            {error}
          </p>
        )}

        <Button type="submit" disabled={busy || !key.trim()} className="w-full">
          {busy ? 'Checking…' : 'Sign in'}
        </Button>
      </form>
    </main>
  );
}
