import Link from 'next/link';
import { SettingsBody } from '@/components/app/control-panel';

/**
 * Settings as a whole page, at /settings.
 *
 * The slide-over on the call screen can be covered - the call view is a
 * full-screen element with children stacked above it, so a floating button is
 * one stacking change away from being unreachable. This page is reached by URL
 * and cannot be covered by anything.
 */
export default function SettingsPage() {
  return (
    <main className="bg-background mx-auto flex min-h-svh w-full max-w-3xl flex-col">
      <header className="border-border flex items-center justify-between border-b px-5 py-4">
        <div>
          <h1 className="text-base font-semibold">Jarvis — settings</h1>
          <p className="text-muted-foreground text-xs">
            What it knows, what it may do, and who it thinks with.
          </p>
        </div>
        <Link
          href="/"
          className="border-border hover:bg-secondary rounded-md border px-3 py-1.5 text-sm transition"
        >
          Back to Jarvis
        </Link>
      </header>

      <SettingsBody />
    </main>
  );
}
