import { redirect } from 'next/navigation';

/**
 * The old settings address, kept because things point at it: butler-settings.bat,
 * the orb's right-click menu, and whatever bookmark you made. Settings are one
 * part of the console now, so this sends you there rather than showing a second,
 * older version of the same screens.
 */
export default function SettingsPage() {
  redirect('/dashboard');
}
