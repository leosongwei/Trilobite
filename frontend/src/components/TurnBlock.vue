<template>
  <div class="turn-block">
    <ThinkingBlock
      v-if="turn.thinking"
      :content="turn.thinking"
    />
    <div
      v-if="turn.text"
      class="message assistant markdown-body"
      v-html="renderedText"
    ></div>
    <ToolEntry
      v-for="(tool, idx) in turn.tools"
      :key="idx"
      :tool="tool"
    />
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import type { TurnItem } from '../types'
import ThinkingBlock from './ThinkingBlock.vue'
import ToolEntry from './ToolEntry.vue'
import { renderMarkdown } from '../utils/markdown'

const props = defineProps<{ turn: TurnItem; streaming?: boolean }>()

const renderedText = computed(() => renderMarkdown(props.turn.text))
</script>
