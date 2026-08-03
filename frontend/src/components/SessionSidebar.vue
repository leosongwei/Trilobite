<template>
  <aside class="sidebar">
    <div class="sidebar-header">
      <h1>Trilobite<span v-if="version" class="version"> v{{ version }}</span></h1>
      <input v-model="name" type="text" placeholder="Session name" />
      <label>Working directory:</label>
      <input v-model="workingDir" type="text" placeholder="/home/user/project" />
      <button @click="handleCreate">+ New Session</button>
    </div>
    <div class="sessions" :style="{ height: sessionsHeight + 'px' }">
      <template v-for="s in sessionTree" :key="s.id">
        <div
          class="session-item"
          :class="{ active: s.id === state.currentSession }"
          @click="handleSelect(s.id)"
        >
          <span class="session-label">
            <span v-if="s.is_running" class="running-dot" title="running"></span>
            {{ s.name }}
          </span>
          <span class="delete" @click.stop="handleDelete(s.id)">&times;</span>
        </div>
        <div
          v-for="c in s.children"
          :key="c.id"
          class="session-item child"
          :class="{ active: c.id === state.currentSession }"
          @click="handleSelect(c.id)"
        >
          <span class="session-label">
            <span v-if="c.is_running" class="running-dot" title="running"></span>
            <span class="child-badge" :class="{ explore: c.subagent_type === 'explore' }" :title="c.subagent_type">{{ (c.subagent_type || '').slice(0, 2) }}</span>
            <span v-if="c.sealed" class="sealed-dot" title="finished"></span>
            {{ c.description || c.name }}
          </span>
        </div>
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
            <button class="icon-btn" title="Save" @click="saveName">&#10003;</button>
            <button class="icon-btn" title="Cancel" @click="cancelName">&times;</button>
          </template>
          <template v-else>
            <span class="info-value" :title="currentSessionName">{{ currentSessionName }}</span>
            <button
              v-if="!state.isSubagent"
              class="icon-btn"
              title="Rename session"
              @click="startEditName"
            >&#9998;</button>
          </template>
        </div>
        <div class="info-row">
          <span class="info-label">cwd:</span>
          <span class="info-value" :title="currentSessionCwd">{{ currentSessionCwd }}</span>
        </div>
        <details>
          <summary>Allowed directories ({{ state.additionalDirs.length }})</summary>
          <div v-for="d in state.additionalDirs" :key="d" class="dir-item">
            <span class="dir-path" :title="d">{{ d }}</span>
            <span class="delete" @click="handleRemoveDir(d)">&times;</span>
          </div>
          <div class="dir-add">
            <input v-model="newDir" type="text" placeholder="/path/to/dir" @keydown.enter="handleAddDir" />
            <button @click="handleAddDir">+</button>
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
import { getCwd, getVersion } from '../api'
import type { Session } from '../types'
import FileTree from './FileTree.vue'
import type { FsRoot } from './FileTree.vue'

const emit = defineEmits<{
  select: []
  'open-file': [file: { path: string; name: string; status?: string }]
  'root-info': [info: { path: string; isGit: boolean; branches: string[]; currentBranch: string }]
}>()

// Diff base branch, shared with the file manager; the tree's change
// highlighting always compares the working tree against it.
const props = defineProps<{ base: string }>()

const { state, selectSession, createSession, deleteSession, addDir, removeDir, renameSession } = useStore()
const name = ref('')
const workingDir = ref('')
const newDir = ref('')
const version = ref('')
const editingName = ref(false)
const editName = ref('')
const sessionsHeight = ref(300)
const treeRef = ref<InstanceType<typeof FileTree> | null>(null)

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

// Tool completions (and subagent updates / run ends) bump fsRefreshTick;
// reload the tree so files the agent created or modified show up without
// waiting for the session poll. Debounced so a burst of tool completions
// collapses into a single reload. New assistant turns alone do not trigger a
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

// Build a one-level tree: top-level sessions with their subagent children
// nested under them.
const sessionTree = computed<SessionNode[]>(() => {
  const all = state.sessions
  const childrenByParent = new Map<string, Session[]>()
  for (const s of all) {
    if (s.parent_session) {
      const arr = childrenByParent.get(s.parent_session) ?? []
      arr.push(s)
      childrenByParent.set(s.parent_session, arr)
    }
  }
  return all
    .filter((s) => !s.parent_session)
    .map((s) => ({
      ...s,
      children: (childrenByParent.get(s.id) ?? []).slice().sort((a, b) => {
        // Newest subagents on top: descending by created_at, with missing
        // timestamps (legacy sessions) pushed to the bottom.
        return (b.created_at ?? 0) - (a.created_at ?? 0)
      }),
    }))
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
  const label = s?.name ?? id
  if (!confirm(`Delete session "${label}"?`)) return
  await deleteSession(id)
}

async function handleAddDir() {
  const path = newDir.value.trim()
  if (!path) return
  await addDir(path)
  newDir.value = ''
}

async function handleRemoveDir(path: string) {
  await removeDir(path)
}
</script>
