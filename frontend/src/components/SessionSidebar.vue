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
        @click="selectSession(s.name)"
      >
        <span>{{ s.name }}</span>
        <span class="delete" @click.stop="handleDelete(s.name)">&times;</span>
      </div>
    </div>
  </aside>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import { useStore } from '../store'

const { state, selectSession, createSession, deleteSession } = useStore()
const name = ref('')
const workingDir = ref('')

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
</script>
