<template>
  <div class="app" :class="{ 'sidebar-open': sidebarOpen }">
    <div class="sidebar-backdrop" @click="sidebarOpen = false"></div>
    <SessionSidebar @select="sidebarOpen = false" />
    <main class="main">
      <button class="menu-toggle" @click="sidebarOpen = true">&#9776;</button>
      <ChatView />
      <ChatInput />
      <TokenBar />
    </main>
  </div>
</template>

<script setup lang="ts">
import { ref, watch } from 'vue'
import { useStore } from './store'
import SessionSidebar from './components/SessionSidebar.vue'
import ChatView from './components/ChatView.vue'
import ChatInput from './components/ChatInput.vue'
import TokenBar from './components/TokenBar.vue'

const { state, loadSessions } = useStore()
const sidebarOpen = ref(false)

watch(() => state.currentSession, () => {
  sidebarOpen.value = false
})

loadSessions()
</script>
