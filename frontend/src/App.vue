<template>
  <div class="app" :class="{ 'sidebar-open': sidebarOpen }">
    <div class="sidebar-backdrop" @click="sidebarOpen = false"></div>
    <SessionSidebar @select="sidebarOpen = false" />
    <main class="main">
      <button class="menu-toggle" @click="sidebarOpen = true">&#9776;</button>
      <div v-if="state.isSubagent" class="subagent-bar">
        <button class="back-btn" @click="goParent">&larr; parent</button>
        <span class="subagent-tag" :class="state.subagentType">{{ state.subagentType }}</span>
        <span class="subagent-title">{{ state.subagentDescription || state.currentSession }}</span>
        <span v-if="state.sealed" class="sealed-label">finished (read-only)</span>
      </div>
      <ChatView />
      <div v-if="state.planExitRequest" class="plan-exit-banner">
        <span>Agent requests to switch to Build mode</span>
        <button class="approve" @click="approvePlanExit">Approve</button>
        <button class="reject" @click="rejectPlanExit">Reject</button>
      </div>
      <div v-if="state.permissionRequest" class="plan-exit-banner">
        <span>Agent needs access to: {{ state.permissionRequest.path }}</span>
        <button class="approve" @click="approvePermission">Grant</button>
        <button class="reject" @click="rejectPermission">Deny</button>
      </div>
      <div v-if="state.subagentPermissionRequest" class="plan-exit-banner">
        <span>Subagent [{{ state.subagentPermissionRequest.childType }}: {{ state.subagentPermissionRequest.childDescription }}] needs access to: {{ state.subagentPermissionRequest.path }}</span>
        <button class="approve" @click="approveSubagentPermission">Grant</button>
        <button class="reject" @click="rejectSubagentPermission">Deny</button>
      </div>
      <ChatInput />
      <TokenBar />
    </main>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch, onMounted, onUnmounted } from 'vue'
import { useStore } from './store'
import SessionSidebar from './components/SessionSidebar.vue'
import ChatView from './components/ChatView.vue'
import ChatInput from './components/ChatInput.vue'
import TokenBar from './components/TokenBar.vue'

const { state, loadSessions, setMode, approvePlanExit, rejectPlanExit, approvePermission, rejectPermission, approveSubagentPermission, rejectSubagentPermission, selectSession } = useStore()
const sidebarOpen = ref(false)

const parentSession = computed(() => {
  const cur = state.sessions.find((s) => s.name === state.currentSession)
  return cur?.parent_session ?? null
})

function goParent() {
  if (parentSession.value) selectSession(parentSession.value)
}

watch(() => state.currentSession, () => {
  sidebarOpen.value = false
})

function handleKeydown(e: KeyboardEvent) {
  if (e.key === 'Tab' && !state.isSubagent) {
    e.preventDefault()
    setMode(state.planMode ? 'build' : 'plan')
  }
}

onMounted(() => document.addEventListener('keydown', handleKeydown))
onUnmounted(() => document.removeEventListener('keydown', handleKeydown))

loadSessions()
</script>
