<template>
  <section class="card p-6 mt-6 space-y-4">
    <h2 class="text-xl font-semibold">{{ $t('apiKey.webhookTitle') }}</h2>
    <p v-if="error" role="alert" class="text-red-600">{{ error }}</p>
    <button class="btn btn-secondary" @click="load">{{ $t('common.refresh') }}</button>
    <div class="overflow-x-auto">
      <table class="w-full text-sm text-left"><thead><tr><th>{{ $t('common.user') }}</th><th>{{ $t('apiKey.tokenName') }}</th><th>{{ $t('webhook.url') }}</th><th>{{ $t('common.actions') }}</th></tr></thead>
        <tbody><tr v-for="key in keys" :key="key.key_id" class="border-t"><td class="py-3">{{ key.username }}</td><td>{{ key.name }} <code>{{ key.prefix }}</code></td><td class="break-all">{{ key.webhook_enabled ? key.webhook_url : '—' }}</td><td><button class="btn btn-secondary" @click="selected = key">{{ $t('common.edit') }}</button></td></tr></tbody>
      </table>
    </div>
    <KeyWebhookDialog v-if="selected" :key-id="selected.key_id" :key-name="selected.name" @close="selected = null" @saved="load" />
  </section>
</template>
<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { getAllAPIKeys } from '@/api/authApi'
import type { AdminAPIKeyInfo } from '@/api/types'
import KeyWebhookDialog from './KeyWebhookDialog.vue'
const keys = ref<AdminAPIKeyInfo[]>([])
const selected = ref<AdminAPIKeyInfo | null>(null)
const error = ref('')
async function load() {
  try { keys.value = (await getAllAPIKeys()).api_keys; error.value = '' }
  catch (e: any) { error.value = e.message }
}
onMounted(load)
</script>
