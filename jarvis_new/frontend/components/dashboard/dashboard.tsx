'use client';

import { useCallback, useEffect, useMemo, useState } from 'react';
import Link from 'next/link';
import { TokenSource } from 'livekit-client';
import {
  ArrowLeft,
  BrainCircuit,
  Clapperboard,
  Database,
  GraduationCap,
  ListChecks,
  Mic2,
  Radio,
  RefreshCw,
  ScrollText,
  ShieldCheck,
  Terminal,
  TriangleAlert,
} from 'lucide-react';
import { useSession } from '@livekit/components-react';
import type { AppConfig } from '@/app-config';
import { AgentSessionProvider } from '@/components/agents-ui/agent-session-provider';
import {
  AITab,
  CommandsTab,
  LearnedTab,
  MemoryTab,
  type State,
  TasksTab,
  VoiceTab,
} from '@/components/app/control-panel';
import { JarvisBackground } from '@/components/app/jarvis-background';
import { usePolled } from '@/hooks/useControl';
import { type ContentSnapshot, type RoutinesSnapshot, api } from '@/lib/jarvis';
import { cn } from '@/lib/shadcn/utils';
import { getSandboxTokenSource } from '@/lib/utils';
import { ContentPanel } from './content-panel';
import { Action, Card, Empty, Pill } from './kit';
import { LivePanel } from './live-panel';
import { PermissionsPanel } from './permissions-panel';
import { RoutinesPanel } from './routines-panel';

/**
 * One console for the whole assistant.
 *
 * It is drawn twice from the same body: as a page at /dashboard, and as a
 * slide-over on the call screen. The only difference is where the navigation
 * sits, because the answer to "what is it doing" should not change shape
 * depending on how you opened it.
 *
 * The live panel needs a LiveKit session, so the page owns one. On the call
 * screen there is already a session above the slide-over, which is why the
 * provider is on `Dashboard` and not on `DashboardBody`.
 */

type SectionKey =
  | 'live'
  | 'content'
  | 'permissions'
  | 'routines'
  | 'voice'
  | 'tasks'
  | 'ai'
  | 'learned'
  | 'memory'
  | 'commands';

const SECTIONS: {
  key: SectionKey;
  label: string;
  short: string;
  icon: typeof Radio;
  group: 'Console' | 'Settings';
}[] = [
  { key: 'live', label: 'Live voice & status', short: 'Live', icon: Radio, group: 'Console' },
  {
    key: 'content',
    label: 'Content engine',
    short: 'Content',
    icon: Clapperboard,
    group: 'Console',
  },
  {
    key: 'permissions',
    label: 'Permissions',
    short: 'Permissions',
    icon: ShieldCheck,
    group: 'Console',
  },
  {
    key: 'routines',
    label: 'Rules & routines',
    short: 'Rules',
    icon: ScrollText,
    group: 'Console',
  },
  { key: 'voice', label: 'Wake word & voice', short: 'Voice', icon: Mic2, group: 'Settings' },
  { key: 'tasks', label: 'Scheduled tasks', short: 'Tasks', icon: ListChecks, group: 'Settings' },
  { key: 'ai', label: 'Which AI thinks', short: 'AI', icon: BrainCircuit, group: 'Settings' },
  {
    key: 'learned',
    label: 'What it has learned',
    short: 'Learned',
    icon: GraduationCap,
    group: 'Settings',
  },
  { key: 'memory', label: 'Memory', short: 'Memory', icon: Database, group: 'Settings' },
  {
    key: 'commands',
    label: 'Spoken commands',
    short: 'Commands',
    icon: Terminal,
    group: 'Settings',
  },
];

/** How often each live surface is re-read. Slower where nothing moves fast. */
const CONTENT_EVERY = 4000;
const ROUTINES_EVERY = 15000;

export function Dashboard({ appConfig }: { appConfig: AppConfig }) {
  const tokenSource = useMemo(
    () =>
      typeof process.env.NEXT_PUBLIC_CONN_DETAILS_ENDPOINT === 'string'
        ? getSandboxTokenSource(appConfig)
        : TokenSource.endpoint('/api/token'),
    [appConfig]
  );
  const session = useSession(
    tokenSource,
    appConfig.agentName ? { agentName: appConfig.agentName } : undefined
  );

  return (
    <AgentSessionProvider session={session}>
      {/*
        The HUD behind the console is `position: fixed`, so it paints the
        viewport and nothing below it. The console is a fixed-height shell
        that scrolls inside itself for exactly that reason - without this
        wrapper's colour, a page taller than the window shows the light body
        background under the glass, which is the one thing the whole design
        assumes cannot happen.
      */}
      <div className="min-h-svh bg-[#02060d]">
        <JarvisBackground />
        <DashboardBody layout="page" />
      </div>
    </AgentSessionProvider>
  );
}

