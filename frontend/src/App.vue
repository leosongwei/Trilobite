<template>
  <div v-if="authState !== 'ok'" class="auth-screen">
    <form v-if="authState === 'required'" class="auth-card" @submit.prevent="submitAuth">
      <h1>Trilobite</h1>
      <p class="auth-hint">Enter the access key printed when the server started.</p>
      <input
        v-model="authKey"
        type="password"
        placeholder="access key"
        autofocus
        autocomplete="off"
      />
      <button type="submit" :disabled="authSubmitting">Unlock</button>
      <p v-if="authError" class="auth-error">{{ authError }}</p>
    </form>
  </div>
  <div v-else class="app" :class="{ 'sidebar-open': sidebarOpen }">
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
      <template v-if="state.currentSession">
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
      </template>
    </main>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch, onMounted, onUnmounted } from 'vue'
import { useStore } from './store'
import * as api from './api'
import SessionSidebar from './components/SessionSidebar.vue'
import ChatView from './components/ChatView.vue'
import ChatInput from './components/ChatInput.vue'
import TokenBar from './components/TokenBar.vue'

const { state, loadSessions, setMode, approvePlanExit, rejectPlanExit, approvePermission, rejectPermission, approveSubagentPermission, rejectSubagentPermission, selectSession } = useStore()
const sidebarOpen = ref(false)

// Access-key gate: 'checking' while probing the server, 'required' shows the
// key dialog, 'ok' renders the app.
const authState = ref<'checking' | 'required' | 'ok'>('checking')
const authKey = ref('')
const authError = ref('')
const authSubmitting = ref(false)

const parentSession = computed(() => {
  const cur = state.sessions.find((s) => s.id === state.currentSession)
  return cur?.parent_session ?? null
})

function goParent() {
  if (parentSession.value) selectSession(parentSession.value)
}

watch(() => state.currentSession, () => {
  sidebarOpen.value = false
})

function handleKeydown(e: KeyboardEvent) {
  if (authState.value !== 'ok') return
  if (e.key === 'Tab' && !state.isSubagent) {
    e.preventDefault()
    setMode(state.planMode ? 'build' : 'plan')
  }
}

async function init() {
  // Opening the printed link (?token=...) exchanges the token for the session
  // cookie right away, then strips it from the URL so it does not linger in
  // the address bar / browser history.
  const token = new URLSearchParams(location.search).get('token')
  if (token) {
    try {
      await api.login(token)
      history.replaceState({}, '', location.pathname)
    } catch {
      // Stale token (server restarted): fall through to the dialog.
    }
  }
  const { authenticated } = await api.getAuthStatus()
  if (authenticated) {
    authState.value = 'ok'
    loadSessions()
  } else {
    authState.value = 'required'
  }
}

async function submitAuth() {
  authError.value = ''
  authSubmitting.value = true
  try {
    await api.login(authKey.value.trim())
    authState.value = 'ok'
    authKey.value = ''
    loadSessions()
  } catch {
    authError.value = 'Invalid key'
  } finally {
    authSubmitting.value = false
  }
}

function showAuthDialog() {
  authState.value = 'required'
}

onMounted(() => {
  document.addEventListener('keydown', handleKeydown)
  window.addEventListener('trilobite:unauthorized', showAuthDialog)
  init()
})
onUnmounted(() => {
  document.removeEventListener('keydown', handleKeydown)
  window.removeEventListener('trilobite:unauthorized', showAuthDialog)
})
</script>
