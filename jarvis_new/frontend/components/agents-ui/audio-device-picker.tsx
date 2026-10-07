'use client';

import { useMemo } from 'react';
import { Mic, Volume2 } from 'lucide-react';
import { useMaybeRoomContext, useMediaDeviceSelect } from '@livekit/components-react';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { cn } from '@/lib/shadcn/utils';

/**
 * Which microphone the call listens on and which speaker it plays through.
 *
 * The browser otherwise uses whatever Windows has as its default, and that is
 * not always the device you are actually listening on.
 */
export function AudioDevicePicker({ className }: { className?: string }) {
  return (
    <div className={cn('grid w-full gap-2 sm:grid-cols-2', className)}>
      <DeviceSelect kind="audioinput" label="Microphone" icon={Mic} />
      <DeviceSelect kind="audiooutput" label="Speaker" icon={Volume2} />
    </div>
  );
}

function DeviceSelect({
  kind,
  label,
  icon: Icon,
}: {
  kind: 'audioinput' | 'audiooutput';
  label: string;
  icon: typeof Mic;
}) {
  const room = useMaybeRoomContext();
  // A live call already has microphone permission, so asking for it here only
  // makes the device names readable; it never shows a second prompt.
  const { devices, activeDeviceId, setActiveMediaDevice } = useMediaDeviceSelect({
    room,
    kind,
    requestPermissions: true,
  });
  const named = useMemo(() => devices.filter((d) => d.deviceId !== ''), [devices]);

  return (
    <label className="text-muted-foreground flex min-w-0 flex-col gap-1 text-xs">
      <span className="flex items-center gap-1.5">
        <Icon className="size-3.5" />
        {label}
      </span>
      <Select
        value={activeDeviceId}
        onValueChange={(deviceId) => void setActiveMediaDevice(deviceId)}
      >
        <SelectTrigger aria-label={label} className="w-full min-w-0 text-xs">
          <SelectValue placeholder="System default" />
        </SelectTrigger>
        <SelectContent position="popper">
          {named.map((device, index) => (
            <SelectItem key={device.deviceId} value={device.deviceId} className="text-xs">
              {device.label || `${label} ${index + 1}`}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </label>
  );
}
