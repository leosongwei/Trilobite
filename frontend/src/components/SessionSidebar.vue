<template>
  <aside class="sidebar" :style="{ '--sidebar-width': sidebarWidth + 'px' }">
    <div class="sidebar-header">
      <h1>Trilobite<span v-if="version" class="version"> v{{ version }}</span></h1>
      <input v-model="name" type="text" placeholder="Session / Project name" />
      <label>Working directory:</label>
      <input v-model="workingDir" type="text" placeholder="/home/user/project" />
      <div class="header-buttons">
        <button @click="handleCreate">+ New Session</button>
        <button class="secondary" @click="handleCreateProject">+ New Project</button>
      </div>
    </div>
    <div class="sessions" :style="{ height: sessionsHeight + 'px' }">
      <template v-for="item in sessionRows" :key="item.key">
        <div
          v-if="item.kind === 'project'"
          class="session-item project-row"
          :title="item.project.working_dir"
          @click="toggleProject(item.project.id)"
        >
          <span class="session-label">
            <span class="status-dot" :class="statusDot(item.dot).cls" :title="statusDot(item.dot).title"></span>
            <span class="tree-arrow ms ms-expand" :class="{ open: !isProjectCollapsed(item.project.id) }"></span>
            <span class="tree-icon ms ms-folder"></span>
            <span class="project-name">{{ item.project.name }}</span>
          </span>
          <span class="project-actions">
            <button class="project-add" type="button" title="New session in project" @click.stop="handleCreateInProject(item.project)"><span class="ms ms-add"></span></button>
            <button class="delete" type="button" title="Delete project" @click.stop="handleDeleteProject(item.project)"><span class="ms ms-close"></span></button>
          </span>
        </div>
        <div
          v-else-if="item.kind === 'session'"
          class="session-item"
          :class="[item.project ? 'project-session' : '', { active: item.session.id === state.currentSession }]"
          @click="handleSelect(item.session.id)"
        >
          <span class="session-label">
            <span class="status-dot" :class="statusDot(item.dot).cls" :title="statusDot(item.dot).title"></span>
            <span
              v-if="item.kind === 'session' && item.children.length"
              class="tree-arrow ms ms-expand"
              :class="{ open: isSessionExpanded(item.session.id) }"
              :title="isSessionExpanded(item.session.id) ? 'Collapse subagents' : `Expand subagents (${item.children.length})`"
              @click.stop="toggleSessionChildren(item.session.id)"
            ></span>
            <span v-if="item.kind === 'session' && item.children.length && !isSessionExpanded(item.session.id)" class="child-count">{{ item.children.length }}</span>
            {{ item.session.name }}
          </span>
          <span class="delete" @click.stop="handleDelete(item.session.id)"><span class="ms ms-close"></span></span>
        </div>
        <template v-if="sessionChildrenExpanded(item)">
          <div
            v-for="c in item.children"
            :key="c.id"
            class="session-item child"
            :class="[item.project ? 'project-child' : '', { active: c.id === state.currentSession }]"
            @click="handleSelect(c.id)"
          >
            <span class="session-label" :title="childLabel(c)">
              <span v-if="c.is_running" class="running-dot" title="running"></span>
              <span v-if="c.kind === 'scheduled'" class="child-badge scheduled" title="scheduled agent"><span class="ms ms-schedule"></span></span>
              <span v-else class="child-badge" :class="{ explore: c.subagent_type === 'explore' }" :title="c.subagent_type">{{ (c.subagent_type || '').slice(0, 2) }}</span>
              <span v-if="c.kind === 'scheduled' && !c.is_running && c.last_state === 'error'" class="stopped-dot" title="error"></span>
              <span v-else-if="c.kind === 'scheduled' && !c.is_running && c.last_state" class="sealed-dot" title="finished"></span>
              <span v-else-if="c.kind === 'scheduled' && !c.is_running" class="pending-dot" title="pending"></span>
              <span v-if="c.sealed" class="sealed-dot" title="finished"></span>
              {{ c.description || c.name }}
            </span>
            <span v-if="!c.is_running" class="delete" title="delete session" @click.stop="handleDelete(c.id)"><span class="ms ms-close"></span></span>
          </div>
        </template>
      </template>
    </div>
    <div class="sidebar-resizer" title="Drag to resize" @mousedown="startResize"></div>
    <div class="sidebar-bottom">
      <div v-if="state.currentSession" class="session-info">
        <div class="info-row">
          <span class="info-label">Session:</span>
          <template v-if="editingName">
            <input
              v-model="editName"
              class="info-edit"
              type="text"
              @keydown.enter="saveName"
              @keydown.esc="cancelName"
            />
            <button class="icon-btn" title="Save" @click="saveName"><span class="ms ms-check"></span></button>
            <button class="icon-btn" title="Cancel" @click="cancelName"><span class="ms ms-close"></span></button>
          </template>
          <template v-else>
            <span class="info-value" :title="currentSessionName">{{ currentSessionName }}</span>
            <button
              v-if="!state.isSubagent"
              class="icon-btn"
              title="Rename session"
              @click="startEditName"
            ><span class="ms ms-edit-square"></span></button>
          </template>
        </div>
        <div class="info-row">
          <span class="info-label">cwd:</span>
          <span class="info-value" :title="currentSessionCwd">{{ currentSessionCwd }}</span>
        </div>
        <div v-if="projectSelectorVisible" class="info-row">
          <span class="info-label">project:</span>
          <select
            :value="projectSelValue"
            class="info-select"
            title="Sidebar grouping only; does not change cwd"
            @change="handleProjectChange"
          >
            <option value="">(none)</option>
            <option v-for="p in state.projects" :key="p.id" :value="p.id">{{ p.name }}</option>
          </select>
        </div>
        <details>
          <summary>Allowed directories ({{ groupDirs.length }})</summary>
          <div v-if="groupDirs.length === 0" class="requests-empty">
            No allowed directories
          </div>
          <div v-for="entry in groupDirs" :key="entry.path" class="dir-item">
            <span class="dir-path" :title="entry.path">
              <span v-if="entry.source" class="dir-source">{{ entry.source }}</span>
              {{ entry.path }}
            </span>
            <span class="delete" @click="handleRemoveGroupDir(entry)"><span class="ms ms-close"></span></span>
          </div>
          <div class="dir-add">
            <input v-model="newDir" type="text" placeholder="/path/to/dir" @keydown.enter="handleAddDir" />
            <button @click="handleAddDir">+</button>
          </div>
        </details>
        <details ref="requestsDetails" class="requests-list">
          <summary>Pending Requests ({{ state.pendingRequests.length }})</summary>
          <div v-if="state.pendingRequests.length === 0" class="requests-empty">
            No pending requests
          </div>
          <div v-for="r in state.pendingRequests" :key="r.key" class="request-item">
            <div class="request-item-label">
              <span class="request-kind">{{ r.kind === 'plan_exit' ? 'mode' : 'dir' }}</span>
              <span class="request-agent" :title="r.session">{{ requestAgentLabel(r) }}</span>
            </div>
            <div class="request-item-detail">{{ requestDetail(r) }}</div>
            <div class="request-actions">
              <button class="request-approve" @click="approveRequest(r)">Approve</button>
              <button class="request-reject" @click="rejectRequest(r)">Reject</button>
            </div>
          </div>
        </details>
      </div>
      <div v-if="!state.isSubagent && state.currentSession" class="sidebar-tree">
        <div class="sidebar-tree-header">
          <span class="sidebar-tree-title">Session files</span>
        </div>
        <FileTree
          ref="treeRef"
          :session-id="state.currentSession"
          :roots="fsRoots"
          :base="base"
          @open-file="(f) => emit('open-file', f)"
          @root-info="onRootInfo"
        />
      </div>
    </div>
  </aside>
