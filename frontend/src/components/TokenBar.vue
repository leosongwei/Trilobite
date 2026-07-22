<template>
  <div class="token-bar" :class="barClass">
    <div class="token-bar-fill" :style="{ width: fillPct + '%' }"></div>
    <span class="token-bar-text">{{ barText }}</span>
  </div>
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

const fillPct = computed(() => {
  if (state.maxTokens > 0) {
    return Math.min(100, (state.tokenCount / state.maxTokens) * 100)
  }
  return 0
})

const barClass = computed(() => {
  if (state.maxTokens > 0) {
    if (state.tokenCount >= state.maxTokens * 0.6) return 'danger'
    if (state.tokenCount >= state.maxTokens * 0.3) return 'warn'
    return 'safe'
  }
  return ''
})
</script>
