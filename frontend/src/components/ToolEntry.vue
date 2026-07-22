<template>
  <div class="tool-entry">
    <div class="tool-action">
      [{{ tool.name }}]<span v-if="tool.status === 'running'"> running...</span>
    </div>
    <pre class="tool-result">{{ displayContent }}</pre>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import type { ToolDisplay } from '../types'

const props = defineProps<{ tool: ToolDisplay }>()

const displayContent = computed(() => {
  if (props.tool.status === 'done') {
    return props.tool.result ?? ''
  }
  if (props.tool.status === 'streaming') {
    return props.tool.args || '...'
  }
  return props.tool.args || 'running...'
})
</script>
