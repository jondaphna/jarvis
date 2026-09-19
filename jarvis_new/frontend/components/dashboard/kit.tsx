'use client';

import { useEffect, useState } from 'react';
import { Loader2 } from 'lucide-react';
import { cn } from '@/lib/shadcn/utils';

/**
 * The dashboard's visual vocabulary: glass over the HUD background.
 *
 * Everything here is a plain element with classes rather than a component
 * library. The panels are read at a glance while something is running, so the
 * whole surface is a handful of shapes - a card, a row, a pill, a switch -
 * repeated, and a new panel is assembled from them rather than styled again.
 */

export const GLASS =
  'rounded-2xl border border-white/10 bg-white/[0.04] backdrop-blur-xl ' +
  'shadow-[0_10px_40px_-12px_rgba(0,0,0,0.65)]';

export function Card({ className, children, ...props }: React.ComponentProps<'section'>) {
  return (
    <section className={cn(GLASS, 'p-5', className)} {...props}>
      {children}
    </section>
  );
}

export function CardTitle({
  icon: Icon,
  title,
  hint,
  right,
}: {
  icon?: React.ComponentType<{ className?: string }>;
  title: string;
  hint?: string;
  right?: React.ReactNode;
}) {
  return (
    <header className="mb-4 flex items-start justify-between gap-4">
      <div className="min-w-0">
        <h2 className="flex items-center gap-2 text-sm font-semibold tracking-tight text-white">
          {Icon && <Icon className="size-4 shrink-0 text-cyan-300" />}
          {title}
        </h2>
        {hint && <p className="mt-1 text-xs leading-relaxed text-white/50">{hint}</p>}
      </div>
      {right && <div className="shrink-0">{right}</div>}
    </header>
  );
}

const TONES = {
  neutral: 'bg-white/10 text-white/70 ring-white/15',
  live: 'bg-cyan-400/15 text-cyan-200 ring-cyan-300/30',
  good: 'bg-emerald-400/15 text-emerald-200 ring-emerald-300/30',
  warn: 'bg-amber-400/15 text-amber-200 ring-amber-300/30',
  bad: 'bg-rose-400/15 text-rose-200 ring-rose-300/30',
} as const;

export type Tone = keyof typeof TONES;

export function Pill({
  tone = 'neutral',
  pulse = false,
  className,
  children,
}: {
  tone?: Tone;
  pulse?: boolean;
  className?: string;
  children: React.ReactNode;
}) {
  return (
    <span
      className={cn(
        'inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-[11px] font-medium ring-1 ring-inset',
        TONES[tone],
        className
      )}
    >
      {pulse && (
        <span className="relative flex size-1.5">
          <span className="absolute inline-flex size-full animate-ping rounded-full bg-current opacity-70" />
          <span className="relative inline-flex size-1.5 rounded-full bg-current" />
        </span>
      )}
      {children}
    </span>
  );
}

/** A number worth looking at, with a word under it. */
export function Stat({
  label,
  value,
  tone = 'neutral',
}: {
  label: string;
  value: React.ReactNode;
  tone?: Tone;
}) {
  const colour = {
    neutral: 'text-white',
    live: 'text-cyan-200',
    good: 'text-emerald-200',
    warn: 'text-amber-200',
    bad: 'text-rose-200',
  }[tone];
  return (
    <div className="rounded-xl border border-white/8 bg-white/[0.03] px-3 py-2.5">
      <p className={cn('font-mono text-xl leading-none font-semibold', colour)}>{value}</p>
      <p className="mt-1.5 text-[11px] text-white/45">{label}</p>
    </div>
  );
}