</template>

<script setup lang="ts">
import { ref, computed, watch, onMounted } from 'vue'
import { useStore } from '../store'
import { getCwd, getVersion, removeDir as apiRemoveDir } from '../api'
import type { Session, PendingRequest, Project } from '../types'
import { findSessionRoot } from '../utils/sessions'
import { projectStatus, sessionStatus, statusDot } from '../utils/sessionStatus'
import type { SessionStatus } from '../utils/sessionStatus'
import FileTree from './FileTree.vue'
import type { FsRoot } from './FileTree.vue'

const emit = defineEmits<{
  select: []
  'open-file': [file: { path: string; name: string; status?: string }]
  'root-info': [info: { path: string; isGit: boolean; branches: string[]; currentBranch: string }]
}>()

// Diff base branch, shared with the file manager; the tree's change
// highlighting always compares the working tree against it.
const props = defineProps<{ base: string; requestsTick?: number; sidebarWidth: number }>()

const { state, selectSession, createSession, deleteSession, addDir, renameSession, approveRequest, rejectRequest, createProject, deleteProject, setSessionProject } = useStore()
const name = ref('')
const workingDir = ref('')
const newDir = ref('')
const version = ref('')
const editingName = ref(false)
const editName = ref('')
const sessionsHeight = ref(300)
const treeRef = ref<InstanceType<typeof FileTree> | null>(null)
const requestsDetails = ref<HTMLDetailsElement | null>(null)
// Expanded/collapsed state of project folders (in-memory only, not persisted).
const collapsedProjects = ref(new Set<string>())
// Subagent lists collapse per session; expanded only while explicitly
// toggled, so new sessions are collapsed by default (in-memory only).
const expandedSessions = ref(new Set<string>())

