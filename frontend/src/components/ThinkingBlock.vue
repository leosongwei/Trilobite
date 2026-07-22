<template>
  <details class="thinking" :open="open">
    <summary @click.prevent="open = !open">thinking...</summary>
    <span>{{ content }}</span>
  </details>
</template>

<script setup lang="ts">
import { ref, watch } from 'vue'

const props = defineProps<{ content: string; defaultOpen?: boolean }>()
const open = ref(props.defaultOpen ?? false)

watch(
  () => props.defaultOpen,
  (val) => {
    open.value = val ?? false
  },
)
</script>
