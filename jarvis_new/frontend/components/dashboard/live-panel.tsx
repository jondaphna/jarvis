'use client';

import { useEffect, useRef, useState } from 'react';
import {
  Activity,
  CalendarClock,
  Clapperboard,
  Cpu,
  MessageSquare,
  Mic,
  PhoneOff,
  Radio,
  Sunrise,
  Waves,
} from 'lucide-react';
import {
  useSessionContext,
  useSessionMessages,
  useVoiceAssistant,
} from '@livekit/components-react';
import { AgentAudioVisualizerBar } from '@/components/agents-ui/agent-audio-visualizer-bar';
import { AgentControlBar } from '@/components/agents-ui/agent-control-bar';
import {
  type ContentSnapshot,
  type Routine,
  type RoutinesSnapshot,
  post,
  when,
} from '@/lib/jarvis';
import { WorkerPauseButton } from './content-panel';
import { Action, Card, CardTitle, Empty, Flash, Pill, Row, Stat, useFlash } from './kit';

/**
 * The call, and the state of everything running behind it.
 *
 * The voice half comes from the LiveKit session this page owns; the rest is
 * polled from the control service. They are deliberately on one screen: the
 * question people actually have is "is it listening, and is it doing anything
 * for me right now", and that spans both.
 */

const AGENT_STATE: Record<string, { label: string; tone: 'live' | 'good' | 'warn' | 'neutral' }> = {
  disconnected: { label: 'not connected', tone: 'neutral' },
  connecting: { label: 'connecting', tone: 'warn' },
  initializing: { label: 'waking up', tone: 'warn' },
  listening: { label: 'listening', tone: 'live' },
  thinking: { label: 'thinking', tone: 'warn' },
  speaking: { label: 'speaking', tone: 'good' },
};