export function Switch({
  on,
  onChange,
  busy = false,
  label,
  disabled = false,
}: {
  on: boolean;
  onChange: (next: boolean) => void;
  busy?: boolean;
  label: string;
  disabled?: boolean;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={on}
      aria-label={label}
      disabled={busy || disabled}
      onClick={() => onChange(!on)}
      className={cn(
        'relative h-6 w-11 shrink-0 rounded-full transition-colors duration-200',
        'focus-visible:ring-2 focus-visible:ring-cyan-300/60 focus-visible:outline-none',
        'disabled:cursor-not-allowed disabled:opacity-50',
        on ? 'bg-cyan-400/80 shadow-[0_0_18px_-2px_rgba(34,211,238,0.7)]' : 'bg-white/15'
      )}
    >
      <span
        className={cn(
          'absolute top-0.5 left-0.5 grid size-5 place-items-center rounded-full bg-white shadow transition-transform duration-200',
          on && 'translate-x-5'
        )}
      >
        {busy && <Loader2 className="size-3 animate-spin text-slate-600" />}
      </span>
    </button>
  );
}

const ACTION_TONES = {
  primary:
    'bg-cyan-400/90 text-slate-950 hover:bg-cyan-300 shadow-[0_0_24px_-6px_rgba(34,211,238,0.8)]',
  ghost: 'bg-white/8 text-white hover:bg-white/15 ring-1 ring-inset ring-white/10',
  danger: 'bg-rose-500/15 text-rose-200 hover:bg-rose-500/25 ring-1 ring-inset ring-rose-400/25',
} as const;

export function Action({
  icon: Icon,
  children,
  onClick,
  tone = 'ghost',
  busy = false,
  disabled = false,
  title,
  className,
  type = 'button',
}: {
  icon?: React.ComponentType<{ className?: string }>;
  children: React.ReactNode;
  onClick?: () => void;
  tone?: keyof typeof ACTION_TONES;
  busy?: boolean;
  disabled?: boolean;
  title?: string;
  className?: string;
  type?: 'button' | 'submit';
}) {
  return (
    <button
      type={type}
      title={title}
      onClick={onClick}
      disabled={busy || disabled}
      className={cn(
        'inline-flex items-center justify-center gap-2 rounded-xl px-3.5 py-2 text-sm font-medium',
        'transition-all duration-150 active:scale-[0.98]',
        'focus-visible:ring-2 focus-visible:ring-cyan-300/60 focus-visible:outline-none',
        'disabled:cursor-not-allowed disabled:opacity-45',
        ACTION_TONES[tone],
        className
      )}
    >
      {busy ? (
        <Loader2 className="size-4 animate-spin" />
      ) : (
        Icon && <Icon className="size-4 shrink-0" />
      )}
      {children}
    </button>
  );
}

export const INPUT =
  'w-full rounded-xl border border-white/12 bg-white/[0.04] px-3 py-2 text-sm text-white ' +
  'placeholder:text-white/30 outline-none transition focus:border-cyan-300/50 ' +
  'focus:ring-2 focus:ring-cyan-300/25';

export function Empty({ children }: { children: React.ReactNode }) {
  return (
    <p className="rounded-xl border border-dashed border-white/12 px-4 py-8 text-center text-sm text-white/40">
      {children}
    </p>
  );
}

export function Row({ className, children, ...props }: React.ComponentProps<'div'>) {
  return (
    <div
      className={cn(
        'flex items-start justify-between gap-4 rounded-xl border border-white/8 bg-white/[0.025] p-3.5',
        className
      )}
      {...props}
    >
      {children}
    </div>
  );
}

/** A short-lived line of feedback under an action ("Queued", "Saved"). */
export function useFlash(): [string, (message: string) => void] {
  const [message, setMessage] = useState('');
  useEffect(() => {
    if (!message) return;
    const timer = setTimeout(() => setMessage(''), 4000);
    return () => clearTimeout(timer);
  }, [message]);
  return [message, setMessage];
}

export function Flash({ children, bad = false }: { children: React.ReactNode; bad?: boolean }) {
  if (!children) return null;
  return <p className={cn('text-xs', bad ? 'text-rose-300' : 'text-emerald-300')}>{children}</p>;
}