// The aggregated banner's Review button bumps requestsTick to open the
// Requests list (the user can still collapse/expand it freely afterwards).
watch(
  () => props.requestsTick,
  () => {
    if (requestsDetails.value) requestsDetails.value.open = true
  },
)

// Requests list display helpers: which agent asked, and for what.
function childLabel(c: Session): string {
  if (c.kind === 'scheduled') {
    const parts = [`cron: ${c.cron || ''}`, `runs: ${c.run_count ?? 0}`, `last: ${c.last_state ?? '-'}`]
    if (c.schedule_active === false) parts.push(c.deleted ? '(schedule deleted)' : '(completed)')
    return parts.join(' · ')
  }
  return c.subagent_type || ''
}

function requestAgentLabel(r: PendingRequest): string {
  if (r.childType) return `[${r.childType}: ${r.childDescription}]`
  const s = state.sessions.find((x) => x.id === r.session)
  return s?.name || r.session
}

function requestDetail(r: PendingRequest): string {
  if (r.kind === 'plan_exit') return 'Switch to Build mode'
  return r.path ?? ''
}

function onRootInfo(info: { path: string; isGit: boolean; branches: string[]; currentBranch: string }) {
  emit('root-info', info)
}

const currentSessionObj = computed(() =>
  state.sessions.find((s) => s.id === state.currentSession),
)
const currentSessionName = computed(() => {
  if (state.isSubagent) return state.subagentDescription || state.currentSession || ''
  return currentSessionObj.value?.name ?? state.currentSession ?? ''
})
const currentSessionCwd = computed(() => currentSessionObj.value?.working_dir ?? '')

// The project selector only makes sense for top-level main sessions: a
// subagent/scheduled session is nested under its parent and never rendered
// as an independent row.
const projectSelectorVisible = computed(() => {
  if (state.isSubagent || state.isScheduled) return false
  const cur = currentSessionObj.value
  return !!cur && !cur.parent_session
})
const projectSelValue = computed(() => currentSessionObj.value?.project_id ?? '')

async function handleProjectChange(e: Event) {
  const v = (e.target as HTMLSelectElement).value
  try {
    await setSessionProject(v || null)
  } catch (err) {
    alert(err instanceof Error ? err.message : String(err))
  }
}

// File tree roots: the session's working dir plus the authorized dirs. The
// key is a joined string so the tree only rebuilds when they actually change
// (the sessions list itself is polled every 3 s).
const rootsKey = computed(() => {
  const cur = currentSessionObj.value
  return cur ? [cur.working_dir, ...state.additionalDirs].join('\n') : ''
})
const fsRoots = ref<FsRoot[]>([])
watch(rootsKey, (key) => {
  if (!key) {
    fsRoots.value = []
    return
  }
  const parts = key.split('\n')
  fsRoots.value = parts.map((p) => ({ path: p, name: basename(p) }))
}, { immediate: true })

function basename(p: string): string {
  const parts = p.split('/').filter(Boolean)
  return parts[parts.length - 1] || p
}

// Drag the divider between the session list and the bottom panel.
function startResize(e: MouseEvent) {
  e.preventDefault()
  const startY = e.clientY
  const startH = sessionsHeight.value
  const onMove = (ev: MouseEvent) => {
    sessionsHeight.value = Math.min(600, Math.max(100, startH + (ev.clientY - startY)))
  }
  const onUp = () => {
    window.removeEventListener('mousemove', onMove)
    window.removeEventListener('mouseup', onUp)
    document.body.style.cursor = ''
    document.body.style.userSelect = ''
  }
  document.body.style.cursor = 'row-resize'
  document.body.style.userSelect = 'none'
  window.addEventListener('mousemove', onMove)
  window.addEventListener('mouseup', onUp)
}

// Refresh the tree after the file manager saves a file.
function reloadTreeDir(path: string) {
  treeRef.value?.reloadDir(path)
}

defineExpose({ reloadTreeDir })

// File-changing tool completions (edit/write/bash/task; plus subagent updates
// and run ends) bump fsRefreshTick; reload the tree so files the agent
// created or modified show up without waiting for the session poll. Debounced
// so a burst of tool completions collapses into a single reload. Read-only
// tools (read/glob/grep/todo) and new assistant turns do not trigger a
// reload - they change no files.
let refreshTimer: ReturnType<typeof setTimeout> | null = null
watch(
  () => state.fsRefreshTick,
  () => {
    if (refreshTimer) clearTimeout(refreshTimer)
    refreshTimer = setTimeout(() => treeRef.value?.reloadAll(), 1000)
  },
)

