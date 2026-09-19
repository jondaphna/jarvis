import { promises as fs } from 'fs';
import os from 'os';
import path from 'path';

/**
 * Where JARVIS keeps its files. Mirrors jarvis/paths.py exactly — if that
 * moves, this has to move with it, or the interface quietly loses its token
 * and every panel shows "can't reach the control API".
 */
export function jarvisRoot(): string {
  const override = process.env.JARVIS_HOME;
  if (override) return override;

  if (process.platform === 'win32') {
    const base = process.env.APPDATA ?? path.join(os.homedir(), 'AppData', 'Roaming');
    return path.join(base, 'JARVIS');
  }
  if (process.platform === 'darwin') {
    return path.join(os.homedir(), 'Library', 'Application Support', 'JARVIS');
  }
  const base = process.env.XDG_DATA_HOME ?? path.join(os.homedir(), '.local', 'share');
  return path.join(base, 'jarvis');
}

export const CONTROL_URL = process.env.JARVIS_CONTROL_URL ?? 'http://127.0.0.1:8765';

/**
 * The shared secret the control API requires. Read on the server only — it
 * must never reach the browser, which is the whole reason these calls are
 * proxied rather than made directly from the page.
 */
export async function controlToken(): Promise<string | null> {
  try {
    const raw = await fs.readFile(path.join(jarvisRoot(), 'control.token'), 'utf8');
    return raw.trim() || null;
  } catch {
    return null;
  }
}