export function LivePanel({
  content,
  routines,
  reload,
}: {
  content: ContentSnapshot | null;
  routines: RoutinesSnapshot | null;
  reload: () => void | Promise<void>;
}) {
  const session = useSessionContext();
  const { state: agentState, audioTrack } = useVoiceAssistant();
  const { messages } = useSessionMessages(session);
  const [starting, setStarting] = useState(false);
  const [flash, setFlash] = useFlash();
  const [failed, setFailed] = useState('');
  const transcriptRef = useRef<HTMLDivElement>(null);

  const connected = session.isConnected;
  const status = AGENT_STATE[connected ? agentState : 'disconnected'] ?? AGENT_STATE.disconnected;
  const workers = content?.workers;
  const morning = routines?.routines.find(
    (routine: Routine) => routine.id === 'good-morning' || routine.action === 'briefing'
  );

  useEffect(() => {
    const box = transcriptRef.current;
    if (box) box.scrollTop = box.scrollHeight;
  }, [messages]);

  const connect = async () => {
    setStarting(true);
    setFailed('');
    try {
      await session.start();
    } catch (e) {
      setFailed(e instanceof Error ? e.message : String(e));
    } finally {
      setStarting(false);
    }
  };

  const runMorning = async () => {
    if (!morning) return;
    setFailed('');
    try {
      await post(`routines/${encodeURIComponent(morning.id)}/run`, {});
      setFlash(`${morning.name} is running. It appears under Rules & routines when it finishes.`);
      await reload();
    } catch (e) {
      setFailed(e instanceof Error ? e.message : String(e));
    }
  };

  return (
    <div className="space-y-5">
      <Card className="overflow-hidden">
        <CardTitle
          icon={Radio}
          title="Live voice"
          hint="The microphone is only open while a call is running. Nothing is sent anywhere when it is not."
          right={
            <Pill tone={status.tone} pulse={connected && agentState !== 'disconnected'}>
              {status.label}
            </Pill>
          }
        />

        <div className="flex flex-col items-center gap-5 rounded-2xl border border-white/8 bg-black/20 px-4 py-8">
          <AgentAudioVisualizerBar
            state={connected ? agentState : 'disconnected'}
            audioTrack={audioTrack}
            barCount={9}
            className="h-16 text-cyan-300"
          />
          <p className="text-center text-sm text-white/60">
            {connected
              ? agentState === 'listening'
                ? 'Go ahead — he is listening.'
                : `Jarvis is ${status.label}.`
              : 'Start a call and just talk. The browser asks for the microphone the first time.'}
          </p>

          {connected ? (
            <div className="w-full max-w-lg">
              <AgentControlBar
                variant="livekit"
                isConnected={connected}
                onDisconnect={session.end}
                controls={{
                  leave: true,
                  microphone: true,
                  chat: true,
                  camera: false,
                  screenShare: false,
                }}
              />
            </div>
          ) : (
            <Action icon={Mic} tone="primary" busy={starting} onClick={() => void connect()}>
              Start talking
            </Action>
          )}
          <div className="space-y-1">
            <Flash>{flash}</Flash>
            <Flash bad>{failed}</Flash>
          </div>
        </div>
      </Card>

      <div className="grid gap-5 @3xl:grid-cols-2">
        <Card>
          <CardTitle
            icon={Activity}
            title="What is running behind the call"
            hint="Background work never shares the voice thread, so a long job here cannot make him slow to answer."
            right={
              workers ? (
                <Pill
                  tone={workers.paused ? 'warn' : workers.running ? 'live' : 'neutral'}
                  pulse={Boolean(workers.running && !workers.paused && workers.in_progress)}
                >
                  {workers.paused ? 'paused' : workers.running ? 'working' : 'stopped'}
                </Pill>
              ) : undefined
            }
          />
          <div className="grid grid-cols-2 gap-3">
            <Stat
              label="Jobs in flight"
              value={workers?.in_progress ?? 0}
              tone={workers?.in_progress ? 'live' : 'neutral'}
            />
            <Stat label="Waiting" value={workers?.queued ?? 0} />
            <Stat
              label="Routines armed"
              value={routines?.summary.armed ?? 0}
              tone={routines?.summary.armed ? 'live' : 'neutral'}
            />
            <Stat
              label="Content engine"
              value={<span className="text-sm">{content?.enabled ? 'on' : 'off'}</span>}
              tone={content?.enabled ? 'live' : 'neutral'}
            />
          </div>

          {routines?.summary.next && (
            <p className="mt-3 flex items-center gap-2 text-xs text-white/55">
              <CalendarClock className="size-3.5 text-cyan-300" />
              {routines.summary.next.name}, {when(routines.summary.next.at)}
            </p>
          )}

          <div className="mt-4 flex flex-wrap gap-2">
            {morning && (
              <Action icon={Sunrise} tone="primary" onClick={() => void runMorning()}>
                Run morning briefing now
              </Action>
            )}
            {workers && (
              <WorkerPauseButton
                paused={workers.paused}
                running={workers.running}
                reload={reload}
              />
            )}
          </div>
        </Card>

        <Card>
          <CardTitle
            icon={Cpu}
            title="Background jobs"
            hint="The last few things the worker host picked up."
          />
          <div className="space-y-2">
            {workers?.jobs.slice(0, 6).map((job) => (
              <Row key={job.ref}>
                <div className="min-w-0 flex-1">
                  <p className="truncate text-sm text-white/90">{job.name}</p>
                  <p className="mt-0.5 font-mono text-[11px] text-white/40">{job.ref}</p>
                  {job.error && <p className="mt-1 text-[11px] text-rose-300">{job.error}</p>}
                </div>
                <Pill
                  tone={
                    job.status === 'running'
                      ? 'live'
                      : job.status === 'done'
                        ? 'good'
                        : job.status === 'failed'
                          ? 'bad'
                          : 'neutral'
                  }
                  pulse={job.status === 'running'}
                >
                  {job.status}
                </Pill>
              </Row>
            ))}
            {!workers?.jobs.length && <Empty>The worker host has had nothing to do.</Empty>}
          </div>
          {content && content.counts.running + content.counts.queued > 0 && (
            <p className="mt-3 flex items-center gap-2 text-xs text-cyan-200">
              <Clapperboard className="size-3.5" />
              {content.counts.running + content.counts.queued} content job
              {content.counts.running + content.counts.queued === 1 ? '' : 's'} in the pipeline.
            </p>
          )}
        </Card>
      </div>

      <Card>
        <CardTitle
          icon={MessageSquare}
          title="This conversation"
          hint="What has been said since the call started. It is not kept here — the durable record is his memory."
          right={
            <Pill tone={messages.length ? 'live' : 'neutral'}>
              <Waves className="size-3" />
              {messages.length} turn{messages.length === 1 ? '' : 's'}
            </Pill>
          }
        />
        <div ref={transcriptRef} className="max-h-72 space-y-2 overflow-y-auto pr-1">
          {messages.map((message) => (
            <div
              key={message.id}
              className={`rounded-xl px-3.5 py-2.5 text-sm ${
                message.from?.isLocal
                  ? 'ml-8 bg-cyan-400/10 text-cyan-50'
                  : 'mr-8 bg-white/[0.045] text-white/85'
              }`}
            >
              <p className="mb-0.5 text-[10px] tracking-wide text-white/35 uppercase">
                {message.from?.isLocal ? 'you' : 'jarvis'}
              </p>
              {message.message}
            </div>
          ))}
          {!messages.length && (
            <Empty>
              {connected ? 'Nothing said yet.' : 'Start a call and what you say appears here.'}
            </Empty>
          )}
        </div>
      </Card>
    </div>
  );
}

export { PhoneOff };
