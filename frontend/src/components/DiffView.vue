<template>
  <div ref="root" class="diff-view-wrap">
    <table v-if="mode === 'split'" class="diff-view diff-view-split">
      <tbody>
        <tr v-for="(pair, idx) in splitRows" :key="idx" class="diff-row">
          <td class="diff-ln" :class="pair.left.type">{{ pair.left.lineNo ?? '' }}</td>
          <td class="diff-text" :class="pair.left.type">{{ pair.left.text }}</td>
          <td class="diff-ln" :class="pair.right.type">{{ pair.right.lineNo ?? '' }}</td>
          <td class="diff-text" :class="pair.right.type">{{ pair.right.text }}</td>
        </tr>
      </tbody>
    </table>
    <table v-else class="diff-view diff-view-unified">
      <tbody>
        <tr v-for="(row, idx) in rows" :key="idx" class="diff-row" :class="row.type">
          <td class="diff-ln">{{ row.type === 'removed' ? row.old : row.new }}</td>
          <td class="diff-sign">{{ row.type === 'added' ? '+' : row.type === 'removed' ? '−' : '' }}</td>
          <td class="diff-text">{{ row.text }}</td>
        </tr>
      </tbody>
    </table>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import type { DiffRow } from '../types'

const props = defineProps<{ rows: DiffRow[] }>()

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
  const rows = props.rows
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

// Below this width the two side-by-side panes get too cramped; fall back to a
// single-column unified view (one line-number column + +/- markers).
const SPLIT_BREAKPOINT = 560
const root = ref<HTMLElement | null>(null)
const mode = ref<'split' | 'unified'>('split')
let observer: ResizeObserver | null = null

onMounted(() => {
  if (!root.value) return
  const update = () => {
    if (!root.value) return
    mode.value = root.value.clientWidth <= SPLIT_BREAKPOINT ? 'unified' : 'split'
  }
  update()
  observer = new ResizeObserver(update)
  observer.observe(root.value)
})
onBeforeUnmount(() => observer?.disconnect())
</script>

<style scoped>
.diff-view-wrap { width: 100%; }

.diff-view {
  margin: 2px 0 4px;
  border-collapse: collapse;
  width: 100%;
  table-layout: fixed;
  font-size: 12px;
  font-family: var(--mono-font, ui-monospace, SFMono-Regular, Menlo, Consolas, monospace);
  background: #1e1e1e;
  border-radius: 3px;
  overflow: hidden;
}
.diff-view .diff-row { display: table-row; }
.diff-view .diff-ln {
  color: #6e7681;
  text-align: right;
  padding: 0 6px;
  white-space: nowrap;
  user-select: none;
  vertical-align: top;
  overflow: hidden;
}
.diff-view .diff-text {
  white-space: pre-wrap;
  word-break: break-word;
  padding: 0 8px;
  color: #d4d4d4;
  vertical-align: top;
}

/* --- split (two-pane) layout --- */
.diff-view-split .diff-ln { width: 44px; }
/* Divider between the old (left) and new (right) panes. */
.diff-view-split .diff-ln.new { border-left: 1px solid #33363b; }
.diff-view-split .removed { background: rgba(248, 81, 73, 0.14); }
.diff-view-split .diff-text.removed { color: #f08888; }
.diff-view-split .added { background: rgba(63, 185, 80, 0.14); }
.diff-view-split .diff-text.added { color: #4ec98a; }
.diff-view-split .empty { background: rgba(110, 118, 129, 0.07); }
.diff-view-split .diff-ln.removed,
.diff-view-split .diff-ln.added { color: #8b949e; }

/* --- unified (single-column) layout --- */
.diff-view-unified .diff-ln { width: 40px; }
.diff-view-unified .diff-sign {
  width: 16px;
  text-align: center;
  user-select: none;
  vertical-align: top;
  color: #6e7681;
}
.diff-view-unified tr.added { background: rgba(63, 185, 80, 0.14); }
.diff-view-unified tr.removed { background: rgba(248, 81, 73, 0.14); }
.diff-view-unified tr.added .diff-text,
.diff-view-unified tr.added .diff-sign { color: #4ec98a; }
.diff-view-unified tr.removed .diff-text,
.diff-view-unified tr.removed .diff-sign { color: #f08888; }
.diff-view-unified tr.added .diff-ln,
.diff-view-unified tr.removed .diff-ln { color: #8b949e; }
</style>
