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
      <table v-if="tool.diff" class="tool-diff">
        <tbody>
          <tr
            v-for="(row, idx) in tool.diff"
            :key="idx"
            class="diff-row"
            :class="`diff-${row.type}`"
          >
            <td class="diff-ln old">{{ row.old ?? '' }}</td>
            <td class="diff-ln new">{{ row.new ?? '' }}</td>
            <td class="diff-sign">{{ row.type === 'added' ? '+' : row.type === 'removed' ? '−' : '' }}</td>
            <td class="diff-text">{{ row.text }}</td>
          </tr>
        </tbody>
      </table>
      <pre v-else-if="tool.diffPrev" class="tool-result">{{ tool.diffCurrent ?? tool.diffPrev }}</pre>
      <pre v-else class="tool-result">{{ displayContent }}</pre>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import type { ToolDisplay } from '../types'
import { useStore } from '../store'

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
  return props.tool.name
})

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
