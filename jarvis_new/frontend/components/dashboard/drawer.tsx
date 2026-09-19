'use client';

import { useEffect, useState } from 'react';
import { SlidersHorizontal, X } from 'lucide-react';
import { DashboardBody } from './dashboard';

/**
 * The console as a slide-over, for when you are mid-call and don't want to
 * leave the screen.
 *
 * There is a keyboard way in as well as a button. The button floats above the
 * call view, and anything that changes the stacking there can cover it - which
 * has happened. Ctrl+Shift+S cannot be covered, and /dashboard cannot either.
 */
export function ControlPanel() {
  const [open, setOpen] = useState(false);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false);
      if (event.key.toLowerCase() === 's' && (event.ctrlKey || event.metaKey) && event.shiftKey) {
        event.preventDefault();
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
        aria-label="Open the Jarvis console"
        title="Console (Ctrl+Shift+S)"
        className="fixed top-4 right-4 z-[120] rounded-full border border-white/15 bg-black/50 p-2.5 text-white/80 shadow-lg backdrop-blur-xl transition hover:bg-white/10 hover:text-white"
      >
        <SlidersHorizontal className="size-4" />
      </button>

      {open && (
        <div className="fixed inset-0 z-[130] flex justify-end">
          <div
            className="absolute inset-0 bg-black/50 backdrop-blur-sm"
            onClick={() => setOpen(false)}
          />

          <aside className="relative flex h-full w-full max-w-xl flex-col border-l border-white/10 bg-[#03070f]/95 shadow-2xl">
            <button
              onClick={() => setOpen(false)}
              aria-label="Close"
              className="absolute top-3.5 right-4 z-30 rounded-lg p-1.5 text-white/60 transition hover:bg-white/10 hover:text-white"
            >
              <X className="size-4" />
            </button>
            <DashboardBody layout="drawer" />
          </aside>
        </div>
      )}
    </>
  );
}
