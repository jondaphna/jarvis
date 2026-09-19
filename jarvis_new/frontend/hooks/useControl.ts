'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { api } from '@/lib/jarvis';

export interface Polled<T> {
  data: T | null;
  error: string;
  /** True only before the first answer. Later refreshes never blank the page. */
  loading: boolean;
  /** Fetch now, and reset the clock. Returns once the new data is in. */
  reload: () => Promise<void>;
}

/**
 * Poll one control-API path and keep the latest answer.
 *
 * Polling rather than a socket, for one reason worth writing down: the
 * background worker host has no publisher, so a socket would need a second
 * channel out of the agent process, and the one thing the voice loop may
 * never do is wait on the interface. A GET every few seconds costs a few
 * microseconds of SQLite read on a thread nobody is listening to.
 *
 * Two behaviours matter:
 *
 * * **A hidden tab polls nothing.** Left open overnight, a 3-second timer is
 *   28,000 pointless round trips; `visibilitychange` starts it again, with an
 *   immediate fetch so the first thing you see is current rather than stale.
 * * **An error keeps the last good data.** The control service restarts with
 *   the agent, and a panel that empties itself every time is unreadable.
 */
export function usePolled<T>(path: string, everyMs = 4000, active = true): Polled<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  //  Held in a ref so `reload` never changes identity: it goes into effect
  //  dependency lists and into onClick handlers all over the dashboard.
  const inFlight = useRef(false);

  const fetchOnce = useCallback(async () => {
    if (inFlight.current) return;
    inFlight.current = true;
    try {
      setData(await api<T>(path));
      setError('');
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      inFlight.current = false;
      setLoading(false);
    }
  }, [path]);

  useEffect(() => {
    if (!active) return;

    let timer: ReturnType<typeof setInterval> | null = null;

    const start = () => {
      if (timer !== null) return;
      void fetchOnce();
      timer = setInterval(() => void fetchOnce(), everyMs);
    };
    const stop = () => {
      if (timer === null) return;
      clearInterval(timer);
      timer = null;
    };
    const onVisibility = () => (document.hidden ? stop() : start());

    if (!document.hidden) start();
    document.addEventListener('visibilitychange', onVisibility);
    return () => {
      stop();
      document.removeEventListener('visibilitychange', onVisibility);
    };
  }, [active, everyMs, fetchOnce]);

  return { data, error, loading, reload: fetchOnce };
}
