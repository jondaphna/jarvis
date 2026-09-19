'use client';

import { useState } from 'react';
import {
  AlertCircle,
  CheckCircle2,
  Clapperboard,
  Film,
  Instagram,
  PauseCircle,
  PenLine,
  PlayCircle,
  Quote,
  Wrench,
} from 'lucide-react';
import {
  type ContentAsset,
  type ContentJob,
  type ContentSnapshot,
  type PipelineStage,
  ago,
  post,
} from '@/lib/jarvis';
import { Action, Card, CardTitle, Empty, Flash, INPUT, Pill, Row, Stat, useFlash } from './kit';
import { ContentEngineSwitch } from './permissions-panel';

/**
 * The content side of the business, as it actually stands.
 *
 * It reports rather than flatters: only the script stage is built, so the
 * pipeline strip says so for the other five and names what each one is
 * waiting for. A dashboard that draws a green "publishing" light over a stage
 * nobody has written is worse than no dashboard.
 */

const STATUS_TONE = {
  queued: 'neutral',
  running: 'live',
  done: 'good',
  failed: 'bad',
  cancelled: 'neutral',
} as const;

const STAGE_ICON: Record<string, typeof Film> = {
  script: PenLine,
  voiceover: Quote,
  visuals: Film,
  render: Clapperboard,
  review: CheckCircle2,
  publish: Instagram,
};

export function ContentPanel({
  snapshot,
  reload,
}: {
  snapshot: ContentSnapshot | null;
  reload: () => void | Promise<void>;
}) {
  const [topic, setTopic] = useState('');
  const [count, setCount] = useState(3);
  const [queuing, setQueuing] = useState(false);
  const [flash, setFlash] = useFlash();
  const [failed, setFailed] = useState('');

  if (!snapshot) {
    return (
      <Card>
        <Empty>Reading the content engine…</Empty>
      </Card>
    );
  }

  const { counts, workers } = snapshot;
  const live = snapshot.jobs.filter((job) => job.status === 'queued' || job.status === 'running');

  const queue = async () => {
    setQueuing(true);
    setFailed('');
    try {
      const result = await post<{ ref: string }>('content', { topic, count });
      setFlash(`Queued as ${result.ref}. It writes in the background — this page follows it.`);
      setTopic('');
      await reload();
    } catch (e) {
      setFailed(e instanceof Error ? e.message : String(e));
    } finally {
      setQueuing(false);
    }
  };

  return (
    <div className="space-y-5">
      <Card>
        <CardTitle
          icon={Clapperboard}
          title="Content engine"
          hint={
            snapshot.enabled
              ? 'Switched on: Jarvis can write Reel scripts in the background while you talk to him about something else.'
              : 'Switched off. Its tools are not in the prompt, and nothing here will run until you turn it on.'
          }
          right={
            <Pill tone={snapshot.enabled ? 'live' : 'neutral'} pulse={snapshot.enabled}>
              {snapshot.enabled ? 'on' : 'off'}
            </Pill>
          }
        />
        <div className="grid grid-cols-2 gap-3 @xl:grid-cols-4">
          <Stat
            label="In flight"
            value={counts.running}
            tone={counts.running ? 'live' : 'neutral'}
          />
          <Stat label="Waiting" value={counts.queued} />
          <Stat label="Finished" value={counts.done} tone={counts.done ? 'good' : 'neutral'} />
          <Stat label="Failed" value={counts.failed} tone={counts.failed ? 'bad' : 'neutral'} />
        </div>
        <div className="mt-4 flex flex-wrap items-center gap-2">
          <ContentEngineSwitch enabled={snapshot.enabled} onChanged={reload} />
          <WorkerPauseButton paused={workers.paused} running={workers.running} reload={reload} />
        </div>
        {snapshot.error && (
          <p className="mt-3 text-xs text-amber-300">
            Part of this panel could not be read: {snapshot.error}
          </p>
        )}
      </Card>

      <Card>
        <CardTitle
          icon={PenLine}
          title="Write scripts now"
          hint="Say what the Reels are about. It runs on the background worker, so it never touches the conversation."
        />
        <form
          className="flex flex-col gap-2 @xl:flex-row"
          onSubmit={(event) => {
            event.preventDefault();
            if (topic.trim() && !queuing) void queue();
          }}
        >
          <input
            className={INPUT}
            value={topic}
            placeholder="e.g. three hooks about why most home studios sound bad"
            onChange={(event) => setTopic(event.target.value)}
          />
          <select
            className={`${INPUT} @xl:w-32`}
            value={count}
            onChange={(event) => setCount(Number(event.target.value))}
          >
            {[1, 3, 5].map((n) => (
              <option key={n} value={n} className="bg-slate-900">
                {n} script{n === 1 ? '' : 's'}
              </option>
            ))}
          </select>
          <Action
            type="submit"
            tone="primary"
            icon={PlayCircle}
            busy={queuing}
            disabled={!topic.trim() || !snapshot.enabled}
            title={snapshot.enabled ? undefined : 'Switch the content engine on first'}
          >
            Queue
          </Action>
        </form>
        <div className="mt-2 space-y-1">
          <Flash>{flash}</Flash>
          <Flash bad>{failed}</Flash>
        </div>
      </Card>

      <Card>
        <CardTitle
          icon={Wrench}
          title="The pipeline"
          hint="Script to published Reel. Each stage says what it is still waiting for, rather than pretending to be ready."
        />
        <div className="grid gap-2.5 @2xl:grid-cols-2">
          {snapshot.stages.map((stage) => (
            <StageCard key={stage.key} stage={stage} />
          ))}
          {!snapshot.stages.length && <Empty>The pipeline description could not be read.</Empty>}
        </div>
        {snapshot.style?.name && (
          <p className="mt-3 text-[11px] text-white/40">
            House style: <span className="text-white/70">{snapshot.style.name}</span>
            {snapshot.style.placeholder && ' — still the shipped placeholder'}
            {snapshot.style.file && (
              <>
                {' · '}
                <span className="font-mono break-all">{snapshot.style.file}</span>
              </>
            )}
          </p>
        )}
      </Card>

      <Card>
        <CardTitle
          icon={Film}
          title="Jobs"
          hint="Newest first. A job that is running shows the stage it is on."
          right={
            live.length ? (
              <Pill tone="live" pulse>
                {live.length} live
              </Pill>
            ) : undefined
          }
        />
        <div className="space-y-2">
          {snapshot.jobs.map((job) => (
            <JobRow key={job.ref} job={job} />
          ))}
          {!snapshot.jobs.length && <Empty>Nothing has been queued yet.</Empty>}
        </div>
      </Card>

      <Card>
        <CardTitle
          icon={Quote}
          title="Latest scripts"
          hint="The hook of each finished script, which is the part worth judging at a glance."
        />
        <div className="space-y-2">
          {snapshot.scripts.map((asset) => (
            <ScriptRow key={asset.id} asset={asset} />
          ))}
          {!snapshot.scripts.length && <Empty>No scripts written yet.</Empty>}
        </div>
      </Card>
    </div>
  );
}

