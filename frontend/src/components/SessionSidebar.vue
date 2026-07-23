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
      <template v-for="s in sessionTree" :key="s.name">
        <div
          class="session-item"
          :class="{ active: s.name === state.currentSession }"
          @click="handleSelect(s.name)"
        >
          <span class="session-label">
            <span v-if="s.is_running" class="running-dot" title="running"></span>
            {{ s.name }}
          </span>
          <span class="delete" @click.stop="handleDelete(s.name)">&times;</span>
        </div>
        <div
          v-for="c in s.children"
          :key="c.name"
          class="session-item child"
          :class="{ active: c.name === state.currentSession }"
          @click="handleSelect(c.name)"
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
    <div v-if="state.currentSession" class="dirs-section">
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

const { state, selectSession, createSession, deleteSession, addDir, removeDir } = useStore()
const name = ref('')
const workingDir = ref('')
const newDir = ref('')
const version = ref('')

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
      children: (childrenByParent.get(s.name) ?? []).slice().sort((a, b) => {
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

async function handleSelect(sessionName: string) {
  await selectSession(sessionName)
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

async function handleDelete(sessionName: string) {
  if (!confirm(`Delete session "${sessionName}"?`)) return
  await deleteSession(sessionName)
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
