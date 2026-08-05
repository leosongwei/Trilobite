import type { Session } from '../types'

// Walk up parent_session links to the main session of the group a session
// belongs to. An unknown session (not yet picked up by the poll) is treated
// as its own root.
export function findSessionRoot(sessions: Session[], id: string | null): string | null {
  if (!id) return null
  let curId = id
  let parentId = sessions.find((s) => s.id === curId)?.parent_session ?? null
  while (parentId) {
    curId = parentId
    parentId = sessions.find((s) => s.id === curId)?.parent_session ?? null
  }
  return curId
}
