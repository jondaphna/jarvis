import { redirect } from 'next/navigation';

/**
 * The old settings address, kept because things point at it: butler-settings.bat,
 * the orb's right-click menu, and whatever bookmark you made. Settings are one
 * part of the console now, so this sends you there rather than showing a second,
 * older version of the same screens.
 *
 * The query string comes along. Since owner authentication went in, the way
 * that matters is `?key=…`: a redirect that dropped it would take you from a
 * link that signs you in to a sign-in screen.
 */
export default async function SettingsPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const params = new URLSearchParams();
  for (const [name, value] of Object.entries(await searchParams)) {
    for (const one of Array.isArray(value) ? value : value === undefined ? [] : [value]) {
      params.append(name, one);
    }
  }
  const query = params.toString();
  redirect(query ? `/dashboard?${query}` : '/dashboard');
}
