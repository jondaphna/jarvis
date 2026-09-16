'use client';

import { useCallback, useEffect, useState } from 'react';

/**
 * Everything about Jarvis you can change without touching a file: what it
 * knows, what it's allowed to do while you're away, which AI it uses, and
 * what it says when you greet it.
 *
 * Reads and writes through /api/control, which the Next server proxies to the
 * local control API with a token the browser never sees.
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
type Command = { id: string; trigger: string; response: string };
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
type Permission = {
  key: string;
  label: string;
  detail: string;
  tools: string[];
  enabled: boolean;
  risk: 'low' | 'medium' | 'high';
};
/** Only the parts the panel actually reads are typed; the rest passes through. */
type Settings = {
  wake?: { phrase?: string; reply?: string };
  persona?: { instructions?: string; never?: string };
} & Record<string, unknown>;
type State = {
  settings: Settings;
  providers: Provider[];
  missions: Mission[];
  facts: Fact[];
  commands: Command[];
  conversations: { id: number; title?: string; updated_at?: string }[];
  plugins: Record<string, string>;
  browser_profiles: BrowserProfile[];
  permissions: Permission[];
  thinking_models: { id: string; label: string }[];
  authorisations: Authorisation[];
};

const TABS = ['Rules', 'Permissions', 'Voice', 'Tasks', 'AI', 'Memory', 'Commands'] as const;
type Tab = (typeof TABS)[number];

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
/* Rules — what it should and shouldn't do                                     */
/* -------------------------------------------------------------------------- */

