'use client';

import { useState } from 'react';
import {
  AlertTriangle,
  Clapperboard,
  Globe,
  Laptop,
  Power,
  ShieldCheck,
  Sparkles,
} from 'lucide-react';
import { type Permission, post } from '@/lib/jarvis';
import { Action, Card, CardTitle, Empty, Flash, Pill, Row, Stat, Switch, useFlash } from './kit';

/**
 * Every capability switch, grouped the way you think about them.
 *
 * The matrix only draws what `permissions.CAPABILITIES` already declares, and
 * a toggle writes `permissions.<key>` through the same settings route the old
 * panel used. Nothing here decides what a fresh install starts with - that is
 * each capability's own `default`, which is shown beside the switch so a
 * machine that has drifted from it is obvious at a glance.
 */

const GROUPS: { title: string; icon: typeof Globe; keys: string[]; detail: string }[] = [
  {
    title: 'Web',
    icon: Globe,
    keys: ['browse', 'read_web', 'control_web'],
    detail: 'Opening sites, reading them, and acting inside them.',
  },
  {
    title: 'This machine',
    icon: Laptop,
    keys: ['apps', 'media', 'machine', 'files'],
    detail: 'Apps, windows, volume, and reading your own documents.',
  },
  {
    title: 'Knowing you',
    icon: Sparkles,
    keys: ['memory', 'learning', 'reasoning'],
    detail: 'What it remembers, what you teach it, and how hard it thinks.',
  },
  {
    title: 'The business',
    icon: Clapperboard,
    keys: ['content'],
    detail: 'The content engine. Off until you switch it on.',
  },
  {
    title: 'Power',
    icon: Power,
    keys: ['power'],
    detail: 'Lock, sleep, restart, shut down. It asks out loud first.',
  },
];

const RISK_LABEL: Record<Permission['risk'], string> = {
  low: '',
  medium: 'powerful',
  high: 'careful',
};

