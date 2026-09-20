'use client';

import { useState } from 'react';
import { OctagonX, Play, RotateCcw, TriangleAlert } from 'lucide-react';
import { type ServicesState, post } from '@/lib/jarvis';
import { Action, Card, CardTitle, Pill, Row } from './kit';

/**
 * The master stop for everything running in the background.
 *
 * Three things shape this panel, and all three are about not lying to the
 * person pressing the button.
 *
 * **It asks first.** One click arms it, the second does it. A control that
 * stops the machine should not be a thing you can hit while reaching for the
 * tab beside it, and a confirm dialog is worse - people dismiss those without
 * reading. Arming shows exactly what is about to be stopped.
 *
 * **It says what it cannot do.** Cancelling a job that is a plain blocking
 * function does not end the function: Python cannot stop a thread from
 * outside, so a render or an API call already in flight runs to its end with
 * nobody waiting for the result. The panel says so before you press, not after.
 *
 * **It can be undone.** A stop with no way back is a trap - the only remedy
 * would be closing the window and running the launcher again, and somebody
 * will press it to see what it does. Killed is a state you can come back from.
 */

export function KillSwitch({
  services,
  reload,
}: {
  services: ServicesState | null;
  reload: () => void | Promise<void>;
}) {
  const [armed, setArmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [stopped, setStopped] = useState<number | null>(null);

  const running = services?.running ?? false;
  const killed = services?.killed ?? false;
  const workers = services?.workers;
  const inFlight = workers?.in_progress ?? 0;
  const queued = workers?.queued ?? 0;
  const ticking = services?.routines?.ticking ?? false;

  const act = async (action: 'kill' | 'start' | 'restart') => {
    setBusy(true);
    setError('');
    try {
      const result = await post<ServicesState>('services', { action });
      setStopped(action === 'kill' ? (result.cancelled ?? 0) : null);
      setArmed(false);
      await reload();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card className="border-rose-400/20">
      <CardTitle
        icon={OctagonX}
        title="Kill switch"
        hint="Stops the background workers and the routine ticker. The call you are on is not affected."
        right={
          running ? (
            <Pill tone="good" pulse>
              services running
            </Pill>
          ) : killed ? (
            <Pill tone="bad">stopped by you</Pill>
          ) : (
            <Pill tone="neutral">not running</Pill>
          )
        }
      />

      <div className="space-y-2">
        <Row>
          <span className="text-sm text-white/70">Background worker</span>
          <Pill tone={workers?.running ? 'good' : 'neutral'}>
            {workers?.running ? (workers.paused ? 'paused' : 'running') : 'stopped'}
          </Pill>
        </Row>
        <Row>
          <span className="text-sm text-white/70">Routine ticker</span>
          <Pill tone={ticking ? 'good' : 'neutral'}>{ticking ? 'ticking' : 'stopped'}</Pill>
        </Row>
        <Row>
          <span className="text-sm text-white/70">Jobs in flight</span>
          <span className="text-sm text-white/90">
            {inFlight} running, {queued} queued
          </span>
        </Row>
      </div>

      {services && !services.owns_services && services.owner && (
        <p className="mt-3 text-xs leading-relaxed text-amber-300">
          These services are running in a different JARVIS process ({services.owner}). This button
          still reaches them, because the dashboard talks to whichever process holds the control
          port, and that is the same process that runs them.
        </p>
      )}

      {services?.error && (
        <p className="mt-3 text-xs leading-relaxed text-amber-300">
          Something is not right with the background side: {services.error}
        </p>
      )}

      {error && <p className="mt-3 text-xs leading-relaxed text-rose-300">{error}</p>}

      {stopped !== null && !armed && (
        <p className="mt-3 text-xs leading-relaxed text-white/60">
          Stopped. {stopped} job{stopped === 1 ? '' : 's'} cancelled.
          {stopped > 0 && ' Anything that was mid-render finishes on its own; nothing new starts.'}
        </p>
      )}

      {armed ? (
        <div className="mt-4 rounded-xl bg-rose-500/10 p-4 ring-1 ring-rose-400/25 ring-inset">
          <p className="flex items-start gap-2 text-sm text-rose-100">
            <TriangleAlert className="mt-0.5 size-4 shrink-0" />
            <span>
              Stop the routine ticker and cancel {inFlight + queued} job
              {inFlight + queued === 1 ? '' : 's'}?
            </span>
          </p>
          <p className="mt-2 text-xs leading-relaxed text-rose-200/70">
            Scheduled routines will not fire until you start them again. Work already part-way
            through a render or a paid API call finishes on its own — it cannot be interrupted from
            outside — but nothing new will start after it.
          </p>
          <div className="mt-3 flex flex-wrap gap-2">
            <Action icon={OctagonX} tone="danger" busy={busy} onClick={() => void act('kill')}>
              Yes, stop everything
            </Action>
            <Action onClick={() => setArmed(false)} disabled={busy}>
              Cancel
            </Action>
          </div>
        </div>
      ) : (
        <div className="mt-4 flex flex-wrap gap-2">
          <Action
            icon={OctagonX}
            tone="danger"
            disabled={!running}
            onClick={() => setArmed(true)}
            title={
              running
                ? 'Stop the background workers and the routine ticker'
                : 'Nothing is running to stop'
            }
          >
            Stop everything
          </Action>
          {!running && (
            <Action
              icon={killed ? RotateCcw : Play}
              tone="primary"
              busy={busy}
              onClick={() => void act(killed ? 'restart' : 'start')}
            >
              {killed ? 'Start them again' : 'Start background services'}
            </Action>
          )}
        </div>
      )}
    </Card>
  );
}
