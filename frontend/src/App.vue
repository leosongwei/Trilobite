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
    <SessionSidebar
      ref="sidebarRef"
      :view="viewMode"
      :base="diffBase"
      @select="sidebarOpen = false"
      @open-file="handleOpenFile"
      @root-info="(info) => { rootInfoMap[info.path] = info }"
      @view-click="(v) => { viewMode = v }"
    />
    <main class="main">
      <button class="menu-toggle" @click="sidebarOpen = true">&#9776;</button>
      <FileManager
        v-if="showFiles && state.currentSession"
        :session-id="state.currentSession"
        :file="openedFile"
        :root-info="rootInfoMap"
        :view="viewMode"
        :base="diffBase"
        @close="showFiles = false"
        @file-saved="(dir) => sidebarRef?.reloadTreeDir(dir)"
        @update:view="(v) => { viewMode = v }"
        @update:base="(b) => { diffBase = b }"
      />
      <template v-else>
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
      </template>
    </main>
  </div>
</template>

<script setup lang="ts">
import { computed, reactive, ref, watch, onMounted, onUnmounted } from 'vue'
import { useStore } from './store'
import * as api from './api'
import SessionSidebar from './components/SessionSidebar.vue'
import ChatView from './components/ChatView.vue'
import ChatInput from './components/ChatInput.vue'
import TokenBar from './components/TokenBar.vue'
import FileManager from './components/FileManager.vue'
import type { RootInfo } from './components/FileManager.vue'
import type { OpenFilePayload } from './components/FileTree.vue'

const { state, loadSessions, setMode, approvePlanExit, rejectPlanExit, approvePermission, rejectPermission, approveSubagentPermission, rejectSubagentPermission, selectSession } = useStore()
const sidebarOpen = ref(false)
const showFiles = ref(false)
const sidebarRef = ref<InstanceType<typeof SessionSidebar> | null>(null)
const openedFile = ref<OpenFilePayload | null>(null)
// View mode and diff base branch are shared by the sidebar tree (mode tabs,
// diff highlighting) and the file manager (content rendering).
const viewMode = ref<'view' | 'diff' | 'edit'>('diff')
const diffBase = ref('master')
// Git info per workspace root, collected from the sidebar tree's root-info
// events; the file manager needs it for the diff branch selector.
const rootInfoMap = reactive<Record<string, RootInfo>>({})

// Clicking a file in the sidebar tree opens it in the file manager view.
function handleOpenFile(f: OpenFilePayload) {
  openedFile.value = f
  showFiles.value = true
  sidebarOpen.value = false
}

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
  showFiles.value = false
  openedFile.value = null
  viewMode.value = 'diff'
  diffBase.value = 'master'
  for (const k of Object.keys(rootInfoMap)) delete rootInfoMap[k]
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
