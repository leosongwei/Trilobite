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
      :base="diffBase"
      :sidebar-width="sidebarWidth"
      :requests-tick="requestsTick"
      @select="sidebarOpen = false"
      @open-file="handleOpenFile"
      @root-info="(info) => { rootInfoMap[info.path] = info }"
    />
    <div class="sidebar-width-resizer" title="Drag to resize" @mousedown="startWidthResize"></div>
    <main class="main">
      <button class="menu-toggle" @click="sidebarOpen = true"><span class="ms ms-menu"></span></button>
      <FileManager
        v-if="showFiles && state.currentSession"
        :session-id="state.currentSession"
        :file="openedFile"
        :root-info="rootInfoMap"
        :base="diffBase"
        @close="showFiles = false"
        @file-saved="(dir) => sidebarRef?.reloadTreeDir(dir)"
        @update:base="(b) => { diffBase = b }"
      />
      <template v-else>
        <div v-if="state.isSubagent" class="subagent-bar">
          <button class="back-btn" @click="goParent"><span class="ms ms-back"></span> parent</button>
          <span class="subagent-tag" :class="state.subagentType">{{ state.subagentType }}</span>
          <span class="subagent-title">{{ state.subagentDescription || state.currentSession }}</span>
          <span v-if="state.sealed" class="sealed-label">finished (read-only)</span>
        </div>
        <div v-if="currentSessionInfo?.has_sleep" class="sleep-banner">
          <span class="sleep-text">⏳ 挂起至 {{ formatSleepUntil(currentSessionInfo.sleep_until) }}</span>
          <button class="wake-btn" :disabled="state.isStreaming" title="结束挂起，立即继续对话" @click="wakeNow">立即唤醒</button>
        </div>
        <ChatView />
        <template v-if="state.currentSession">
          <div v-if="bannerRequest" class="plan-exit-banner">
            <span>{{ bannerText }}</span>
            <button class="approve" @click="approveRequest(bannerRequest)">{{ bannerApproveLabel }}</button>
            <button class="reject" @click="rejectRequest(bannerRequest)">{{ bannerRejectLabel }}</button>
          </div>
          <div v-else-if="groupPendingCount > 1" class="plan-exit-banner">
            <span>{{ groupPendingCount }} permission requests are pending</span>
            <button class="approve" @click="openRequestsList">Review</button>
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
import type { PendingRequest } from './types'
import { findSessionRoot } from './utils/sessions'

const { state, loadSessions, setMode, approveRequest, rejectRequest, selectSession } = useStore()

// The current session's full record from the session poll; a session
// suspended via sleep_until carries has_sleep/sleep_until which the
// suspension banner renders.
const currentSessionInfo = computed(() =>
  state.sessions.find((s) => s.id === state.currentSession) ?? null,
)

function formatSleepUntil(ts?: number | null): string {
  if (!ts) return '-'
  return new Date(ts * 1000).toLocaleString()
}

async function wakeNow() {
  const info = currentSessionInfo.value
  if (!info?.has_sleep) return
  try {
    await api.wakeSession(info.id)
  } catch (e) {
    alert(e instanceof Error ? e.message : String(e))
  }
}
const sidebarOpen = ref(false)
// Sidebar width, draggable on desktop and persisted across reloads. On mobile
// the CSS overrides the width (drawer) and the resizer is hidden.
const sidebarWidth = ref(Number(localStorage.getItem('trilobite.sidebarWidth')) || 260)
const showFiles = ref(false)
const sidebarRef = ref<InstanceType<typeof SessionSidebar> | null>(null)
const openedFile = ref<OpenFilePayload | null>(null)
// Diff base branch, shared by the sidebar tree (change highlighting) and the
// file manager (diff view + branch selector).
const diffBase = ref('master')
// Git info per workspace root, collected from the sidebar tree's root-info
// events; the file manager needs it for the diff branch selector.
const rootInfoMap = reactive<Record<string, RootInfo>>({})

// ── permission banners ─────────────────────────────────────────────────────
// The banner shows pending requests of the current main-session group (the
// main session and its subagents), no matter which session is being viewed,
// so a subagent's request is visible while browsing a sibling.
function sessionRoot(id: string | null): string | null {
  return findSessionRoot(state.sessions, id)
}

const groupPending = computed<PendingRequest[]>(() => {
  const root = sessionRoot(state.currentSession)
  if (!root) return []
  return state.pendingRequests.filter((r) => sessionRoot(r.session) === root)
})

const groupPendingCount = computed(() => groupPending.value.length)

// One pending request: show its details with Approve/Reject. Several: collapse
// to a count and point at the sidebar Requests list.
const bannerRequest = computed(() =>
  groupPending.value.length === 1 ? groupPending.value[0] : null,
)

const bannerText = computed(() => {
  const r = bannerRequest.value
  if (!r) return ''
  if (r.kind === 'plan_exit') return 'Agent requests to switch to Build mode'
  if (r.childType) {
    return `Subagent [${r.childType}: ${r.childDescription}] needs access to: ${r.path}`
  }
  return `Agent needs access to: ${r.path}`
})

const bannerApproveLabel = computed(() =>
  bannerRequest.value?.kind === 'plan_exit' ? 'Approve' : 'Grant',
)
const bannerRejectLabel = computed(() =>
  bannerRequest.value?.kind === 'plan_exit' ? 'Reject' : 'Deny',
)

// The aggregated banner's Review button opens the sidebar and expands the
// Requests list (SessionSidebar watches the tick).
const requestsTick = ref(0)
function openRequestsList() {
  sidebarOpen.value = true
  requestsTick.value++
}

// Clicking a file in the sidebar tree opens it in the file manager view.
function handleOpenFile(f: OpenFilePayload) {
  openedFile.value = f
  showFiles.value = true
  sidebarOpen.value = false
}

// Drag the divider between the sidebar and the chat to resize the sidebar
// width. Clamped to keep the chat usable (min 200 px) and the sidebar under
// half the viewport.
function startWidthResize(e: MouseEvent) {
  e.preventDefault()
  const startX = e.clientX
  const startW = sidebarWidth.value
  const onMove = (ev: MouseEvent) => {
    sidebarWidth.value = Math.min(
      Math.max(startW + (ev.clientX - startX), 200),
      Math.round(window.innerWidth * 0.4),
    )
  }
  const onUp = () => {
    window.removeEventListener('mousemove', onMove)
    window.removeEventListener('mouseup', onUp)
    document.body.style.cursor = ''
    document.body.style.userSelect = ''
    localStorage.setItem('trilobite.sidebarWidth', String(sidebarWidth.value))
  }
  document.body.style.cursor = 'col-resize'
  document.body.style.userSelect = 'none'
  window.addEventListener('mousemove', onMove)
  window.addEventListener('mouseup', onUp)
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
