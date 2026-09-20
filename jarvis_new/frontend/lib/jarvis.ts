/**
 * The browser's side of the control API, and the shapes it sends back.
 *
 * Every call goes to /api/control/…, which the Next server forwards to the
 * local control service with a token the page never sees. Nothing here talks
 * to 127.0.0.1:8765 directly, and nothing here should: the token is the only
 * thing standing between a stray tab and your key vault.
 */

export class ControlError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = 'ControlError';
    this.status = status;
  }
}

export async function api<T = unknown>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`/api/control/${path}`, {
    ...init,
    cache: 'no-store',
    headers: { 'Content-Type': 'application/json' },
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new ControlError(
      (body as { error?: string })?.error ?? `Request failed (${res.status})`,
      res.status
    );
  }
  return body as T;
}

/** POST with a JSON body, which is most of what the dashboard does. */
export function post<T = unknown>(path: string, body: unknown): Promise<T> {
  return api<T>(path, { method: 'POST', body: JSON.stringify(body) });
}

/* -------------------------------------------------------------------------- */
/* Shapes                                                                      */
/* -------------------------------------------------------------------------- */

export type Risk = 'low' | 'medium' | 'high';

export type Permission = {
  key: string;
  label: string;
  detail: string;
  tools: string[];
  enabled: boolean;
  default: boolean;
  risk: Risk;
};

/**
 * The always-on background side: the worker host and the routine ticker,
 * owned by whichever process holds the control API's port.
 *
 * `owner` and `owns_services` exist because there can be more than one JARVIS
 * process on the machine - the voice agent and a standalone control API - and
 * only one of them runs the services. Without this the dashboard could show
 * "workers idle" while the other process was busy.
 */
export type ServicesState = {
  /**
   * 'running' | 'degraded' | 'stopped' | 'killed' | 'unknown'. `killed` was
   * chosen; `degraded` means it came up but something in it did not.
   */
  state: string;
  running: boolean;
  /** Up, but the worker host or the routine ticker failed to start. */
  degraded: boolean;
  killed: boolean;
  /**
   * The durable stop latch. Unlike `killed`, this is a file on the machine,
   * so it is still true after closing the window and opening it again, and
   * every JARVIS process on the machine reads the same one.
   */
  execution_disabled: boolean;
  latch?: {
    disabled: boolean;
    at?: string;
    reason?: string;
    by?: string;
    generation?: number;
    path?: string;
  };
  owner: string;
  this_process: string;
  owns_services: boolean;
  uptime_seconds: number;
  error: string;
  /** How many jobs the last kill cancelled. Only present on a kill reply. */
  cancelled?: number;
  workers: {
    running: boolean;
    paused: boolean;
    queued: number;
    in_progress: number;
    error?: string;
  };
  routines: { ticking: boolean; error?: string };
};

export type WorkerJob = {
  ref: string;
  name: string;
  status: 'queued' | 'running' | 'done' | 'failed' | 'cancelled';
  error?: string;
  attempts?: number;
  queued_at?: number;
  started_at?: number | null;
  finished_at?: number | null;
};

export type WorkerStatus = {
  running: boolean;
  paused: boolean;
  queued: number;
  in_progress: number;
  concurrency: number;
  jobs: WorkerJob[];
  error?: string;
};

export type ContentJob = {
  ref: string;
  kind: string;
  topic: string;
  style?: string;
  status: 'queued' | 'running' | 'done' | 'failed';
  stage?: string;
  created_at?: string;
  started_at?: string | null;
  finished_at?: string | null;
  error?: string;
  result?: { count?: number; hooks?: string[] } | null;
};

export type ContentAsset = {
  id: number;
  job_ref: string;
  kind: string;
  body?: { hook?: string; caption?: string; hashtags?: string[] } | null;
  meta?: { hook?: string; seconds?: number } | null;
  at?: string;
};

export type PipelineStage = {
  key: string;
  label: string;
  detail: string;
  implemented: boolean;
  free: boolean;
  ready: boolean;
  missing: string[];
  blocker: string;
  notes?: string;
};

export type ContentSnapshot = {
  enabled: boolean;
  workers: WorkerStatus;
  jobs: ContentJob[];
  scripts: ContentAsset[];
  counts: { queued: number; running: number; done: number; failed: number };
  stages: PipelineStage[];
  style: { name?: string; placeholder?: boolean; file?: string };
  error?: string;
};

export type RoutineAction = { key: string; label: string; detail: string; needs: string };

/** One standing routine, as `RoutineEngine.listing()` sends it. */
export type Routine = {
  id: string;
  name: string;
  action: string;
  action_label?: string;
  instruction?: string;
  schedule: string;
  schedule_in_words?: string;
  timezone?: string;
  enabled: boolean;
  catch_up?: boolean;
  speak?: boolean;
  grace_seconds?: number;
  next_run_at?: string | null;
  last_run_at?: string | null;
  last_status?: string;
  last_output?: string;
  last_error?: string;
  runs?: number;
  failures?: number;
};

export type RoutineRun = {
  id: number;
  routine_id: string;
  job_ref?: string;
  trigger?: string;
  due_at?: string;
  started_at?: string;
  finished_at?: string | null;
  status: string;
  output?: string;
  error?: string;
};

export type RoutinesSnapshot = {
  routines: Routine[];
  actions: RoutineAction[];
  summary: {
    total?: number;
    enabled?: number;
    armed?: number;
    next?: { name: string; at: string } | null;
    failing?: string[];
    ticking?: boolean;
  };
  recent: RoutineRun[];
  error?: string;
};

/* -------------------------------------------------------------------------- */
/* Small formatting helpers                                                    */
/* -------------------------------------------------------------------------- */

/** "4m ago", from either an ISO string or epoch seconds. Never throws. */
export function ago(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === '') return '';
  const then = typeof value === 'number' ? value * 1000 : Date.parse(value);
  if (!Number.isFinite(then)) return '';
  const seconds = Math.round((Date.now() - then) / 1000);
  if (seconds < 0) return 'in a moment';
  if (seconds < 45) return 'just now';
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.round(hours / 24)}d ago`;
}

/** "today 07:30" / "Fri 19 Sep 07:30" — a time you can act on. */
export function when(value: string | null | undefined): string {
  if (!value) return '';
  const at = new Date(value);
  if (Number.isNaN(at.getTime())) return String(value);
  const clock = at.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  if (at.toDateString() === new Date().toDateString()) return `today ${clock}`;
  const day = at.toLocaleDateString([], { weekday: 'short', day: 'numeric', month: 'short' });
  return `${day} ${clock}`;
}

/** Seconds between two moments, for a job that is still running. */
export function elapsed(from?: number | null, to?: number | null): string {
  if (!from) return '';
  const seconds = Math.max(0, Math.round(((to ?? Date.now() / 1000) - from) * 10) / 10);
  if (seconds < 90) return `${Math.round(seconds)}s`;
  return `${Math.round(seconds / 60)}m`;
}
