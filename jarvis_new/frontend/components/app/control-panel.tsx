'use client';

import { useState } from 'react';
import type { Permission } from '@/lib/jarvis';

/**
 * The settings tabs: the wake word, scheduled tasks, which AI thinks, what it
 * has learned, what it remembers, and your spoken commands.
 *
 * They are drawn by the console in `components/dashboard`, which owns the
 * layout and loads the state these take as a prop. Everything here reads and
 * writes through /api/control, which the Next server proxies to the local
 * control API with a token the browser never sees.
 */

type Provider = {
  name: string;
  label: string;
  where: string;
  free_tier: string;
  note: string;
  required: boolean;
  configured: boolean;
};
type Fact = { key: string; value: string; category?: string };
type Command = { id: string; trigger: string; response: string; kind?: string };
type Orders = { text: string; fingerprint: string; note?: string; error?: string };
type Step = { name?: string; plugin: string; params?: Record<string, unknown> };
type Mission = {
  id: string;
  name: string;
  description?: string;
  schedule?: string;
  enabled?: boolean;
  authorizations?: string[];
  steps: Step[];
  broken?: string;
};
type Authorisation = { id: string; label: string; sentence: string };
type BrowserProfile = { directory: string; name: string; email: string; active?: boolean };
/** Only the parts the panel actually reads are typed; the rest passes through. */
type Settings = {
  wake?: { phrase?: string; reply?: string };
  persona?: { instructions?: string; never?: string };
} & Record<string, unknown>;
export type State = {
  settings: Settings;
  providers: Provider[];
  missions: Mission[];
  facts: Fact[];
  commands: Command[];
  conversations: { id: number; title?: string; updated_at?: string }[];
  plugins: Record<string, string>;
  browser_profiles: BrowserProfile[];
  permissions: Permission[];
  thinking_models: ThinkingModel[];
  costs: Costs;
  lessons: Lesson[];
  orders: Orders;
  authorisations: Authorisation[];
};
type ThinkingModel = { id: string; label: string; free: boolean; detail?: string };
type Costs = { allow_paid: boolean; using: string; spent_30d_usd: number; note: string };
type Lesson = {
  id: number;
  trigger: string;
  said: string;
  steps: string;
  source: string;
  uses: number;
};

async function api(path: string, init?: RequestInit) {
  const res = await fetch(`/api/control/${path}`, {
    ...init,
    headers: { 'Content-Type': 'application/json' },
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body?.error ?? `Request failed (${res.status})`);
  return body;
}

/* -------------------------------------------------------------------------- */
/* Small shared pieces                                                         */
/* -------------------------------------------------------------------------- */

function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <label className="block space-y-1.5">
      <span className="text-foreground block text-sm font-medium">{label}</span>
      {hint && <span className="text-muted-foreground block text-xs">{hint}</span>}
      {children}
    </label>
  );
}

const inputClass =
  'w-full rounded-md border border-input bg-background px-3 py-2 text-sm ' +
  'outline-none focus:ring-2 focus:ring-ring/40 transition';

function Btn({
  children,
  onClick,
  tone = 'default',
  disabled,
  type = 'button',
}: {
  children: React.ReactNode;
  onClick?: () => void;
  tone?: 'default' | 'primary' | 'danger';
  disabled?: boolean;
  type?: 'button' | 'submit';
}) {
  const tones = {
    default: 'bg-secondary text-secondary-foreground hover:bg-secondary/80',
    primary: 'bg-primary text-primary-foreground hover:bg-primary/90',
    danger: 'bg-transparent text-destructive hover:bg-destructive/10',
  };
  return (
    <button
      type={type}
      onClick={onClick}
      disabled={disabled}
      className={`rounded-md px-3 py-1.5 text-sm font-medium transition disabled:opacity-40 ${tones[tone]}`}
    >
      {children}
    </button>
  );
}

function Empty({ children }: { children: React.ReactNode }) {
  return (
    <p className="text-muted-foreground border-border rounded-lg border border-dashed px-4 py-6 text-center text-sm">
      {children}
    </p>
  );
}

/* -------------------------------------------------------------------------- */
/* Voice                                                                       */
/* -------------------------------------------------------------------------- */

