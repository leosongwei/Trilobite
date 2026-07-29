<template>
  <div class="user-message">
    <template v-if="!editing">
      <button class="user-pencil" @click="startEdit" title="编辑并重发">✎</button>
      <div class="user-content">
        <span class="message user">{{ item.content }}</span>
        <div v-if="item.images?.length" class="user-images">
          <img
            v-for="img in item.images"
            :key="img.filename"
            class="user-image"
            :src="`/api/sessions/${sessionId}/images/${img.filename}`"
            :title="img.original_name"
          />
        </div>
      </div>
    </template>
    <div v-else class="user-edit-wrap">
      <textarea
        ref="taRef"
        v-model="draft"
        class="user-edit-input"
        rows="2"
        @keydown.esc="cancelEdit"
      ></textarea>
      <div class="user-edit-actions">
        <button class="user-edit-btn" @click="confirmEdit" title="确认">✓</button>
        <button class="user-edit-btn" @click="cancelEdit" title="取消">✗</button>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, nextTick } from 'vue'
import type { UserItem } from '../types'
import { useStore } from '../store'

const props = defineProps<{ item: UserItem }>()
const { state, revert } = useStore()
const sessionId = state.currentSession

const editing = ref(false)
const draft = ref('')
const taRef = ref<HTMLTextAreaElement | null>(null)

function startEdit() {
  draft.value = props.item.content
  editing.value = true
  nextTick(() => taRef.value?.focus())
}

function cancelEdit() {
  editing.value = false
}

async function confirmEdit() {
  const seq = props.item.userSeq
  const text = draft.value
  editing.value = false
  if (seq !== undefined && text.trim()) {
    await revert(seq, text)
  }
}
</script>

<style scoped>
.user-content {
  display: block;
  flex: 1;
}
.user-images {
  display: flex;
  gap: 6px;
  margin-top: 6px;
  flex-wrap: wrap;
}
.user-image {
  width: 120px;
  height: 120px;
  object-fit: cover;
  border-radius: 6px;
  border: 1px solid #3c3c3c;
}
</style>
