'use client';

import { useEffect, useMemo, useRef } from 'react';
import { ConnectionState } from 'livekit-client';
import { Volume2 } from 'lucide-react';
import {
  useConnectionState,
  useMaybeRoomContext,
  useMediaDeviceSelect,
} from '@livekit/components-react';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { cn } from '@/lib/shadcn/utils';

/**
 * Where JARVIS's voice comes out.
 *
 * The browser plays a call through the Windows default output, and on this
 * machine that is not the device anyone listens to - the voice went to a
 * speaker nobody could hear. So the call picks its output itself: the one
 * chosen last time if it is still plugged in, otherwise the first device whose
 * label matches PREFERRED, otherwise whatever Windows defaults to.
 */
const STORAGE_KEY = 'jarvis-speaker';
const PREFERRED = [/^AAA \(NVIDIA High Definition Audio\)/i, /NVIDIA High Definition Audio/i];

function savedSpeaker(): string | null {
  try {
    return window.localStorage.getItem(STORAGE_KEY);
  } catch {
    return null;
  }
}

function saveSpeaker(deviceId: string) {
  try {
    window.localStorage.setItem(STORAGE_KEY, deviceId);
  } catch {
    // A private window has no storage; the choice lasts for this call only.
  }
}

function pickSpeaker(devices: MediaDeviceInfo[]): string | undefined {
  const saved = savedSpeaker();
  if (saved && devices.some((d) => d.deviceId === saved)) return saved;
  for (const pattern of PREFERRED) {
    const match = devices.find((d) => pattern.test(d.label));
    if (match) return match.deviceId;
  }
  return undefined;
}

function useSpeakers() {
  const room = useMaybeRoomContext();
  const connected = useConnectionState(room) === ConnectionState.Connected;
  // Labels are blank until the page has microphone permission, which a live
  // call already has - so asking here never shows a second prompt.
  const select = useMediaDeviceSelect({ kind: 'audiooutput', room, requestPermissions: connected });
  const devices = useMemo(
    () => select.devices.filter((d) => d.deviceId !== '' && d.label !== ''),
    [select.devices]
  );
  return { ...select, devices, connected };
}

/**
 * Sends the call to the right speaker as soon as it connects. Renders nothing,
 * so it sits next to the audio renderer and works on every call screen.
 */
export function SpeakerRouting() {
  const { devices, activeDeviceId, setActiveMediaDevice, connected } = useSpeakers();
  const applied = useRef(false);

  useEffect(() => {
    if (!connected) {
      applied.current = false;
      return;
    }
    if (applied.current || devices.length === 0) return;
    const target = pickSpeaker(devices);
    applied.current = true;
    if (target && target !== activeDeviceId) {
      void setActiveMediaDevice(target);
    }
  }, [connected, devices, activeDeviceId, setActiveMediaDevice]);

  return null;
}

/** A dropdown to change the speaker mid-call. The choice is remembered. */
export function SpeakerSelect({ className }: { className?: string }) {
  const { devices, activeDeviceId, setActiveMediaDevice } = useSpeakers();

  if (devices.length < 2) return null;

  return (
    <Select
      value={activeDeviceId}
      onValueChange={(deviceId) => {
        saveSpeaker(deviceId);
        void setActiveMediaDevice(deviceId);
      }}
    >
      <SelectTrigger aria-label="Speaker" className={cn('w-auto gap-1.5', className)}>
        <Volume2 className="size-4" />
        <SelectValue className="sr-only" placeholder="Speaker" />
      </SelectTrigger>
      <SelectContent position="popper">
        {devices.map((device) => (
          <SelectItem key={device.deviceId} value={device.deviceId} className="font-mono text-xs">
            {device.label}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}
