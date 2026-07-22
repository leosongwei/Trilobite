<template>
  <div class="token-bar" :class="barClass">{{ barText }}</div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useStore } from '../store'

const { state } = useStore()

const barText = computed(() => {
  if (state.maxTokens > 0) {
    const pct = ((state.tokenCount / state.maxTokens) * 100).toFixed(1)
    return `Tokens: ${state.tokenCount.toLocaleString()} / ${state.maxTokens.toLocaleString()} (${pct}%)`
  }
  if (state.tokenCount > 0) {
    return `Tokens: ${state.tokenCount.toLocaleString()}`
  }
  return 'Tokens: \u2014'
})

const barClass = computed(() => {
  if (state.maxTokens > 0) {
    if (state.tokenCount >= state.maxTokens * 0.9) return 'danger'
    if (state.tokenCount >= state.maxTokens * 0.7) return 'warn'
  }
  return ''
})
</script>
