<template>
  <div class="tool-entry">
    <template v-if="isTask">
      <div class="tool-action">
        [task: {{ taskCount }} subagent{{ taskCount === 1 ? '' : 's' }}]<span v-if="tool.status === 'running'"> running...</span>
      </div>
      <div class="subagent-tree">
        <div
          v-for="c in subagents"
          :key="c.session"
          class="subagent-node"
          @click="openChild(c.session)"
          :title="`Open subagent session: ${c.description || c.session}`"
        >
          <span class="subagent-type" :class="c.type">{{ c.type }}</span>
          <span class="subagent-desc">{{ c.description }}</span>
          <span class="subagent-state" :class="c.state">{{ stateLabel(c.state) }}</span>
        </div>
      </div>
    </template>
    <details v-else-if="isRead" class="tool-collapsible">
      <summary class="tool-action">
        [{{ label }}]<span v-if="tool.status === 'running'"> running...</span>
      </summary>
      <pre class="tool-result">{{ displayContent }}</pre>
    </details>
    <template v-else>
      <div class="tool-action">
        [{{ label }}]<span v-if="tool.status === 'running'"> running...</span>
      </div>
      <DiffView v-if="tool.diff" :rows="tool.diff" />
      <pre v-else-if="tool.diffPrev" class="tool-result">{{ tool.diffCurrent ?? tool.diffPrev }}</pre>
      <pre v-else class="tool-result">{{ displayContent }}</pre>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import type { ToolDisplay } from '../types'
import { useStore } from '../store'
import DiffView from './DiffView.vue'

const props = defineProps<{ tool: ToolDisplay }>()
const { selectSession } = useStore()

const isRead = computed(() => props.tool.name === 'read')
const isTask = computed(() => props.tool.name === 'task')

const subagents = computed(() => props.tool.subagents ?? [])

const taskCount = computed(() => {
  const tasks = props.tool.startArgs?.tasks
  return Array.isArray(tasks) ? tasks.length : subagents.value.length
})

function stateLabel(s: string): string {
  if (s === 'running') return 'running'
  if (s === 'completed') return 'done'
  if (s === 'interrupted') return 'interrupted'
  if (s === 'error') return 'error'
  return s
}

function openChild(session: string) {
  selectSession(session)
}

const label = computed(() => {
  const args = props.tool.startArgs
  if (!args) return props.tool.name
  if (props.tool.name === 'read' && args.filename) {
    return `read: ${args.filename}`
  }
  if (props.tool.name === 'bash' && args.command) {
    return `bash: ${args.command}`
  }
  if (props.tool.name === 'edit' && args.filename) {
    return `edit: ${args.filename}`
  }
  if (props.tool.name === 'write' && args.filename) {
    return `write: ${args.filename}`
  }
  if (props.tool.name === 'glob' && args.pattern) {
    return args.path ? `glob: ${args.pattern} in ${args.path}` : `glob: ${args.pattern}`
  }
  if (props.tool.name === 'grep' && args.pattern) {
    let s = `grep: ${args.pattern}`
    if (args.glob) s += ` (${args.glob})`
    if (args.path) s += ` in ${args.path}`
    return s
  }
  return props.tool.name
})

const displayContent = computed(() => {
  if (props.tool.status === 'done') {
    return props.tool.result ?? ''
  }
  if (props.tool.status === 'streaming') {
    return props.tool.args || '...'
  }
  // running: show live streamed output (bash) if available
  if (props.tool.liveOutput) {
    return props.tool.liveOutput
  }
  return props.tool.args || 'running...'
})
</script>
