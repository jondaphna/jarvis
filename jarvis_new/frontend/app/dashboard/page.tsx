import { headers } from 'next/headers';
import { Dashboard } from '@/components/dashboard/dashboard';
import { getAppConfig } from '@/lib/utils';

/**
 * The console, at its own address.
 *
 * It owns a LiveKit session of its own, so you can talk to Jarvis from the
 * same screen that shows what he is doing in the background - and so this
 * page is a complete way in even when the call screen is not open.
 */
export default async function DashboardPage() {
  const hdrs = await headers();
  const appConfig = await getAppConfig(hdrs);

  return <Dashboard appConfig={appConfig} />;
}
