<template>
  <aside class="sidebar">
    <div class="sidebar-header">
      <h1>Trilobite</h1>
      <input v-model="name" type="text" placeholder="Session name" />
      <label>Working directory:</label>
      <input v-model="workingDir" type="text" placeholder="/home/user/project" />
      <button @click="handleCreate">+ New Session</button>
    </div>
    <div class="sessions">
      <div
        v-for="s in state.sessions"
        :key="s.name"
        class="session-item"
        :class="{ active: s.name === state.currentSession }"
        @click="handleSelect(s.name)"
      >
        <span>{{ s.name }}</span>
        <span class="delete" @click.stop="handleDelete(s.name)">&times;</span>
      </div>
    </div>
    <div v-if="state.currentSession" class="dirs-section">
      <label>Allowed directories:</label>
      <div v-for="d in state.additionalDirs" :key="d" class="dir-item">
        <span class="dir-path" :title="d">{{ d }}</span>
        <span class="delete" @click="handleRemoveDir(d)">&times;</span>
      </div>
      <div class="dir-add">
        <input v-model="newDir" type="text" placeholder="/path/to/dir" @keydown.enter="handleAddDir" />
        <button @click="handleAddDir">+</button>
      </div>
    </div>
  </aside>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import { useStore } from '../store'

const emit = defineEmits<{ select: [] }>()

const { state, selectSession, createSession, deleteSession, addDir, removeDir } = useStore()
const name = ref('')
const workingDir = ref('')
const newDir = ref('')

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
    name.value = ''
    workingDir.value = ''
  } catch (e) {
    alert(e instanceof Error ? e.message : String(e))
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
