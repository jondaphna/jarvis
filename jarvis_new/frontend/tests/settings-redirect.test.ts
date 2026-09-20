/**
 * `/settings` still exists because things point at it: `butler-settings.bat`,
 * the orb's menu, and whatever bookmark you made. It sends you to the console.
 *
 * Since owner authentication went in, that redirect has to carry the query
 * string. A `?key=…` dropped on the way is a link that signs you in turning
 * into a sign-in screen, which is exactly the failure nobody would think to
 * test by hand.
 */
import { describe, expect, it } from 'vitest';
import SettingsPage from '@/app/settings/page';

/** Where `redirect()` was sending us, read off the error it throws. */
async function redirectTarget(params: Record<string, string | string[]>): Promise<string> {
  try {
    await SettingsPage({ searchParams: Promise.resolve(params) });
  } catch (error) {
    const digest = (error as { digest?: string }).digest ?? '';
    expect(digest).toContain('NEXT_REDIRECT');
    return digest.split(';')[2];
  }
  throw new Error('the settings page did not redirect');
}

describe('/settings', () => {
  it('goes to the console', async () => {
    expect(await redirectTarget({})).toBe('/dashboard');
  });

  it('brings the sign-in key with it', async () => {
    expect(await redirectTarget({ key: 'a'.repeat(43) })).toBe(`/dashboard?key=${'a'.repeat(43)}`);
  });

  it('brings everything else too', async () => {
    expect(await redirectTarget({ autostart: '1', tab: 'content' })).toBe(
      '/dashboard?autostart=1&tab=content'
    );
  });

  it('escapes what it carries', async () => {
    expect(await redirectTarget({ key: 'a b&c=d' })).toBe('/dashboard?key=a+b%26c%3Dd');
  });
});