export function VoiceTab({ state, reload }: { state: State; reload: () => void }) {
  const wake = state.settings?.wake ?? {};
  const [phrase, setPhrase] = useState(wake.phrase ?? 'hey jarvis');
  const [reply, setReply] = useState(wake.reply ?? '');
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);

  const save = async () => {
    setSaving(true);
    try {
      await api('settings', {
        method: 'POST',
        body: JSON.stringify({ 'wake.phrase': phrase, 'wake.reply': reply }),
      });
      setSaved(true);
      setTimeout(() => setSaved(false), 2000);
      reload();
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="space-y-5">
      <Field
        label="Wake phrase"
        hint="Say this on its own and Jarvis answers immediately, without thinking about it first."
      >
        <input className={inputClass} value={phrase} onChange={(e) => setPhrase(e.target.value)} />
      </Field>
      <Field label="What it says back" hint="Keep it short — it's a greeting, not an introduction.">
        <input className={inputClass} value={reply} onChange={(e) => setReply(e.target.value)} />
      </Field>
      <div className="flex items-center gap-3">
        <Btn tone="primary" onClick={save} disabled={saving}>
          {saving ? 'Saving…' : 'Save'}
        </Btn>
        {saved && (
          <span className="text-xs text-emerald-500">Saved — restart the agent to apply.</span>
        )}
      </div>
      <p className="text-muted-foreground border-border border-t pt-4 text-xs">
        Changes to the wake phrase take effect the next time the agent starts. A live call keeps the
        instructions it began with.
      </p>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Tasks (missions)                                                            */
/* -------------------------------------------------------------------------- */

const SCHEDULE_PRESETS = [
  { label: 'Every night at 2am', cron: '0 2 * * *' },
  { label: 'Every weekday at 7am', cron: '0 7 * * 1-5' },
  { label: 'Every hour', cron: '0 * * * *' },
  { label: 'Every Monday at 9am', cron: '0 9 * * 1' },
  { label: 'Only when I ask', cron: '' },
];

function MissionEditor({
  state,
  mission,
  onDone,
}: {
  state: State;
  mission: Mission | null;
  onDone: () => void;
}) {
  const [name, setName] = useState(mission?.name ?? '');
  const [description, setDescription] = useState(mission?.description ?? '');
  const [schedule, setSchedule] = useState(mission?.schedule ?? '0 2 * * *');
  const [steps, setSteps] = useState<Step[]>(
    mission?.steps?.length ? mission.steps : [{ plugin: 'web_search', params: {} }]
  );
  const [allowed, setAllowed] = useState<string[]>(mission?.authorizations ?? []);
  const [provider, setProvider] = useState<string>(
    (mission?.steps?.find((x) => x.plugin === 'llm')?.params?.provider as string) ?? ''
  );
  const [error, setError] = useState('');
  const [saving, setSaving] = useState(false);

  const toggle = (sentence: string) =>
    setAllowed((a) => (a.includes(sentence) ? a.filter((s) => s !== sentence) : [...a, sentence]));

  const save = async () => {
    setError('');
    setSaving(true);
    try {
      const id =
        mission?.id ??
        name
          .toLowerCase()
          .replace(/[^a-z0-9]+/g, '-')
          .replace(/^-|-$/g, '')
          .slice(0, 40);
      if (!id) throw new Error('Give the task a name.');
      await api('missions', {
        method: 'POST',
        body: JSON.stringify({
          id,
          name: name || id,
          description,
          schedule,
          enabled: true,
          authorizations: allowed,
          // The chosen AI is attached to every thinking step, which is where
          // the model choice actually matters.
          steps: steps.map((s) => ({
            ...s,
            params:
              s.plugin === 'llm' && provider ? { ...(s.params ?? {}), provider } : (s.params ?? {}),
          })),
        }),
      });
      onDone();
    } catch (e) {
      setError(String(e instanceof Error ? e.message : e));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="border-border bg-card/50 space-y-4 rounded-lg border p-4">
      <Field label="What should it be called?">
        <input
          className={inputClass}
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="Morning news summary"
        />
      </Field>

      <Field label="What should it do?" hint="Plain words. This is what Jarvis is told to achieve.">
        <textarea
          className={`${inputClass} min-h-20`}
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          placeholder="Search for news about my industry and write me a short summary."
        />
      </Field>

      <Field label="When?">
        <div className="space-y-2">
          <div className="flex flex-wrap gap-1.5">
            {SCHEDULE_PRESETS.map((p) => (
              <button
                key={p.label}
                type="button"
                onClick={() => setSchedule(p.cron)}
                className={`rounded-full border px-2.5 py-1 text-xs transition ${
                  schedule === p.cron
                    ? 'border-primary bg-primary text-primary-foreground'
                    : 'border-border hover:bg-secondary'
                }`}
              >
                {p.label}
              </button>
            ))}
          </div>
          <input
            className={inputClass}
            value={schedule}
            onChange={(e) => setSchedule(e.target.value)}
            placeholder="0 2 * * *"
          />
          <span className="text-muted-foreground text-xs">
            Five fields: minute, hour, day, month, weekday. Leave empty to run it only when you ask.
          </span>
        </div>
      </Field>

      <Field
        label="Steps"
        hint="Each step runs in order. The result of one is available to the next."
      >
        <div className="space-y-2">
          {steps.map((step, i) => (
            <div key={i} className="flex gap-2">
              <select
                className={inputClass}
                value={step.plugin}
                onChange={(e) =>
                  setSteps((s) => s.map((x, j) => (j === i ? { ...x, plugin: e.target.value } : x)))
                }
              >
                {Object.entries(state.plugins).map(([id, label]) => (
                  <option key={id} value={id}>
                    {label}
                  </option>
                ))}
              </select>
              {steps.length > 1 && (
                <Btn tone="danger" onClick={() => setSteps((s) => s.filter((_, j) => j !== i))}>
                  Remove
                </Btn>
              )}
            </div>
          ))}
          <Btn onClick={() => setSteps((s) => [...s, { plugin: 'llm', params: {} }])}>
            Add a step
          </Btn>
        </div>
      </Field>

      <Field
        label="Which AI should think for it?"
        hint="Only matters for thinking steps. Leave on default unless you want a specific one."
      >
        <select
          className={inputClass}
          value={provider}
          onChange={(e) => setProvider(e.target.value)}
        >
          <option value="">Default</option>
          {state.providers
            .filter((p) => p.configured && p.name.endsWith('_API_KEY'))
            .map((p) => (
              <option key={p.name} value={p.name.replace('_API_KEY', '').toLowerCase()}>
                {p.label}
              </option>
            ))}
        </select>
        {!state.providers.some((p) => p.configured && p.name.endsWith('_API_KEY')) && (
          <span className="text-muted-foreground text-xs">
            No AI keys stored yet — add one under the AI tab and it will appear here.
          </span>
        )}
      </Field>

      <div className="border-border space-y-2 rounded-md border border-dashed p-3">
        <p className="text-sm font-medium">What is it allowed to do?</p>
        <p className="text-muted-foreground text-xs">
          Everything here is blocked unless you tick it. Nothing else can spend your money, post as
          you, or touch files outside your workspace — not even if it decides that would help.
        </p>
        <div className="space-y-1.5 pt-1">
          {state.authorisations.map((a) => (
            <label key={a.id} className="flex cursor-pointer items-start gap-2 text-sm">
              <input
                type="checkbox"
                className="mt-0.5"
                checked={allowed.includes(a.sentence)}
                onChange={() => toggle(a.sentence)}
              />
              <span>{a.label}</span>
            </label>
          ))}
        </div>
      </div>

      {error && <p className="text-destructive text-sm">{error}</p>}

      <div className="flex gap-2">
        <Btn tone="primary" onClick={save} disabled={saving}>
          {saving ? 'Saving…' : 'Save task'}
        </Btn>
        <Btn onClick={onDone}>Cancel</Btn>
      </div>
    </div>
  );
}

export function TasksTab({ state, reload }: { state: State; reload: () => void }) {
  const [editing, setEditing] = useState<Mission | null | 'new'>(null);

  const remove = async (id: string) => {
    await api(`missions/${encodeURIComponent(id)}`, { method: 'DELETE' });
    reload();
  };

  if (editing) {
    return (
      <MissionEditor
        state={state}
        mission={editing === 'new' ? null : editing}
        onDone={() => {
          setEditing(null);
          reload();
        }}
      />
    );
  }

  return (
    <div className="space-y-4">
      <p className="text-muted-foreground text-sm">
        Things Jarvis does on its own — while you&apos;re asleep, or out, or just busy. Each one
        carries its own permissions.
      </p>

      {state.missions.length === 0 && <Empty>No tasks yet.</Empty>}

      <div className="space-y-2">
        {state.missions.map((m) => (
          <div key={m.id} className="border-border bg-card/50 rounded-lg border p-3">
            <div className="flex items-start justify-between gap-3">
              <div className="min-w-0">
                <p className="truncate text-sm font-medium">{m.name}</p>
                <p className="text-muted-foreground truncate text-xs">
                  {m.schedule ? `Runs: ${m.schedule}` : 'Only when you ask'}
                  {m.steps?.length
                    ? ` · ${m.steps.length} step${m.steps.length > 1 ? 's' : ''}`
                    : ''}
                </p>
                {m.broken && (
                  <p className="text-destructive text-xs">Can&apos;t read this one: {m.broken}</p>
                )}
                {!!m.authorizations?.length && (
                  <p className="mt-1 text-xs text-amber-500">
                    Allowed: {m.authorizations.length} extra permission
                    {m.authorizations.length > 1 ? 's' : ''}
                  </p>
                )}
              </div>
              <div className="flex shrink-0 gap-1">
                <Btn onClick={() => setEditing(m)}>Edit</Btn>
                <Btn tone="danger" onClick={() => remove(m.id)}>
                  Delete
                </Btn>
              </div>
            </div>
          </div>
        ))}
      </div>

      <Btn tone="primary" onClick={() => setEditing('new')}>
        New task
      </Btn>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* AI providers                                                                */
/* -------------------------------------------------------------------------- */

export function AITab({ state, reload }: { state: State; reload: () => void }) {
  const [open, setOpen] = useState<string | null>(null);
  const [value, setValue] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const paidOn = Boolean(state.costs?.allow_paid);

  const save = async (name: string) => {
    setError('');
    setBusy(true);
    try {
      await api('providers', { method: 'POST', body: JSON.stringify({ name, value }) });
      setOpen(null);
      setValue('');
      reload();
    } catch (e) {
      setError(String(e instanceof Error ? e.message : e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-4">
      <p className="text-muted-foreground text-sm">
        Every AI this build can use. Paste a key to switch one on — they&apos;re encrypted in your
        vault, never in a file you might share. Nothing here spends money unless you say so below.
      </p>

      <div className="border-border bg-card/50 space-y-3 rounded-lg border p-3">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <p className="text-sm font-medium">Money</p>
            <p className="text-muted-foreground text-xs">
              Everything is free out of the box. Turn this on and Jarvis may use a paid model for
              the hard thinking — nothing else ever costs anything.
            </p>
          </div>
          <button
            type="button"
            role="switch"
            aria-checked={paidOn}
            onClick={async () => {
              await api('settings', {
                method: 'POST',
                body: JSON.stringify({ 'thinking.allow_paid': !paidOn }),
              });
              reload();
            }}
            aria-label="Allow paid AI"
            className={`mt-0.5 h-6 w-11 shrink-0 rounded-full transition ${
              paidOn ? 'bg-amber-500' : 'bg-secondary border-border border'
            }`}
          >
            <span
              className={`block h-5 w-5 rounded-full bg-white shadow transition-transform ${
                paidOn ? 'translate-x-5' : 'translate-x-0.5'
              }`}
            />
          </button>
        </div>
        <p className="text-muted-foreground text-xs">
          Right now: <span className="text-foreground">{state.costs?.using ?? 'free'}</span>
          {state.costs
            ? ` · spent in the last 30 days: $${state.costs.spent_30d_usd.toFixed(2)}`
            : ''}
        </p>
      </div>

      <div className="border-border bg-card/50 space-y-2 rounded-lg border p-3">
        <p className="text-sm font-medium">Which AI does the hard thinking?</p>
        <p className="text-muted-foreground text-xs">
          The voice stays fast whatever you pick. This is the one it hands difficult questions,
          plans and writing to.
        </p>
        <select
          className={inputClass}
          value={(state.settings?.thinking as { model?: string } | undefined)?.model ?? 'auto'}
          onChange={async (e) => {
            await api('settings', {
              method: 'POST',
              body: JSON.stringify({ 'thinking.model': e.target.value }),
            });
            reload();
          }}
        >
          {state.thinking_models?.map((m) => (
            <option key={m.id} value={m.id} disabled={!m.free && !paidOn}>
              {m.label}
              {!m.free && !paidOn ? ' — turn Money on first' : ''}
            </option>
          ))}
        </select>
        <p className="text-muted-foreground text-xs">
          {paidOn
            ? 'Paid models are unlocked. A free one is still used if the paid one is unreachable.'
            : 'Paid models are locked. Choosing one while this is off changes nothing — a free brain answers instead.'}
        </p>
      </div>

      <div className="space-y-2">
        {state.providers.map((p) => (
          <div key={p.name} className="border-border bg-card/50 rounded-lg border p-3">
            <div className="flex items-start justify-between gap-3">
              <div className="min-w-0">
                <p className="flex items-center gap-2 text-sm font-medium">
                  {p.label}
                  {p.configured ? (
                    <span className="rounded-full bg-emerald-500/15 px-2 py-0.5 text-[10px] font-semibold text-emerald-500">
                      ON
                    </span>
                  ) : (
                    <span className="text-muted-foreground bg-secondary rounded-full px-2 py-0.5 text-[10px] font-semibold">
                      OFF
                    </span>
                  )}
                  {p.required && (
                    <span className="text-muted-foreground text-[10px]">required</span>
                  )}
                </p>
                <p className="text-muted-foreground truncate text-xs">
                  {p.where}
                  {p.free_tier ? ` · ${p.free_tier}` : ''}
                </p>
              </div>
              <Btn
                onClick={() => {
                  setOpen(open === p.name ? null : p.name);
                  setValue('');
                  setError('');
                }}
              >
                {p.configured ? 'Replace' : 'Add key'}
              </Btn>
            </div>

            {open === p.name && (
              <div className="mt-3 space-y-2">
                <input
                  className={inputClass}
                  type="password"
                  value={value}
                  onChange={(e) => setValue(e.target.value)}
                  placeholder="Paste the key only — nothing else"
                  autoFocus
                />
                {error && <p className="text-destructive text-xs">{error}</p>}
                <div className="flex gap-2">
                  <Btn tone="primary" onClick={() => save(p.name)} disabled={busy || !value.trim()}>
                    {busy ? 'Saving…' : 'Save'}
                  </Btn>
                  <Btn onClick={() => setOpen(null)}>Cancel</Btn>
                </div>
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Learned — things you taught it how to do                                    */
/* -------------------------------------------------------------------------- */

export function LearnedTab({ state, reload }: { state: State; reload: () => void }) {
  const [trigger, setTrigger] = useState('');
  const [steps, setSteps] = useState('');
  const [error, setError] = useState('');

  const teach = async () => {
    setError('');
    try {
      await api('lessons', { method: 'POST', body: JSON.stringify({ trigger, steps }) });
      setTrigger('');
      setSteps('');
      reload();
    } catch (e) {
      setError(String(e instanceof Error ? e.message : e));
    }
  };

  const forget = async (t: string) => {
    await api('lessons/forget', { method: 'POST', body: JSON.stringify({ trigger: t }) });
    reload();
  };

  const lessons = state.lessons ?? [];

  return (
    <div className="space-y-4">
      <p className="text-muted-foreground text-sm">
        Jarvis gets better at the jobs you give it most. Teach it out loud — &ldquo;when I say put
        music on, open Spotify and press play&rdquo; — or write one here. Correct it and the old way
        is replaced, not kept.
      </p>

      <div className="border-border bg-card/50 space-y-2 rounded-lg border p-3">
        <p className="text-sm font-medium">Teach it something</p>
        <Field label="When I say" hint="Your words for the job, short.">
          <input
            className={inputClass}
            value={trigger}
            onChange={(e) => setTrigger(e.target.value)}
            placeholder="put music on"
          />
        </Field>
        <Field label="Do this" hint="The actual steps, naming sites and buttons.">
          <textarea
            className={`${inputClass} min-h-20`}
            value={steps}
            onChange={(e) => setSteps(e.target.value)}
            placeholder="open_url with site spotify, then click the play button"
          />
        </Field>
        {error && <p className="text-destructive text-xs">{error}</p>}
        <Btn tone="primary" onClick={teach}>
          Teach
        </Btn>
      </div>

      <div>
        <p className="mb-2 text-sm font-medium">
          What it has learned{lessons.length ? ` (${lessons.length})` : ''}
        </p>
        {lessons.length === 0 ? (
          <Empty>Nothing yet. Tell it how you want a job done and it&apos;ll appear here.</Empty>
        ) : (
          <div className="space-y-1.5">
            {lessons.map((l) => (
              <div
                key={l.id}
                className="border-border bg-card/50 flex items-start justify-between gap-3 rounded-md border px-3 py-2"
              >
                <div className="min-w-0">
                  <p className="flex items-center gap-2 text-sm font-medium break-words">
                    &ldquo;{l.said || l.trigger}&rdquo;
                    {l.source === 'watched' ? (
                      <span className="text-muted-foreground bg-secondary rounded-full px-2 py-0.5 text-[10px] font-semibold">
                        FROM WATCHING
                      </span>
                    ) : null}
                    {l.uses > 0 ? (
                      <span className="text-muted-foreground text-[10px]">used {l.uses}×</span>
                    ) : null}
                  </p>
                  <p className="text-muted-foreground text-xs break-words">{l.steps}</p>
                </div>
                <Btn tone="danger" onClick={() => forget(l.trigger)}>
                  Forget
                </Btn>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Memory                                                                      */
/* -------------------------------------------------------------------------- */

export function MemoryTab({ state, reload }: { state: State; reload: () => void }) {
  const [key, setKey] = useState('');
  const [value, setValue] = useState('');

  const add = async () => {
    if (!key.trim() || !value.trim()) return;
    await api('memory/facts', { method: 'POST', body: JSON.stringify({ key, value }) });
    setKey('');
    setValue('');
    reload();
  };

  const forget = async (k: string) => {
    await api(`memory/facts/${encodeURIComponent(k)}`, { method: 'DELETE' });
    reload();
  };

  return (
    <div className="space-y-5">
      <div>
        <p className="mb-2 text-sm font-medium">What Jarvis knows about you</p>
        <p className="text-muted-foreground mb-3 text-xs">
          These carry into every conversation. It adds things here itself as you talk — you can
          correct or delete any of them.
        </p>
        {state.facts.length === 0 ? (
          <Empty>Nothing yet. Tell it something about yourself and it&apos;ll remember.</Empty>
        ) : (
          <div className="space-y-1.5">
            {state.facts.map((f) => (
              <div
                key={f.key}
                className="border-border bg-card/50 flex items-start justify-between gap-3 rounded-md border px-3 py-2"
              >
                <div className="min-w-0">
                  <p className="text-xs font-semibold tracking-wide uppercase opacity-60">
                    {f.key}
                  </p>
                  <p className="text-sm break-words">{f.value}</p>
                </div>
                <Btn tone="danger" onClick={() => forget(f.key)}>
                  Forget
                </Btn>
              </div>
            ))}
          </div>
        )}
      </div>

      <div className="border-border space-y-2 border-t pt-4">
        <p className="text-sm font-medium">Tell it something directly</p>
        <div className="flex gap-2">
          <input
            className={inputClass}
            value={key}
            onChange={(e) => setKey(e.target.value)}
            placeholder="name"
          />
          <input
            className={inputClass}
            value={value}
            onChange={(e) => setValue(e.target.value)}
            placeholder="Jonathan"
          />
          <Btn tone="primary" onClick={add}>
            Add
          </Btn>
        </div>
      </div>

      <div className="border-border border-t pt-4">
        <p className="mb-2 text-sm font-medium">Recent conversations</p>
        {state.conversations.length === 0 ? (
          <Empty>No conversations recorded yet.</Empty>
        ) : (
          <ul className="text-muted-foreground space-y-1 text-xs">
            {state.conversations.map((c) => (
              <li key={c.id}>
                #{c.id} · {c.updated_at ?? ''} {c.title ? `· ${c.title}` : ''}
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Commands                                                                    */
/* -------------------------------------------------------------------------- */

const COMMAND_KINDS = [
  {
    id: 'reply',
    label: 'Say exactly this',
    hint: 'It repeats your words back, word for word. Nothing else.',
    trigger: 'good morning',
    response: 'Good morning, sir. Shall I run the briefing?',
  },
  {
    id: 'run',
    label: 'Do this',
    hint: 'A shorthand. Saying the phrase is the same as asking for the whole thing.',
    trigger: 'movie night',
    response: 'open Netflix, turn the volume up and minimise everything else',
  },
  {
    id: 'instruct',
    label: 'Always do this',
    hint: 'A standing rule, applied in every conversation. No trigger phrase needed.',
    trigger: '',
    response: 'Always answer in Hebrew unless I ask otherwise.',
  },
] as const;

export function CommandsTab({ state, reload }: { state: State; reload: () => void }) {
  const [kind, setKind] = useState<string>('reply');
  const [trigger, setTrigger] = useState('');
  const [response, setResponse] = useState('');
  const [error, setError] = useState('');

  const shape = COMMAND_KINDS.find((k) => k.id === kind) ?? COMMAND_KINDS[0];
  const needsTrigger = kind !== 'instruct';

  const add = async () => {
    setError('');
    if (!response.trim() || (needsTrigger && !trigger.trim())) return;
    try {
      await api('commands', {
        method: 'POST',
        body: JSON.stringify({ trigger, response, kind }),
      });
      setTrigger('');
      setResponse('');
      reload();
    } catch (e) {
      setError(String(e instanceof Error ? e.message : e));
    }
  };

  const remove = async (id: string) => {
    await api(`commands/${encodeURIComponent(id)}`, { method: 'DELETE' });
    reload();
  };

  return (
    <div className="space-y-5">
      <p className="text-muted-foreground text-sm">
        Your own commands. Every one of them is put in front of Jarvis at the start of every
        conversation, above everything else it was told — so they are followed, not weighed up.
      </p>

      {state.commands.length === 0 ? (
        <Empty>No commands yet.</Empty>
      ) : (
        <div className="space-y-1.5">
          {state.commands.map((c) => (
            <div
              key={c.id}
              className="border-border bg-card/50 flex items-start justify-between gap-3 rounded-md border px-3 py-2"
            >
              <div className="min-w-0 text-sm">
                <p className="truncate font-medium">
                  {c.trigger ? `“${c.trigger}”` : 'Always'}
                  <span className="text-muted-foreground bg-secondary ml-2 rounded-full px-2 py-0.5 text-[10px] font-semibold">
                    {
                      (COMMAND_KINDS.find((k) => k.id === (c.kind ?? 'reply')) ?? COMMAND_KINDS[0])
                        .label
                    }
                  </span>
                </p>
                <p className="text-muted-foreground truncate">→ “{c.response}”</p>
              </div>
              <Btn tone="danger" onClick={() => remove(c.id)}>
                Delete
              </Btn>
            </div>
          ))}
        </div>
      )}

      <div className="border-border space-y-3 border-t pt-4">
        <Field
          label="What kind of command?"
          hint="This is the part that used to go wrong: an instruction saved as a thing to say gets recited instead of done."
        >
          <div className="flex flex-wrap gap-1.5">
            {COMMAND_KINDS.map((k) => (
              <button
                key={k.id}
                type="button"
                onClick={() => setKind(k.id)}
                className={`rounded-md px-3 py-1.5 text-sm font-medium transition ${
                  kind === k.id
                    ? 'bg-primary text-primary-foreground'
                    : 'bg-secondary text-secondary-foreground hover:bg-secondary/80'
                }`}
              >
                {k.label}
              </button>
            ))}
          </div>
        </Field>
        <p className="text-muted-foreground text-xs">{shape.hint}</p>

        {needsTrigger && (
          <Field label="When I say">
            <input
              className={inputClass}
              value={trigger}
              onChange={(e) => setTrigger(e.target.value)}
              placeholder={shape.trigger}
            />
          </Field>
        )}
        <Field label={kind === 'reply' ? 'Say back, exactly' : 'Then'}>
          <textarea
            className={`${inputClass} min-h-16`}
            value={response}
            onChange={(e) => setResponse(e.target.value)}
            placeholder={shape.response}
          />
        </Field>
        {error && <p className="text-destructive text-xs">{error}</p>}
        <Btn tone="primary" onClick={add}>
          Add command
        </Btn>
      </div>
    </div>
  );
}
