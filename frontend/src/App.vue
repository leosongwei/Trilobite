<template>
  <div class="app">
    <SessionSidebar />
    <main class="main">
      <ChatView />
      <ChatInput />
      <TokenBar />
    </main>
  </div>
</template>

<script setup lang="ts">
import { useStore } from './store'
import SessionSidebar from './components/SessionSidebar.vue'
import ChatView from './components/ChatView.vue'
import ChatInput from './components/ChatInput.vue'
import TokenBar from './components/TokenBar.vue'

const { loadSessions } = useStore()
loadSessions()
</script>
