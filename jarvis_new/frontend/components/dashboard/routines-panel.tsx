'use client';

import { useState } from 'react';
import {
  AlarmClock,
  BookOpen,
  CalendarClock,
  Chrome,
  Eye,
  EyeOff,
  History,
  Pencil,
  Plus,
  ScrollText,
  Sunrise,
  Trash2,
  Volume2,
  Zap,
} from 'lucide-react';
import { type Routine, type RoutinesSnapshot, ago, api, post, when } from '@/lib/jarvis';
import {
  Action,
  Card,
  CardTitle,
  Empty,
  Flash,
  INPUT,
  Pill,
  Row,
  Stat,
  Switch,
  useFlash,
} from './kit';

/**
 * Standing instructions and the things that happen without being asked.
 *
 * Both live here because they answer the same question - "what does Jarvis do
 * when I haven't said anything?" - and splitting them across two screens is
 * how you end up with a briefing that fires at seven every morning quoting
 * instructions you changed a month ago.
 */

type Profile = { directory: string; name: string; email: string; active?: boolean };
type Orders = { text: string; fingerprint: string; note?: string; error?: string };

const STATUS_TONE: Record<string, 'good' | 'bad' | 'live' | 'neutral'> = {
  done: 'good',
  failed: 'bad',
  running: 'live',
  skipped: 'neutral',
};

