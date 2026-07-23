<template>
  <aside class="sidebar">
    <div class="sidebar-header">
      <h1>Trilobite<span v-if="version" class="version"> v{{ version }}</span></h1>
      <input v-model="name" type="text" placeholder="Session name" />
      <label>Working directory:</label>
      <input v-model="workingDir" type="text" placeholder="/home/user/project" />
      <button @click="handleCreate">+ New Session</button>
    </div>
    <div class="sessions">
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
  </aside>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useStore } from '../store'
import { getCwd, getVersion } from '../api'
import type { Session } from '../types'

const emit = defineEmits<{ select: [] }>()

const { state, selectSession, createSession, deleteSession, addDir, removeDir, renameSession } = useStore()
const name = ref('')
const workingDir = ref('')
const newDir = ref('')
const version = ref('')
const editingName = ref(false)
const editName = ref('')

const currentSessionObj = computed(() =>
  state.sessions.find((s) => s.id === state.currentSession),
)
const currentSessionName = computed(() => {
  if (state.isSubagent) return state.subagentDescription || state.currentSession || ''
  return currentSessionObj.value?.name ?? state.currentSession ?? ''
})
const currentSessionCwd = computed(() => currentSessionObj.value?.working_dir ?? '')

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
