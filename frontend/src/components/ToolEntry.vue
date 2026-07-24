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
      <table v-if="tool.diff" class="tool-diff tool-diff-split">
        <tbody>
          <tr v-for="(pair, idx) in splitRows" :key="idx" class="diff-row">
            <td class="diff-ln" :class="pair.left.type">{{ pair.left.lineNo ?? '' }}</td>
            <td class="diff-text" :class="pair.left.type">{{ pair.left.text }}</td>
            <td class="diff-ln" :class="pair.right.type">{{ pair.right.lineNo ?? '' }}</td>
            <td class="diff-text" :class="pair.right.type">{{ pair.right.text }}</td>
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
import type { DiffRow, ToolDisplay } from '../types'
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

interface DiffSide {
  lineNo: number | null
  text: string
  type: DiffRow['type'] | 'empty'
}
interface DiffPair {
  left: DiffSide
  right: DiffSide
}

/**
 * Turn the unified diff rows into side-by-side pairs: left = original file
 * (context + removed lines), right = result file (context + added lines).
 * Consecutive removed/added runs are aligned row-by-row; the shorter side is
 * padded with empty cells so removed and added lines face each other.
 */
const splitRows = computed<DiffPair[]>(() => {
  const rows = props.tool.diff ?? []
  const pairs: DiffPair[] = []
  let removed: DiffRow[] = []
  let added: DiffRow[] = []
  const flush = () => {
    const n = Math.max(removed.length, added.length)
    for (let i = 0; i < n; i++) {
      const l = removed[i]
      const r = added[i]
      pairs.push({
        left: l
          ? { lineNo: l.old, text: l.text, type: 'removed' }
          : { lineNo: null, text: '', type: 'empty' },
        right: r
          ? { lineNo: r.new, text: r.text, type: 'added' }
          : { lineNo: null, text: '', type: 'empty' },
      })
    }
    removed = []
    added = []
  }
  for (const row of rows) {
    if (row.type === 'equal') {
      flush()
      pairs.push({
        left: { lineNo: row.old, text: row.text, type: 'equal' },
        right: { lineNo: row.new, text: row.text, type: 'equal' },
      })
    } else if (row.type === 'removed') {
      removed.push(row)
    } else {
      added.push(row)
    }
  }
  flush()
  return pairs
})
</script>