interface SessionNode extends Session {
  children: Session[]
}

// Flat render list for the sidebar: a project folder row (with its member
// sessions nested under it when expanded) followed by every top-level
// session and its subagent children. Projects come first, in creation
// order; unassigned sessions follow. Each row carries its persistent
// status dot (running > pending cron > idle).
type SessionRow =
  | { key: string; kind: 'project'; project: Project; children: Session[]; dot: SessionStatus }
  | { key: string; kind: 'session'; session: SessionNode; children: Session[]; project: boolean; dot: SessionStatus }

function isProjectCollapsed(id: string): boolean {
  return collapsedProjects.value.has(id)
}

function toggleProject(id: string) {
  const s = new Set(collapsedProjects.value)
  if (s.has(id)) s.delete(id)
  else s.add(id)
  collapsedProjects.value = s
}

function isSessionExpanded(id: string): boolean {
  return expandedSessions.value.has(id)
}

// Type-guard variant for templates, where vue-tsc loses the v-else-if
// narrowing on union rows inside nested v-if elements.
function sessionChildrenExpanded(item: SessionRow): boolean {
  return item.kind === 'session' && isSessionExpanded(item.session.id)
}

function toggleSessionChildren(id: string) {
  const s = new Set(expandedSessions.value)
  if (s.has(id)) s.delete(id)
  else s.add(id)
  expandedSessions.value = s
}

const sessionRows = computed<SessionRow[]>(() => {
  const all = state.sessions
  const childrenByParent = new Map<string, Session[]>()
  for (const s of all) {
    if (s.parent_session) {
      const arr = childrenByParent.get(s.parent_session) ?? []
      arr.push(s)
      childrenByParent.set(s.parent_session, arr)
    }
  }
  const toNode = (s: Session): SessionNode => ({
    ...s,
    children: (childrenByParent.get(s.id) ?? []).slice().sort((a, b) => {
      // Newest subagents on top: descending by created_at, with missing
      // timestamps (legacy sessions) pushed to the bottom.
      return (b.created_at ?? 0) - (a.created_at ?? 0)
    }),
  })
  // Running sessions first, then sessions with a schedule still to fire
  // (blue dot), then by last activity (updated_at descending, history.json
  // mtime set by the server; missing timestamps of legacy sessions count
  // as zero), then by name.
  const sortTop = (list: Session[]) =>
    list.slice().sort((a, b) => {
      const rank = (s: Session) => (s.is_running ? 2 : s.has_schedule ? 1 : 0)
      const byRank = rank(b) - rank(a)
      if (byRank !== 0) return byRank
      const byTime = (b.updated_at ?? b.created_at ?? 0) - (a.updated_at ?? a.created_at ?? 0)
      if (byTime !== 0) return byTime
      return a.name.localeCompare(b.name)
    })

  const rows: SessionRow[] = []
  const projectIds = new Set(state.projects.map((p) => p.id))
  for (const p of state.projects.slice().sort((a, b) =>
    (a.created_at ?? 0) - (b.created_at ?? 0) || a.name.localeCompare(b.name),
  )) {
    const members = all.filter((s) => !s.parent_session && s.project_id === p.id)
    rows.push({ key: 'p:' + p.id, kind: 'project', project: p, children: [], dot: projectStatus(members) })
    if (!isProjectCollapsed(p.id)) {
      for (const node of sortTop(members).map(toNode)) {
        rows.push({ key: node.id, kind: 'session', session: node, children: node.children, project: true, dot: sessionStatus(node) })
      }
    }
  }
  // Sessions referencing a deleted/unknown project render as unassigned.
  const free = all.filter(
    (s) => !s.parent_session && !(s.project_id && projectIds.has(s.project_id)),
  )
  for (const node of sortTop(free).map(toNode)) {
    rows.push({ key: node.id, kind: 'session', session: node, children: node.children, project: false, dot: sessionStatus(node) })
  }
  return rows
})

onMounted(() => {
  resetDefaults()
  getVersion().then((v) => (version.value = v)).catch(() => {})
})

function startEditName() {
  editName.value = currentSessionObj.value?.name ?? ''
  editingName.value = true
}

async function saveName() {
  const newName = editName.value.trim()
  editingName.value = false
  if (newName && newName !== currentSessionObj.value?.name) {
    await renameSession(newName)
  }
}

function cancelName() {
  editingName.value = false
}

