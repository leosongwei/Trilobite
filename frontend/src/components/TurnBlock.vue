<template>
  <div class="turn-block">
    <ThinkingBlock
      v-if="turn.thinking"
      :content="turn.thinking"
      :default-open="streaming"
    />
    <div v-if="turn.text" class="message assistant">{{ turn.text }}</div>
    <ToolEntry
      v-for="(tool, idx) in turn.tools"
      :key="idx"
      :tool="tool"
    />
  </div>
</template>

<script setup lang="ts">
import type { TurnItem } from '../types'
import ThinkingBlock from './ThinkingBlock.vue'
import ToolEntry from './ToolEntry.vue'

defineProps<{ turn: TurnItem; streaming?: boolean }>()
</script>
