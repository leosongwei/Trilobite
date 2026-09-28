<template>
  <div class="group-setup-overlay">
    <div class="group-setup-card">
      <h2>Create group members</h2>
      <p class="hint">
        Pick how many agents join this group chat (1-16). Each member gets its
        own name, its own session under this group, and a send_to_group tool
        to talk to you and to its teammates.
      </p>
      <div class="count-row">
        <button class="step" :disabled="count <= 1 || busy" @click="count--">−</button>
        <span class="count">{{ count }}</span>
        <button class="step" :disabled="count >= 16 || busy" @click="count++">+</button>
      </div>
      <div class="actions">
        <button class="create-btn" :disabled="busy" @click="confirm">
          {{ busy ? 'Creating…' : `Create ${count} agent${count > 1 ? 's' : ''}` }}
        </button>
      </div>
      <p v-if="error" class="error">{{ error }}</p>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import { useStore } from '../store'

const { spawnGroupMembers } = useStore()
const count = ref(3)
const busy = ref(false)
const error = ref('')

async function confirm() {
  busy.value = true
  error.value = ''
  try {
    await spawnGroupMembers(count.value)
  } finally {
    busy.value = false
  }
}
</script>

<style scoped>
.group-setup-overlay {
  position: fixed;
  inset: 0;
  background: var(--overlay);
  z-index: 900;
  display: flex;
  align-items: center;
  justify-content: center;
}
.group-setup-card {
  width: 420px;
  max-width: 92vw;
  background: var(--bg-panel);
  border: 1px solid var(--border);
  border-radius: 6px;
  box-shadow: 0 8px 32px rgba(0, 0, 0, 0.5);
  padding: 16px 18px;
}
.group-setup-card h2 {
  font-size: 14px;
  color: var(--text);
  margin-bottom: 8px;
}
.hint {
  font-size: 11px;
  color: var(--text-muted);
  margin-bottom: 14px;
  line-height: 1.5;
}
.count-row {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 14px;
  margin-bottom: 14px;
}
.count {
  font-size: 22px;
  font-weight: 600;
  color: var(--text-bright);
  min-width: 34px;
  text-align: center;
}
.step {
  width: 30px;
  height: 30px;
  border-radius: 4px;
  border: 1px solid var(--border);
  background: var(--bg-input);
  color: var(--text);
  font-size: 16px;
  cursor: pointer;
}
.step:hover:not(:disabled) { background: var(--bg-input-hover); }
.step:disabled { opacity: 0.4; cursor: default; }
.actions {
  display: flex;
  justify-content: center;
}
.create-btn {
  padding: 6px 18px;
  background: var(--purple-btn-bg);
  color: var(--text-contrast);
  border: none;
  border-radius: 3px;
  font-family: inherit;
  font-size: 12px;
  font-weight: 600;
  cursor: pointer;
}
.create-btn:hover:not(:disabled) { background: var(--purple-btn-hover); }
.create-btn:disabled { opacity: 0.6; cursor: default; }
.error {
  margin-top: 10px;
  font-size: 11px;
  color: var(--danger-text);
  text-align: center;
}
</style>