export function PermissionsPanel({
  permissions,
  reload,
}: {
  permissions: Permission[];
  reload: () => void | Promise<void>;
}) {
  const [busy, setBusy] = useState('');
  const [flash, setFlash] = useFlash();
  const [failed, setFailed] = useState('');

  const toggle = async (key: string, next: boolean) => {
    setBusy(key);
    setFailed('');
    try {
      await post('settings', { [`permissions.${key}`]: next });
      setFlash(`${next ? 'Switched on' : 'Switched off'}. It applies to the next conversation.`);
      await reload();
    } catch (e) {
      setFailed(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy('');
    }
  };

  const on = permissions.filter((p) => p.enabled);
  const tools = on.reduce((total, p) => total + p.tools.length, 0);
  const drifted = permissions.filter((p) => p.enabled !== p.default);
  const ungrouped = permissions.filter((p) => !GROUPS.some((group) => group.keys.includes(p.key)));

  return (
    <div className="space-y-5">
      <Card>
        <CardTitle
          icon={ShieldCheck}
          title="What Jarvis is allowed to do"
          hint="A switch that is off takes the tools away entirely — it is not asked to behave, it has no way to act. Changes apply when the next conversation starts; a call already running keeps what it began with."
          right={
            <div className="flex gap-2">
              <Pill tone={on.length ? 'live' : 'neutral'}>{on.length} on</Pill>
            </div>
          }
        />
        <div className="grid grid-cols-2 gap-3 @xl:grid-cols-4">
          <Stat label="Capabilities on" value={`${on.length}/${permissions.length}`} tone="live" />
          <Stat label="Tools it can reach" value={tools} />
          <Stat
            label="Changed from default"
            value={drifted.length}
            tone={drifted.length ? 'warn' : 'neutral'}
          />
          <Stat
            label="Careful ones on"
            value={on.filter((p) => p.risk === 'high').length}
            tone={on.some((p) => p.risk === 'high') ? 'warn' : 'neutral'}
          />
        </div>
        <div className="mt-3 space-y-1">
          <Flash>{flash}</Flash>
          <Flash bad>{failed}</Flash>
        </div>
      </Card>

      {!permissions.length && (
        <Card>
          <Empty>No capability switches came back from the control service.</Empty>
        </Card>
      )}

      {GROUPS.map((group) => {
        const rows = group.keys
          .map((key) => permissions.find((p) => p.key === key))
          .filter((p): p is Permission => Boolean(p));
        if (!rows.length) return null;
        return (
          <Card key={group.title}>
            <CardTitle icon={group.icon} title={group.title} hint={group.detail} />
            <div className="space-y-2.5">
              {rows.map((permission) => (
                <PermissionRow
                  key={permission.key}
                  permission={permission}
                  busy={busy === permission.key}
                  onToggle={toggle}
                />
              ))}
            </div>
          </Card>
        );
      })}

      {ungrouped.length > 0 && (
        <Card>
          <CardTitle
            icon={ShieldCheck}
            title="Everything else"
            hint="Capabilities this build added after the matrix was laid out."
          />
          <div className="space-y-2.5">
            {ungrouped.map((permission) => (
              <PermissionRow
                key={permission.key}
                permission={permission}
                busy={busy === permission.key}
                onToggle={toggle}
              />
            ))}
          </div>
        </Card>
      )}
    </div>
  );
}

function PermissionRow({
  permission,
  busy,
  onToggle,
}: {
  permission: Permission;
  busy: boolean;
  onToggle: (key: string, next: boolean) => void;
}) {
  const [showTools, setShowTools] = useState(false);
  const offByDefault = permission.default === false;

  return (
    <Row className={permission.enabled ? 'border-cyan-300/20 bg-cyan-400/[0.04]' : undefined}>
      <div className="min-w-0 flex-1">
        <p className="flex flex-wrap items-center gap-2 text-sm font-medium text-white">
          {permission.label}
          {permission.risk !== 'low' && (
            <Pill tone={permission.risk === 'high' ? 'bad' : 'warn'}>
              <AlertTriangle className="size-3" />
              {RISK_LABEL[permission.risk]}
            </Pill>
          )}
          {offByDefault && !permission.enabled && <Pill>off by default</Pill>}
          {permission.enabled !== permission.default && permission.enabled && (
            <Pill tone="warn">on, and off by default</Pill>
          )}
        </p>
        <p className="mt-1 text-xs leading-relaxed text-white/50">{permission.detail}</p>
        <button
          type="button"
          onClick={() => setShowTools((was) => !was)}
          className="mt-1.5 text-[11px] text-white/35 underline-offset-2 transition hover:text-cyan-200 hover:underline"
        >
          {permission.tools.length} tool{permission.tools.length === 1 ? '' : 's'}
          {showTools ? ' — hide' : ''}
        </button>
        {showTools && (
          <p className="mt-1.5 font-mono text-[11px] break-words text-white/40">
            {permission.tools.join('  ·  ')}
          </p>
        )}
      </div>
      <Switch
        on={permission.enabled}
        busy={busy}
        label={permission.label}
        onChange={(next) => onToggle(permission.key, next)}
      />
    </Row>
  );
}

/** The content-engine switch on its own, for the panels that want just it. */
export function ContentEngineSwitch({
  enabled,
  onChanged,
}: {
  enabled: boolean;
  onChanged: () => void | Promise<void>;
}) {
  const [busy, setBusy] = useState(false);

  const toggle = async () => {
    setBusy(true);
    try {
      await post('settings', { 'permissions.content': !enabled });
      await onChanged();
    } finally {
      setBusy(false);
    }
  };

  return (
    <Action
      icon={Clapperboard}
      tone={enabled ? 'ghost' : 'primary'}
      busy={busy}
      onClick={toggle}
      title="Adds or removes the content tools the next time a conversation starts"
    >
      {enabled ? 'Switch content engine off' : 'Switch content engine on'}
    </Action>
  );
}