export function RoutinesPanel({
  instructions,
  never,
  orders,
  profiles,
  snapshot,
  reloadState,
  reloadRoutines,
}: {
  instructions: string;
  never: string;
  orders?: Orders;
  profiles: Profile[];
  snapshot: RoutinesSnapshot | null;
  reloadState: () => void | Promise<void>;
  reloadRoutines: () => void | Promise<void>;
}) {
  return (
    <div className="space-y-5">
      <RoutinesSection snapshot={snapshot} reload={reloadRoutines} />
      <StandingInstructions
        instructions={instructions}
        never={never}
        orders={orders}
        profiles={profiles}
        reload={reloadState}
      />
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Routines                                                                    */
/* -------------------------------------------------------------------------- */

function RoutinesSection({
  snapshot,
  reload,
}: {
  snapshot: RoutinesSnapshot | null;
  reload: () => void | Promise<void>;
}) {
  const [editing, setEditing] = useState<Routine | 'new' | null>(null);
  const [busy, setBusy] = useState('');
  const [flash, setFlash] = useFlash();
  const [failed, setFailed] = useState('');

  if (!snapshot) {
    return (
      <Card>
        <Empty>Reading the routines…</Empty>
      </Card>
    );
  }

  const { summary } = snapshot;
  const morning = findMorningBriefing(snapshot.routines);

  const guard = async (key: string, work: () => Promise<unknown>, note: string) => {
    setBusy(key);
    setFailed('');
    try {
      await work();
      setFlash(note);
      await reload();
    } catch (e) {
      setFailed(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy('');
    }
  };

  const runNow = (routine: Routine) =>
    guard(
      `run:${routine.id}`,
      () => post(`routines/${encodeURIComponent(routine.id)}/run`, {}),
      `${routine.name} is running in the background. Its result appears below when it finishes.`
    );

  const setEnabled = (routine: Routine, enabled: boolean) =>
    guard(
      `on:${routine.id}`,
      () => post(`routines/${encodeURIComponent(routine.id)}/enabled`, { enabled }),
      enabled ? `${routine.name} is armed.` : `${routine.name} will not fire.`
    );

  const remove = (routine: Routine) =>
    guard(
      `rm:${routine.id}`,
      () => api(`routines/${encodeURIComponent(routine.id)}`, { method: 'DELETE' }),
      `${routine.name} deleted.`
    );

  return (
    <>
      <Card>
        <CardTitle
          icon={CalendarClock}
          title="Daily routines"
          hint="Things Jarvis does on his own, on a schedule. They run on the background worker, never on the conversation."
          right={
            <Pill tone={summary.ticking ? 'live' : 'warn'} pulse={summary.ticking}>
              {summary.ticking ? 'ticking' : 'not ticking'}
            </Pill>
          }
        />
        <div className="grid grid-cols-2 gap-3 @xl:grid-cols-4">
          <Stat
            label="Armed"
            value={summary.armed ?? 0}
            tone={summary.armed ? 'live' : 'neutral'}
          />
          <Stat label="Switched on" value={`${summary.enabled ?? 0}/${summary.total ?? 0}`} />
          <Stat
            label="Failing"
            value={summary.failing?.length ?? 0}
            tone={summary.failing?.length ? 'bad' : 'neutral'}
          />
          <Stat
            label="Next"
            value={<span className="text-sm">{summary.next ? when(summary.next.at) : '—'}</span>}
          />
        </div>
        {summary.next && (
          <p className="mt-2.5 text-xs text-white/50">
            Next up: <span className="text-white/80">{summary.next.name}</span>,{' '}
            {when(summary.next.at)}.
          </p>
        )}
        {!summary.ticking && (
          <p className="mt-2.5 text-xs text-amber-300">
            The scheduler is not running, so nothing will fire on its own. It starts with the agent
            — run butler-agent.bat.
          </p>
        )}

        <div className="mt-4 flex flex-wrap items-center gap-2">
          {morning && (
            <Action
              icon={Sunrise}
              tone="primary"
              busy={busy === `run:${morning.id}`}
              onClick={() => void runNow(morning)}
            >
              Run morning briefing now
            </Action>
          )}
          <Action icon={Plus} onClick={() => setEditing('new')}>
            New routine
          </Action>
        </div>
        <div className="mt-2 space-y-1">
          <Flash>{flash}</Flash>
          <Flash bad>{failed}</Flash>
          {snapshot.error && <Flash bad>{snapshot.error}</Flash>}
        </div>
      </Card>

      {editing && (
        <RoutineEditor
          routine={editing === 'new' ? null : editing}
          actions={snapshot.actions}
          onDone={async (message) => {
            setEditing(null);
            if (message) setFlash(message);
            await reload();
          }}
        />
      )}

      <Card>
        <CardTitle icon={AlarmClock} title="Standing routines" />
        <div className="space-y-2.5">
          {snapshot.routines.map((routine) => (
            <Row
              key={routine.id}
              className={routine.enabled ? 'border-cyan-300/20 bg-cyan-400/[0.04]' : undefined}
            >
              <div className="min-w-0 flex-1">
                <p className="flex flex-wrap items-center gap-2 text-sm font-medium text-white">
                  {routine.name}
                  {routine.action_label && <Pill>{routine.action_label}</Pill>}
                  {routine.speak && (
                    <Pill>
                      <Volume2 className="size-3" />
                      spoken
                    </Pill>
                  )}
                  {routine.last_status && (
                    <Pill tone={STATUS_TONE[routine.last_status] ?? 'neutral'}>
                      {routine.last_status}
                    </Pill>
                  )}
                </p>
                <p className="mt-1 text-xs text-white/50">
                  {routine.schedule_in_words || routine.schedule}
                  {routine.enabled && routine.next_run_at && ` · next ${when(routine.next_run_at)}`}
                  {routine.last_run_at && ` · last ran ${ago(routine.last_run_at)}`}
                </p>
                {routine.instruction && (
                  <p className="mt-1 truncate text-[11px] text-white/40">“{routine.instruction}”</p>
                )}
                {routine.last_error && (
                  <p className="mt-1 text-[11px] text-rose-300">{routine.last_error}</p>
                )}
                <div className="mt-2 flex flex-wrap gap-1.5">
                  <Action
                    icon={Zap}
                    className="px-2.5 py-1 text-xs"
                    busy={busy === `run:${routine.id}`}
                    onClick={() => void runNow(routine)}
                  >
                    Run now
                  </Action>
                  <Action
                    icon={Pencil}
                    className="px-2.5 py-1 text-xs"
                    onClick={() => setEditing(routine)}
                  >
                    Edit
                  </Action>
                  <Action
                    icon={Trash2}
                    tone="danger"
                    className="px-2.5 py-1 text-xs"
                    busy={busy === `rm:${routine.id}`}
                    onClick={() => void remove(routine)}
                  >
                    Delete
                  </Action>
                </div>
              </div>
              <Switch
                on={routine.enabled}
                busy={busy === `on:${routine.id}`}
                label={`${routine.name} enabled`}
                onChange={(next) => void setEnabled(routine, next)}
              />
            </Row>
          ))}
          {!snapshot.routines.length && (
            <Empty>No routines yet. A good first one is a morning briefing at 07:30.</Empty>
          )}
        </div>
      </Card>

      <Card>
        <CardTitle
          icon={History}
          title="Recent runs"
          hint="What each routine produced, newest first. This is the same text Jarvis reads back to you."
        />
        <div className="space-y-2">
          {snapshot.recent.map((run) => (
            <Row key={run.id}>
              <div className="min-w-0 flex-1">
                <p className="text-sm text-white/90">
                  {snapshot.routines.find((r) => r.id === run.routine_id)?.name ?? run.routine_id}
                </p>
                <p className="mt-1 text-[11px] text-white/45">
                  {run.trigger === 'manual' ? 'run by hand' : 'on schedule'}
                  {run.started_at && ` · ${ago(run.started_at)}`}
                </p>
                {run.output && (
                  <p className="mt-1.5 line-clamp-3 text-xs leading-relaxed text-white/60">
                    {run.output}
                  </p>
                )}
                {run.error && <p className="mt-1 text-[11px] text-rose-300">{run.error}</p>}
              </div>
              <Pill tone={STATUS_TONE[run.status] ?? 'neutral'} pulse={run.status === 'running'}>
                {run.status}
              </Pill>
            </Row>
          ))}
          {!snapshot.recent.length && <Empty>Nothing has run yet.</Empty>}
        </div>
      </Card>
    </>
  );
}

/** The routine a "run the morning briefing" button should fire. */
function findMorningBriefing(routines: Routine[]): Routine | undefined {
  return (
    routines.find((routine) => routine.id === 'good-morning') ??
    routines.find((routine) => routine.action === 'briefing')
  );
}

function RoutineEditor({
  routine,
  actions,
  onDone,
}: {
  routine: Routine | null;
  actions: RoutinesSnapshot['actions'];
  onDone: (message?: string) => void | Promise<void>;
}) {
  const [name, setName] = useState(routine?.name ?? '');
  const [action, setAction] = useState(routine?.action ?? actions[0]?.key ?? 'briefing');
  const [schedule, setSchedule] = useState(routine?.schedule ?? '07:30');
  const [instruction, setInstruction] = useState(routine?.instruction ?? '');
  const [speak, setSpeak] = useState(routine?.speak ?? true);
  const [catchUp, setCatchUp] = useState(routine?.catch_up ?? true);
  const [saving, setSaving] = useState(false);
  const [failed, setFailed] = useState('');

  const chosen = actions.find((entry) => entry.key === action);

  const save = async () => {
    setSaving(true);
    setFailed('');
    try {
      await post('routines', {
        ...(routine?.id ? { id: routine.id } : {}),
        name: name.trim(),
        action,
        schedule: schedule.trim(),
        instruction,
        speak,
        catch_up: catchUp,
        enabled: routine?.enabled ?? true,
      });
      await onDone(`${name.trim() || 'The routine'} saved.`);
    } catch (e) {
      setFailed(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  };

  return (
    <Card className="border-cyan-300/25 bg-cyan-400/[0.05]">
      <CardTitle
        icon={routine ? Pencil : Plus}
        title={routine ? `Edit ${routine.name}` : 'New routine'}
        hint="A name, a time, and one thing to do. The schedule takes 07:30, @daily, or five-field cron."
      />
      <div className="grid gap-3 @xl:grid-cols-2">
        <label className="block space-y-1.5">
          <span className="text-xs font-medium text-white/70">Name</span>
          <input
            className={INPUT}
            value={name}
            placeholder="Good morning briefing"
            onChange={(event) => setName(event.target.value)}
          />
        </label>
        <label className="block space-y-1.5">
          <span className="text-xs font-medium text-white/70">When</span>
          <input
            className={INPUT}
            value={schedule}
            placeholder="07:30"
            onChange={(event) => setSchedule(event.target.value)}
          />
        </label>
        <label className="block space-y-1.5 @xl:col-span-2">
          <span className="text-xs font-medium text-white/70">What it does</span>
          <select
            className={INPUT}
            value={action}
            onChange={(event) => setAction(event.target.value)}
          >
            {actions.map((entry) => (
              <option key={entry.key} value={entry.key} className="bg-slate-900">
                {entry.label}
              </option>
            ))}
          </select>
          {chosen && (
            <span className="block text-[11px] text-white/45">
              {chosen.detail} <span className="text-white/35">Needs: {chosen.needs}</span>
            </span>
          )}
        </label>
        <label className="block space-y-1.5 @xl:col-span-2">
          <span className="text-xs font-medium text-white/70">
            Anything it should say or do specifically
          </span>
          <textarea
            className={`${INPUT} min-h-24`}
            value={instruction}
            placeholder="Mention the gym at 6, and what the content engine wrote overnight."
            onChange={(event) => setInstruction(event.target.value)}
          />
        </label>
      </div>

      <div className="mt-3 flex flex-wrap gap-4">
        <label className="flex items-center gap-2 text-xs text-white/70">
          <Switch on={speak} onChange={setSpeak} label="Read it out" />
          Read it out next time we speak
        </label>
        <label className="flex items-center gap-2 text-xs text-white/70">
          <Switch on={catchUp} onChange={setCatchUp} label="Catch up" />
          Still run it if the machine was off
        </label>
      </div>

      <div className="mt-4 flex flex-wrap items-center gap-2">
        <Action tone="primary" busy={saving} disabled={!name.trim()} onClick={() => void save()}>
          Save routine
        </Action>
        <Action onClick={() => void onDone()}>Cancel</Action>
        <Flash bad>{failed}</Flash>
      </div>
    </Card>
  );
}

/* -------------------------------------------------------------------------- */
/* Standing instructions                                                       */
/* -------------------------------------------------------------------------- */

function StandingInstructions({
  instructions,
  never,
  orders,
  profiles,
  reload,
}: {
  instructions: string;
  never: string;
  orders?: Orders;
  profiles: Profile[];
  reload: () => void | Promise<void>;
}) {
  const [always, setAlways] = useState(instructions);
  const [limits, setLimits] = useState(never);
  const [saving, setSaving] = useState(false);
  const [showOrders, setShowOrders] = useState(false);
  const [flash, setFlash] = useFlash();
  const [failed, setFailed] = useState('');

  const dirty = always !== instructions || limits !== never;

  const save = async () => {
    setSaving(true);
    setFailed('');
    try {
      await post('settings', { 'persona.instructions': always, 'persona.never': limits });
      setFlash('Saved. A conversation already running picks this up within a few seconds.');
      await reload();
    } catch (e) {
      setFailed(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  };

  return (
    <>
      <Card>
        <CardTitle
          icon={ScrollText}
          title="Standing instructions"
          hint="Written in your words and read at the start of every conversation. This is where you tell him about you, how to talk to you, and what he may do without asking."
          right={dirty ? <Pill tone="warn">unsaved</Pill> : undefined}
        />
        <div className="space-y-4">
          <label className="block space-y-1.5">
            <span className="text-xs font-medium text-white/70">Always</span>
            <textarea
              className={`${INPUT} min-h-36`}
              value={always}
              onChange={(event) => setAlways(event.target.value)}
              placeholder={
                'My business is video production.\n' +
                'Keep answers short unless I ask for detail.\n' +
                'You can open apps and websites without asking.'
              }
            />
          </label>
          <label className="block space-y-1.5">
            <span className="text-xs font-medium text-white/70">Never</span>
            <span className="block text-[11px] text-white/45">
              Absolute limits. He refuses rather than looking for a way around them.
            </span>
            <textarea
              className={`${INPUT} min-h-28`}
              value={limits}
              onChange={(event) => setLimits(event.target.value)}
              placeholder={
                'Never post anything publicly without asking me first.\nNever spend money.'
              }
            />
          </label>
        </div>
        <div className="mt-4 flex flex-wrap items-center gap-3">
          <Action tone="primary" busy={saving} disabled={!dirty} onClick={() => void save()}>
            Save instructions
          </Action>
          <Flash>{flash}</Flash>
          <Flash bad>{failed}</Flash>
        </div>
      </Card>

      <Card>
        <CardTitle
          icon={BookOpen}
          title="What he is actually being told"
          hint={orders?.note ?? 'Whatever you write above appears here, word for word.'}
          right={
            <Action
              icon={showOrders ? EyeOff : Eye}
              disabled={!orders?.text}
              onClick={() => setShowOrders((was) => !was)}
            >
              {showOrders ? 'Hide' : 'Show'}
            </Action>
          }
        />
        {showOrders && orders?.text ? (
          <pre className="max-h-72 overflow-auto rounded-xl border border-white/10 bg-black/30 p-3 font-mono text-[11px] leading-relaxed whitespace-pre-wrap text-white/70">
            {orders.text}
          </pre>
        ) : (
          !orders?.text && <Empty>Nothing yet — write an instruction above.</Empty>
        )}
      </Card>

      <Card>
        <CardTitle
          icon={Chrome}
          title="Which browser is yours"
          hint={
            'When you say "open my Netflix", this is the Chrome profile he opens — the one you are actually signed into.'
          }
        />
        {profiles.length ? (
          <select
            className={INPUT}
            defaultValue={profiles.find((profile) => profile.active)?.directory ?? ''}
            onChange={async (event) => {
              await post('settings', { 'browser.profile': event.target.value });
              await reload();
            }}
          >
            {profiles.map((profile) => (
              <option key={profile.directory} value={profile.directory} className="bg-slate-900">
                {profile.name}
                {profile.email ? ` — ${profile.email}` : ' — not signed in'}
              </option>
            ))}
          </select>
        ) : (
          <Empty>No Chrome profiles found — Jarvis will use Chrome&apos;s default.</Empty>
        )}
      </Card>
    </>
  );
}