function StageCard({ stage }: { stage: PipelineStage }) {
  const Icon = STAGE_ICON[stage.key] ?? Film;
  const tone = stage.ready ? 'good' : stage.implemented ? 'warn' : 'neutral';
  return (
    <div
      className={`rounded-xl border p-3.5 ${
        stage.ready
          ? 'border-emerald-300/25 bg-emerald-400/[0.05]'
          : 'border-white/8 bg-white/[0.025]'
      }`}
    >
      <p className="flex items-center gap-2 text-sm font-medium text-white">
        <Icon className={`size-4 ${stage.ready ? 'text-emerald-300' : 'text-white/40'}`} />
        {stage.label}
        <Pill tone={tone} className="ml-auto">
          {stage.ready ? 'ready' : stage.implemented ? 'blocked' : 'not built'}
        </Pill>
      </p>
      <p className="mt-1.5 text-xs leading-relaxed text-white/50">{stage.detail}</p>
      {stage.blocker && (
        <p className="mt-1.5 flex items-start gap-1.5 text-[11px] text-amber-300/90">
          <AlertCircle className="mt-px size-3 shrink-0" />
          {stage.blocker}
        </p>
      )}
    </div>
  );
}

function JobRow({ job }: { job: ContentJob }) {
  const tone = STATUS_TONE[job.status] ?? 'neutral';
  return (
    <Row>
      <div className="min-w-0 flex-1">
        <p className="truncate text-sm font-medium text-white">{job.topic || 'untitled'}</p>
        <p className="mt-1 text-[11px] text-white/45">
          <span className="font-mono">{job.ref}</span>
          {job.stage && ` · ${job.stage}`}
          {job.created_at && ` · queued ${ago(job.created_at)}`}
          {typeof job.result?.count === 'number' && ` · ${job.result.count} written`}
        </p>
        {job.error && <p className="mt-1 text-[11px] text-rose-300">{job.error}</p>}
      </div>
      <Pill tone={tone} pulse={job.status === 'running'}>
        {job.status}
      </Pill>
    </Row>
  );
}

function ScriptRow({ asset }: { asset: ContentAsset }) {
  const hook = asset.meta?.hook ?? asset.body?.hook ?? '(no hook)';
  const seconds = asset.meta?.seconds;
  return (
    <Row>
      <div className="min-w-0 flex-1">
        <p className="text-sm text-white/90">“{hook}”</p>
        <p className="mt-1 text-[11px] text-white/45">
          <span className="font-mono">{asset.job_ref}</span>
          {asset.at && ` · ${ago(asset.at)}`}
        </p>
      </div>
      {typeof seconds === 'number' && <Pill>{Math.round(seconds)}s</Pill>}
    </Row>
  );
}

/** One click between "the machine is working" and "the machine is quiet". */
export function WorkerPauseButton({
  paused,
  running,
  reload,
}: {
  paused: boolean;
  running: boolean;
  reload: () => void | Promise<void>;
}) {
  const [busy, setBusy] = useState(false);

  const flip = async () => {
    setBusy(true);
    try {
      await post('workers', { paused: !paused });
      await reload();
    } finally {
      setBusy(false);
    }
  };

  return (
    <Action
      icon={paused ? PlayCircle : PauseCircle}
      busy={busy}
      onClick={flip}
      disabled={!running && !paused}
      title={
        paused
          ? 'Let queued background jobs run again'
          : 'Stop starting new background jobs. Anything already running finishes.'
      }
    >
      {paused ? 'Resume background worker' : 'Pause background worker'}
    </Action>
  );
}
