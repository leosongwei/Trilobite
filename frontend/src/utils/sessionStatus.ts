import type { Session } from '../types'

// Persistent sidebar status dot: every session and project row shows one,
// with a fixed priority: running (green, pulsing) > suspended via
// sleep_until (blue) > idle (gray). The dot class/title mapping lives next
// to the status logic so templates stay a one-line lookup.
export type SessionStatus = 'running' | 'pending' | 'idle'

export function sessionStatus(s: Session): SessionStatus {
  if (s.is_running) return 'running'
  if (s.has_sleep) return 'pending'
  return 'idle'
}

// A project's dot follows its member sessions (top-level members; a running
// subagent implies its parent is running too): any running member wins,
// then any suspended member, otherwise idle.
export function projectStatus(members: Session[]): SessionStatus {
  if (members.some((s) => s.is_running)) return 'running'
  if (members.some((s) => s.has_sleep)) return 'pending'
  return 'idle'
}

export function statusDot(status: SessionStatus): { cls: string; title: string } {
  switch (status) {
    case 'running':
      return { cls: 'running-dot', title: 'running' }
    case 'pending':
      return { cls: 'pending-dot', title: 'suspended (sleep_until)' }
    case 'idle':
      return { cls: 'idle-dot', title: 'idle' }
  }
}