function RulesTab({ state, reload }: { state: State; reload: () => void }) {
  const persona = state.settings?.persona ?? {};
  const [instructions, setInstructions] = useState(persona.instructions ?? '');
  const [never, setNever] = useState(persona.never ?? '');
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);

  const save = async () => {
    setSaving(true);
    try {
      await api('settings', {
        method: 'POST',
        body: JSON.stringify({
          'persona.instructions': instructions,
          'persona.never': never,
        }),
      });
      setSaved(true);
      setTimeout(() => setSaved(false), 2500);
      reload();
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="space-y-5">
      <p className="text-muted-foreground text-sm">
        Written in your words, and read at the start of every conversation. This is where you tell
        it about you, how to talk to you, and what it may do without asking.
      </p>

      <Field
        label="Standing instructions"
        hint="Anything it should always know or always do. One thing per line."
      >
        <textarea
          className={`${inputClass} min-h-36`}
          value={instructions}
          onChange={(e) => setInstructions(e.target.value)}
          placeholder={
            'My business is video production.\n' +
            'Keep answers short unless I ask for detail.\n' +
            'You can open apps and websites without asking.\n' +
            'Always check my calendar before suggesting a time.'
          }
        />
      </Field>

      <Field
        label="Never do these"
        hint="Absolute limits. It will refuse rather than look for a way around them."
      >
        <textarea
          className={`${inputClass} min-h-28`}
          value={never}
          onChange={(e) => setNever(e.target.value)}
          placeholder={
            'Never post anything publicly without asking me first.\n' +
            'Never spend money.\n' +
            'Never delete files.\n' +
            'Never send email on my behalf.'
          }
        />
      </Field>

      <div className="flex items-center gap-3">
        <Btn tone="primary" onClick={save} disabled={saving}>
          {saving ? 'Saving…' : 'Save'}
        </Btn>
        {saved && (
          <span className="text-xs text-emerald-500">Saved — restart the agent to apply.</span>
        )}
      </div>

      <div className="border-border space-y-2 border-t pt-5">
        <p className="text-sm font-medium">Which browser is yours?</p>
        <p className="text-muted-foreground text-xs">
          When you say &quot;open my Netflix&quot;, this is the Chrome profile it opens — the one
          you&apos;re actually signed into.
        </p>
        {state.browser_profiles?.length ? (
          <select
            className={inputClass}
            defaultValue={state.browser_profiles.find((p) => p.active)?.directory ?? ''}
            onChange={async (e) => {
              await api('settings', {
                method: 'POST',
                body: JSON.stringify({ 'browser.profile': e.target.value }),
              });
              reload();
            }}
          >
            {state.browser_profiles.map((p) => (
              <option key={p.directory} value={p.directory}>
                {p.name}
                {p.email ? ` — ${p.email}` : ' — not signed in'}
              </option>
            ))}
          </select>
        ) : (
          <p className="text-muted-foreground text-xs">
            No Chrome profiles found — Jarvis will use Chrome&apos;s default.
          </p>
        )}
      </div>

      <p className="text-muted-foreground border-border border-t pt-4 text-xs">
        These apply to conversations. A scheduled task carries its own separate permissions, set on
        the task itself under Tasks.
      </p>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Permissions — what it may and may not do                                    */
/* -------------------------------------------------------------------------- */

function PermissionsTab({ state, reload }: { state: State; reload: () => void }) {
  const [busy, setBusy] = useState<string | null>(null);

  const toggle = async (key: string, next: boolean) => {
    setBusy(key);
    try {
      await api('settings', {
        method: 'POST',
        body: JSON.stringify({ [`permissions.${key}`]: next }),
      });
      reload();
    } finally {
      setBusy(null);
    }
  };

  const risks: Record<Permission['risk'], string> = {
    low: '',
    medium: 'bg-amber-500/15 text-amber-600 dark:text-amber-400',
    high: 'bg-destructive/15 text-destructive',
  };

  return (
    <div className="space-y-4">
      <p className="text-muted-foreground text-sm">
        Switch something off and the tools behind it are taken away entirely — Jarvis isn&apos;t
        asked to behave, it simply has no way to do it. This takes effect when the next conversation
        starts; a call already running keeps what it began with.
      </p>

      <div className="space-y-2">
        {state.permissions?.map((p) => (
          <div
            key={p.key}
            className="border-border bg-card/50 flex items-start justify-between gap-4 rounded-lg border p-3"
          >
            <div className="min-w-0">
              <p className="flex items-center gap-2 text-sm font-medium">
                {p.label}
                {p.risk !== 'low' && (
                  <span
                    className={`rounded-full px-2 py-0.5 text-[10px] font-semibold ${risks[p.risk]}`}
                  >
                    {p.risk === 'high' ? 'careful' : 'powerful'}
                  </span>
                )}
              </p>
              <p className="text-muted-foreground text-xs">{p.detail}</p>
              <p className="text-muted-foreground/70 mt-1 text-[11px]">
                {p.tools.length} tool{p.tools.length === 1 ? '' : 's'}
              </p>
            </div>

            <button
              role="switch"
              aria-checked={p.enabled}
              aria-label={p.label}
              disabled={busy === p.key}
              onClick={() => toggle(p.key, !p.enabled)}
              className={`mt-0.5 h-6 w-11 shrink-0 rounded-full transition disabled:opacity-50 ${
                p.enabled ? 'bg-primary' : 'bg-secondary border-border border'
              }`}
            >
              <span
                className={`block h-5 w-5 rounded-full bg-white shadow transition-transform ${
                  p.enabled ? 'translate-x-5' : 'translate-x-0.5'
                }`}
              />
            </button>
          </div>
        ))}
      </div>

      <p className="text-muted-foreground border-border border-t pt-4 text-xs">
        Changes apply the next time the agent starts. A call already running keeps the tools it
        began with.
      </p>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Voice                                                                       */
/* -------------------------------------------------------------------------- */

function VoiceTab({ state, reload }: { state: State; reload: () => void }) {
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

function TasksTab({ state, reload }: { state: State; reload: () => void }) {
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

function AITab({ state, reload }: { state: State; reload: () => void }) {
  const [open, setOpen] = useState<string | null>(null);
  const [value, setValue] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

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
        vault, never in a file you might share.
      </p>

      <div className="border-border bg-card/50 space-y-2 rounded-lg border p-3">
        <p className="text-sm font-medium">Which AI does the hard thinking?</p>
        <p className="text-muted-foreground text-xs">
          The voice stays fast whatever you pick. This is the one it hands difficult questions,
          plans and writing to.
        </p>
        <select
          className={inputClass}
          defaultValue={
            (state.settings?.thinking as { model?: string } | undefined)?.model ?? 'claude-opus-5'
          }
          onChange={async (e) => {
            await api('settings', {
              method: 'POST',
              body: JSON.stringify({ 'thinking.model': e.target.value }),
            });
            reload();
          }}
        >
          {state.thinking_models?.map((m) => (
            <option key={m.id} value={m.id}>
              {m.label}
            </option>
          ))}
        </select>
        <p className="text-muted-foreground text-xs">
          Needs a Claude key below. Without one it falls back to answering off the cuff.
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
/* Memory                                                                      */
/* -------------------------------------------------------------------------- */

function MemoryTab({ state, reload }: { state: State; reload: () => void }) {
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

function CommandsTab({ state, reload }: { state: State; reload: () => void }) {
  const [trigger, setTrigger] = useState('');
  const [response, setResponse] = useState('');

  const add = async () => {
    if (!trigger.trim() || !response.trim()) return;
    await api('commands', { method: 'POST', body: JSON.stringify({ trigger, response }) });
    setTrigger('');
    setResponse('');
    reload();
  };

  const remove = async (id: string) => {
    await api(`commands/${encodeURIComponent(id)}`, { method: 'DELETE' });
    reload();
  };

  return (
    <div className="space-y-5">
      <p className="text-muted-foreground text-sm">
        Say the words on the left, get exactly the words on the right. No thinking, no variation.
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
                <p className="truncate font-medium">“{c.trigger}”</p>
                <p className="text-muted-foreground truncate">→ “{c.response}”</p>
              </div>
              <Btn tone="danger" onClick={() => remove(c.id)}>
                Delete
              </Btn>
            </div>
          ))}
        </div>
      )}

      <div className="border-border space-y-2 border-t pt-4">
        <Field label="When I say">
          <input
            className={inputClass}
            value={trigger}
            onChange={(e) => setTrigger(e.target.value)}
            placeholder="good morning"
          />
        </Field>
        <Field label="Say back, exactly">
          <input
            className={inputClass}
            value={response}
            onChange={(e) => setResponse(e.target.value)}
            placeholder="Good morning, sir. Shall I run the briefing?"
          />
        </Field>
        <Btn tone="primary" onClick={add}>
          Add command
        </Btn>
      </div>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* The panel                                                                   */
/* -------------------------------------------------------------------------- */

/* -------------------------------------------------------------------------- */
/* The settings themselves                                                     */
/* -------------------------------------------------------------------------- */

/**
 * Loads the state and renders the tabs. Used twice: inside the slide-over on
 * the call screen, and as a whole page at /settings.
 *
 * The standalone page exists because the overlay can be covered. The call view
 * is a full-screen element with children at z-50, so a button floating above it
 * is one stacking-context change away from being unreachable - which is exactly
 * what happened. A page at its own URL cannot be covered by anything.
 */
export function SettingsBody({ onClose }: { onClose?: () => void }) {
  const [tab, setTab] = useState<Tab>('Rules');
  const [state, setState] = useState<State | null>(null);
  const [error, setError] = useState('');

  const load = useCallback(async () => {
    try {
      setState(await api('state'));
      setError('');
    } catch (e) {
      setError(String(e instanceof Error ? e.message : e));
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  return (
    <>
      <nav className="border-border flex gap-1 overflow-x-auto border-b px-3 py-2">
        {TABS.map((t) => (
          <button
            key={t}
            onClick={() => setTab(t)}
            className={`rounded-md px-3 py-1.5 text-sm font-medium whitespace-nowrap transition ${
              tab === t
                ? 'bg-secondary text-foreground'
                : 'text-muted-foreground hover:bg-secondary/60'
            }`}
          >
            {t}
          </button>
        ))}
      </nav>

      <div className="flex-1 overflow-y-auto px-5 py-5">
        {error && (
          <div className="border-destructive/30 bg-destructive/10 text-destructive mb-4 space-y-2 rounded-lg border px-4 py-3 text-sm">
            <p className="font-medium">Can&apos;t reach Jarvis&apos;s settings service.</p>
            <p className="opacity-90">{error}</p>
            <p className="opacity-90">
              It starts with the agent. Run <strong>butler-agent.bat</strong>, or{' '}
              <strong>butler-doctor.bat</strong> to see what&apos;s wrong.
            </p>
            <Btn onClick={load}>Try again</Btn>
          </div>
        )}
        {!state && !error && <p className="text-muted-foreground text-sm">Loading…</p>}
        {state && (
          <>
            {tab === 'Rules' && <RulesTab state={state} reload={load} />}
            {tab === 'Permissions' && <PermissionsTab state={state} reload={load} />}
            {tab === 'Voice' && <VoiceTab state={state} reload={load} />}
            {tab === 'Tasks' && <TasksTab state={state} reload={load} />}
            {tab === 'AI' && <AITab state={state} reload={load} />}
            {tab === 'Memory' && <MemoryTab state={state} reload={load} />}
            {tab === 'Commands' && <CommandsTab state={state} reload={load} />}
          </>
        )}
        {onClose && (
          <div className="border-border mt-6 border-t pt-4">
            <Btn onClick={onClose}>Close</Btn>
          </div>
        )}
      </div>
    </>
  );
}

export function ControlPanel() {
  const [open, setOpen] = useState(false);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false);
      // A keyboard way in, for when the button is covered by something.
      if (e.key.toLowerCase() === 's' && (e.ctrlKey || e.metaKey) && e.shiftKey) {
        e.preventDefault();
        setOpen((was) => !was);
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  return (
    <>
      <button
        onClick={() => setOpen(true)}
        aria-label="Open Jarvis settings"
        title="Settings (Ctrl+Shift+S)"
        className="border-border bg-background/90 hover:bg-secondary fixed top-4 right-4 z-[120] rounded-full border p-2.5 shadow-lg backdrop-blur transition"
      >
        <svg
          width="18"
          height="18"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.8"
        >
          <circle cx="12" cy="12" r="3" />
          <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.6 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.6a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9v0a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" />
        </svg>
      </button>

      {open && (
        <div className="fixed inset-0 z-[130] flex justify-end">
          <div
            className="absolute inset-0 bg-black/40 backdrop-blur-sm"
            onClick={() => setOpen(false)}
          />

          <aside className="bg-background border-border relative flex h-full w-full max-w-lg flex-col border-l shadow-2xl">
            <header className="border-border flex items-center justify-between border-b px-5 py-4">
              <div>
                <h2 className="text-base font-semibold">Jarvis</h2>
                <p className="text-muted-foreground text-xs">
                  What it knows, what it may do, and who it thinks with.
                </p>
              </div>
              <button
                onClick={() => setOpen(false)}
                aria-label="Close"
                className="hover:bg-secondary rounded-md p-1.5 transition"
              >
                <svg
                  width="18"
                  height="18"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="2"
                >
                  <path d="M18 6 6 18M6 6l12 12" />
                </svg>
              </button>
            </header>

            <SettingsBody />
          </aside>
        </div>
      )}
    </>
  );
}