async function handleSelect(id: string) {
  editingName.value = false
  await selectSession(id)
  emit('select')
}

async function handleCreate() {
  if (!name.value.trim() || !workingDir.value.trim()) {
    alert('Please fill in both fields')
    return
  }
  try {
    await createSession(name.value.trim(), workingDir.value.trim())
    resetDefaults()
  } catch (e) {
    alert(e instanceof Error ? e.message : String(e))
  }
}

// Projects reuse the header's name + working directory fields.
async function handleCreateProject() {
  if (!name.value.trim() || !workingDir.value.trim()) {
    alert('Please fill in both fields')
    return
  }
  try {
    await createProject(name.value.trim(), workingDir.value.trim())
    resetDefaults()
  } catch (e) {
    alert(e instanceof Error ? e.message : String(e))
  }
}

// Create a session that belongs to the project, using the project's folder
// and name as the defaults.
async function handleCreateInProject(p: Project) {
  try {
    await createSession(p.name, p.working_dir, p.id)
    resetDefaults()
  } catch (e) {
    alert(e instanceof Error ? e.message : String(e))
  }
}

async function handleDeleteProject(p: Project) {
  if (!confirm(`Delete project "${p.name}"? Its sessions are kept (unassigned).`)) return
  try {
    await deleteProject(p.id)
  } catch (e) {
    alert(e instanceof Error ? e.message : String(e))
  }
}

async function resetDefaults() {
  try {
    const cwd = await getCwd()
    workingDir.value = cwd
    name.value = cwd.split('/').pop() || cwd
  } catch {
    name.value = ''
    workingDir.value = ''
  }
}

async function handleDelete(id: string) {
  const s = state.sessions.find((x) => x.id === id)
  const label = s?.description || s?.name || id
  if (!confirm(`Delete session "${label}"?`)) return
  await deleteSession(id)
}

async function handleAddDir() {
  const path = newDir.value.trim()
  if (!path) return
  await addDir(path)
  newDir.value = ''
}

// ── allowed directories (group-scoped) ─────────────────────────────────────
// Allowed directories are per-session, but a subagent inherits the parent's
// grants and may add its own, so the same path can legitimately live in
// several sessions of the group. Show the union of the current main-session
// group deduped by path instead, with the owning session labelled on entries
// that do not belong to the session being viewed. The + box still adds to
// the viewed session only.
interface GroupDirEntry {
  sessionId: string
  path: string
  source: string | null
}

// Trailing slashes are stripped so `/foo/` and `/foo` (legacy data written
// before dirs were canonicalized server-side) collapse into one entry.
function dirKey(p: string): string {
  return p.replace(/\/+$/, '') || p
}

function sessionLabel(s: Session): string {
  return s.subagent_type ? `[${s.subagent_type}: ${s.description || s.id}]` : s.name
}

const groupDirs = computed<GroupDirEntry[]>(() => {
  const cur = state.currentSession
  if (!cur) return []
  const root = findSessionRoot(state.sessions, cur)
  if (!root) return []
  const group = state.sessions.filter((s) => s.id === root || s.parent_session === root)
  const byPath = new Map<string, GroupDirEntry>()
  const entries: GroupDirEntry[] = []
  for (const s of group) {
    for (const p of s.additional_dirs ?? []) {
      const key = dirKey(p)
      const existing = byPath.get(key)
      if (existing) {
        // Same directory granted to several sessions: prefer the entry owned
        // by the viewed session (no badge); otherwise merge the owner label.
        if (s.id === cur) {
          existing.sessionId = s.id
          existing.source = null
        } else if (existing.sessionId !== cur && existing.source) {
          existing.source += ` + ${sessionLabel(s)}`
        }
        continue
      }
      const entry: GroupDirEntry = {
        sessionId: s.id,
        path: key,
        source: s.id === cur ? null : sessionLabel(s),
      }
      byPath.set(key, entry)
      entries.push(entry)
    }
  }
  return entries
})

async function handleRemoveGroupDir(entry: GroupDirEntry) {
  // The entry may represent the same dir granted to several sessions in the
  // group; remove it from every session that holds it so it does not
  // reappear through another owner. The server canonicalizes paths, so a
  // legacy `/foo/` entry is matched and removed too.
  const root = findSessionRoot(state.sessions, state.currentSession)
  if (!root) return
  const group = state.sessions.filter((s) => s.id === root || s.parent_session === root)
  for (const s of group) {
    const dirs = await apiRemoveDir(s.id, entry.path)
    if (s.id === state.currentSession) state.additionalDirs = dirs
    else s.additional_dirs = dirs
  }
}
</script>