export function DashboardBody({
  layout = 'page',
  onClose,
}: {
  layout?: 'page' | 'drawer';
  onClose?: () => void;
}) {
  const [section, setSection] = useState<SectionKey>('live');
  const [settings, setSettings] = useState<State | null>(null);
  const [settingsError, setSettingsError] = useState('');

  const content = usePolled<ContentSnapshot>('content', CONTENT_EVERY);
  const routines = usePolled<RoutinesSnapshot>('routines', ROUTINES_EVERY);

  /**
   * `state` is the expensive call - it decrypts the vault and enumerates
   * Chrome profiles - so it is loaded once and re-read after a write, never
   * on a timer. The live surfaces have their own cheap endpoints.
   */
  const loadSettings = useCallback(async () => {
    try {
      setSettings(await api<State>('state'));
      setSettingsError('');
    } catch (e) {
      setSettingsError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    void loadSettings();
  }, [loadSettings]);

  const refreshAll = useCallback(async () => {
    await Promise.all([loadSettings(), content.reload(), routines.reload()]);
  }, [loadSettings, content, routines]);

  const offline = Boolean(settingsError && content.error);
  const current = SECTIONS.find((entry) => entry.key === section) ?? SECTIONS[0];

  return (
    <div
      className={cn(
        'dark relative z-10 text-white',
        layout === 'page' ? 'flex h-svh w-full overflow-hidden' : 'flex h-full min-h-0 flex-col'
      )}
    >
      {layout === 'page' && (
        <Sidebar
          section={section}
          onSelect={setSection}
          workersPaused={content.data?.workers.paused}
        />
      )}

      <div className="flex min-w-0 flex-1 flex-col">
        <Header
          title={current.label}
          layout={layout}
          offline={offline}
          onRefresh={refreshAll}
          onClose={onClose}
        />

        {/*
          The sidebar is the navigation on a wide screen, and it is not there
          on a narrow one - so this strip is, or a phone gets a console it
          cannot move around in.
        */}
        <nav
          className={cn(
            'flex gap-1.5 overflow-x-auto border-b border-white/8 px-4 py-2.5',
            layout === 'page' && 'md:hidden'
          )}
        >
          {SECTIONS.map((entry) => (
            <button
              key={entry.key}
              onClick={() => setSection(entry.key)}
              className={cn(
                'flex shrink-0 items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-xs font-medium whitespace-nowrap transition',
                section === entry.key
                  ? 'bg-cyan-400/15 text-cyan-100 ring-1 ring-cyan-300/25 ring-inset'
                  : 'text-white/55 hover:bg-white/8 hover:text-white'
              )}
            >
              <entry.icon className="size-3.5" />
              {entry.short}
            </button>
          ))}
        </nav>

        <main className="min-h-0 flex-1 overflow-y-auto px-4 py-5 sm:px-6 lg:px-8">
          <div className="@container mx-auto w-full max-w-5xl">
            {offline && <Offline message={settingsError} onRetry={refreshAll} />}

            {section === 'live' && (
              <LivePanel content={content.data} routines={routines.data} reload={refreshAll} />
            )}

            {section === 'content' && (
              <ContentPanel snapshot={content.data} reload={content.reload} />
            )}

            {section === 'permissions' &&
              (settings ? (
                <PermissionsPanel permissions={settings.permissions ?? []} reload={loadSettings} />
              ) : (
                <Loading error={settingsError} />
              ))}

            {section === 'routines' &&
              (settings ? (
                <RoutinesPanel
                  instructions={settings.settings?.persona?.instructions ?? ''}
                  never={settings.settings?.persona?.never ?? ''}
                  orders={settings.orders}
                  profiles={settings.browser_profiles ?? []}
                  snapshot={routines.data}
                  reloadState={loadSettings}
                  reloadRoutines={routines.reload}
                />
              ) : (
                <Loading error={settingsError} />
              ))}

            {LEGACY.includes(section) &&
              (settings ? (
                <Card className="legacy-tab">
                  <LegacyTab section={section} settings={settings} reload={loadSettings} />
                </Card>
              ) : (
                <Loading error={settingsError} />
              ))}
          </div>
        </main>
      </div>
    </div>
  );
}

const LEGACY: SectionKey[] = ['voice', 'tasks', 'ai', 'learned', 'memory', 'commands'];

function LegacyTab({
  section,
  settings,
  reload,
}: {
  section: SectionKey;
  settings: State;
  reload: () => void;
}) {
  switch (section) {
    case 'voice':
      return <VoiceTab state={settings} reload={reload} />;
    case 'tasks':
      return <TasksTab state={settings} reload={reload} />;
    case 'ai':
      return <AITab state={settings} reload={reload} />;
    case 'learned':
      return <LearnedTab state={settings} reload={reload} />;
    case 'memory':
      return <MemoryTab state={settings} reload={reload} />;
    case 'commands':
      return <CommandsTab state={settings} reload={reload} />;
    default:
      return null;
  }
}

function Sidebar({
  section,
  onSelect,
  workersPaused,
}: {
  section: SectionKey;
  onSelect: (key: SectionKey) => void;
  workersPaused?: boolean;
}) {
  const groups: ('Console' | 'Settings')[] = ['Console', 'Settings'];
  return (
    <aside className="sticky top-0 hidden h-svh w-60 shrink-0 flex-col border-r border-white/8 bg-black/30 px-3 py-5 backdrop-blur-xl md:flex">
      <Link href="/" className="mb-6 flex items-center gap-2.5 px-2">
        <span className="grid size-8 place-items-center rounded-xl bg-cyan-400/15 ring-1 ring-cyan-300/30 ring-inset">
          <Radio className="size-4 text-cyan-300" />
        </span>
        <span>
          <span className="block text-sm font-semibold tracking-tight">JARVIS</span>
          <span className="block text-[10px] tracking-[0.18em] text-white/35 uppercase">
            console
          </span>
        </span>
      </Link>

      <nav className="flex-1 space-y-5 overflow-y-auto">
        {groups.map((group) => (
          <div key={group}>
            <p className="px-2 pb-1.5 text-[10px] font-medium tracking-[0.18em] text-white/30 uppercase">
              {group}
            </p>
            <div className="space-y-0.5">
              {SECTIONS.filter((entry) => entry.group === group).map((entry) => (
                <button
                  key={entry.key}
                  onClick={() => onSelect(entry.key)}
                  className={cn(
                    'flex w-full items-center gap-2.5 rounded-xl px-2.5 py-2 text-left text-sm transition',
                    section === entry.key
                      ? 'bg-cyan-400/12 text-cyan-100 ring-1 ring-cyan-300/25 ring-inset'
                      : 'text-white/60 hover:bg-white/8 hover:text-white'
                  )}
                >
                  <entry.icon
                    className={cn('size-4 shrink-0', section === entry.key && 'text-cyan-300')}
                  />
                  <span className="truncate">{entry.label}</span>
                </button>
              ))}
            </div>
          </div>
        ))}
      </nav>

      {workersPaused && (
        <Pill tone="warn" className="mt-4 justify-center">
          background work paused
        </Pill>
      )}
    </aside>
  );
}

function Header({
  title,
  layout,
  offline,
  onRefresh,
  onClose,
}: {
  title: string;
  layout: 'page' | 'drawer';
  offline: boolean;
  onRefresh: () => void | Promise<void>;
  onClose?: () => void;
}) {
  const [spinning, setSpinning] = useState(false);

  const refresh = async () => {
    setSpinning(true);
    try {
      await onRefresh();
    } finally {
      setSpinning(false);
    }
  };

  return (
    <header
      className={cn(
        'sticky top-0 z-20 flex items-center justify-between gap-3 border-b border-white/8 bg-black/40 px-4 py-3.5 backdrop-blur-xl sm:px-6',
        // Room for the slide-over's own close button, which sits above this.
        layout === 'drawer' && 'pr-12 sm:pr-12'
      )}
    >
      <div className="flex min-w-0 items-center gap-3">
        {layout === 'page' && (
          <Link
            href="/"
            className="flex items-center gap-1.5 rounded-lg px-2 py-1.5 text-xs text-white/50 transition hover:bg-white/8 hover:text-white md:hidden"
          >
            <ArrowLeft className="size-3.5" />
            Call
          </Link>
        )}
        <h1 className="truncate text-sm font-semibold tracking-tight">{title}</h1>
        <Pill tone={offline ? 'bad' : 'live'} pulse={!offline}>
          {offline ? 'service unreachable' : 'live'}
        </Pill>
      </div>

      <div className="flex shrink-0 items-center gap-2">
        {layout === 'page' && (
          <Link
            href="/"
            className="hidden rounded-xl px-3 py-2 text-sm text-white/60 ring-1 ring-white/10 transition ring-inset hover:bg-white/8 hover:text-white md:inline-flex"
          >
            Back to the call
          </Link>
        )}
        <Action
          icon={RefreshCw}
          busy={spinning}
          onClick={() => void refresh()}
          title="Read everything again"
        >
          <span className="sr-only sm:not-sr-only">Refresh</span>
        </Action>
        {onClose && <Action onClick={onClose}>Close</Action>}
      </div>
    </header>
  );
}

function Loading({ error }: { error: string }) {
  if (error) {
    return (
      <Card>
        <Empty>{error}</Empty>
      </Card>
    );
  }
  return (
    <Card>
      <Empty>Reading Jarvis…</Empty>
    </Card>
  );
}

function Offline({ message, onRetry }: { message: string; onRetry: () => void | Promise<void> }) {
  return (
    <Card className="mb-5 border-rose-400/25 bg-rose-500/[0.07]">
      <p className="flex items-center gap-2 text-sm font-medium text-rose-100">
        <TriangleAlert className="size-4" />
        Can&apos;t reach Jarvis&apos;s control service.
      </p>
      <p className="mt-1.5 text-xs text-rose-100/70">{message}</p>
      <p className="mt-1.5 text-xs text-rose-100/70">
        It starts with the agent. Run <strong>butler-agent.bat</strong>, or{' '}
        <strong>butler-doctor.bat</strong> to see what is wrong.
      </p>
      <div className="mt-3">
        <Action onClick={() => void onRetry()}>Try again</Action>
      </div>
    </Card>
  );
}
