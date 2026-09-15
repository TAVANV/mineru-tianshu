<template>
  <section class="card p-6 mt-6 space-y-6">
    <h2 class="text-xl font-semibold">{{ text('可选功能', 'Optional features') }}</h2>
    <p class="text-sm text-gray-500">{{ text('图片描述保存后生效；默认回调仅应用于随后提交的任务。两项功能默认关闭。', 'Caption settings apply when processing; default webhooks apply to future submissions. Both are off by default.') }}</p>
    <div v-if="message" role="status" class="p-3 rounded bg-blue-50 text-sm">{{ message }}</div>
    <form v-if="loaded" @submit.prevent="save" class="space-y-6">
      <fieldset v-for="group in groups" :key="group.title[1]" class="border rounded-lg p-4">
        <legend class="font-semibold px-2">{{ text(...group.title) }}</legend>
        <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
          <label v-for="field in group.fields" :key="field.key" class="text-sm space-y-1 block">
            <span class="block">{{ text(...field.label) }}</span>
            <select v-if="field.type === 'boolean'" v-model="settings[field.key]" class="w-full border rounded p-2">
              <option value="false">{{ text('关闭', 'Off') }}</option><option value="true">{{ text('开启', 'On') }}</option>
            </select>
            <input v-else v-model="settings[field.key]" :type="field.type || 'text'" :min="field.min" :max="field.max" :maxlength="field.maxLength"
              :placeholder="field.placeholder" :autocomplete="field.type === 'password' ? 'new-password' : 'off'" class="w-full border rounded p-2" />
            <span v-if="field.help" class="block text-xs text-gray-500">{{ text(...field.help) }}</span>
          </label>
        </div>
      </fieldset>
      <div class="flex flex-wrap gap-3">
        <button :disabled="busy" class="btn btn-primary">{{ text('保存可选功能', 'Save optional features') }}</button>
        <button type="button" :disabled="busy" @click="test('/optional-features/test-caption')" class="btn btn-secondary">{{ text('测试已保存的图片描述配置', 'Test saved caption settings') }}</button>
        <button type="button" :disabled="busy" @click="test('/webhooks/test')" class="btn btn-secondary">{{ text('发送已保存的 Webhook 测试', 'Send saved webhook test') }}</button>
      </div>
    </form>
    <section v-for="kind in (['audit', 'deliveries'] as const)" :key="kind" class="border-t pt-4">
      <div class="flex items-center justify-between mb-3">
        <h3 class="font-semibold">{{ kind === 'audit' ? text('审计日志', 'Audit log') : text('Webhook 投递', 'Webhook deliveries') }}</h3>
        <button @click="loadRecords(kind)" class="btn btn-secondary">{{ text('刷新', 'Refresh') }}</button>
      </div>
      <form v-if="kind === 'audit'" @submit.prevent="pages.audit = 1; loadRecords('audit')" class="flex gap-2 mb-3">
        <input v-model="auditAction" :placeholder="text('按操作路径筛选', 'Filter by action path')" class="border rounded p-2 flex-1" />
        <button class="btn btn-secondary">{{ text('筛选', 'Filter') }}</button>
      </form>
      <div class="overflow-x-auto">
        <table class="w-full text-sm text-left"><thead><tr><th class="p-2">{{ text('时间', 'Time') }}</th><th class="p-2">{{ text('操作 / 任务', 'Action / Task') }}</th><th class="p-2">{{ text('状态', 'Status') }}</th></tr></thead>
          <tbody><tr v-for="row in records[kind]" :key="row.id" class="border-t"><td class="p-2 whitespace-nowrap">{{ row.created_at }}</td><td class="p-2 break-all">{{ row.action || row.task_id }} <span class="text-gray-500">{{ row.username || '' }}</span></td><td class="p-2">{{ row.result || row.state }} {{ row.attempt ? `(${row.attempt})` : '' }}</td></tr></tbody>
        </table>
      </div>
      <p v-if="!records[kind].length" class="text-sm text-gray-500 py-3">{{ text('暂无记录', 'No records') }}</p>
      <div class="flex items-center gap-3 mt-3 text-sm">
        <button :disabled="pages[kind] <= 1" @click="pages[kind]--; loadRecords(kind)" class="btn btn-secondary">{{ text('上一页', 'Previous') }}</button>
        <span>{{ pages[kind] }} · {{ totals[kind] }}</span>
        <button :disabled="pages[kind] * 20 >= totals[kind]" @click="pages[kind]++; loadRecords(kind)" class="btn btn-secondary">{{ text('下一页', 'Next') }}</button>
      </div>
    </section>
  </section>
</template>
<script setup lang="ts">
import { ref, reactive, onMounted } from 'vue'
import { useI18n } from 'vue-i18n'
import apiClient from '@/api/client'
const { locale } = useI18n()
const text = (zh: string, en: string) => locale.value.startsWith('zh') ? zh : en
const settings = reactive<Record<string, string>>({})
const loaded = ref(false), busy = ref(false), message = ref(''), auditAction = ref('')
type Kind = 'audit' | 'deliveries'
const records = reactive<Record<Kind, any[]>>({ audit: [], deliveries: [] })
const pages = reactive({ audit: 1, deliveries: 1 }), totals = reactive({ audit: 0, deliveries: 0 })
type Pair = [string, string]
type Field = { key: string; label: Pair; type?: string; placeholder?: string; min?: number; max?: number; maxLength?: number; help?: Pair }
const groups: {title: Pair; fields: Field[]}[] = [
  { title: ['注册与审计', 'Registration and audit'], fields: [
    { key: 'registration_invite_code', maxLength: 100, label: ['注册邀请码', 'Registration invitation code'], type: 'password', help: ['留空允许无邀请码注册；星号表示保持原值。', 'Leave empty for open registration; stars keep the existing value.'] },
    { key: 'audit_retention_days', label: ['审计保留天数', 'Audit retention days'], type: 'number', min: 1, max: 3650 },
  ]},
  { title: ['图片描述', 'Image captions'], fields: [
    { key: 'image_caption_enabled', label: ['启用图片描述', 'Enable image captions'], type: 'boolean' },
    { key: 'image_caption_api_base', label: ['模型接口地址', 'Model API base'], placeholder: 'http://model.internal:8000/v1' },
    { key: 'image_caption_api_key', label: ['模型 API Key', 'Model API key'], type: 'password' },
    { key: 'image_caption_model', label: ['模型名称', 'Model name'] },
    { key: 'image_caption_prompt', label: ['描述提示词', 'Caption prompt'] },
    { key: 'image_caption_max_images', label: ['每次解析最多图片数（分片单独计数）', 'Maximum images per parse (each chunk counts separately)'], type: 'number', min: 1, max: 500 },
    { key: 'image_caption_concurrency', label: ['并发数', 'Concurrency'], type: 'number', min: 1, max: 16 },
    { key: 'image_caption_timeout', label: ['超时秒数', 'Timeout seconds'], type: 'number', min: 1, max: 300 },
  ]},
  { title: ['Webhook', 'Webhook'], fields: [
    { key: 'webhook_enabled', label: ['启用默认回调', 'Enable default webhook'], type: 'boolean' },
    { key: 'webhook_url', label: ['默认回调地址', 'Default webhook URL'] },
    { key: 'webhook_secret', label: ['签名密钥', 'Signing secret'], type: 'password' },
    { key: 'webhook_authorization', label: ['Authorization 请求头', 'Authorization header'], type: 'password', placeholder: 'Bearer …' },
    { key: 'webhook_allowed_hosts', label: ['内网地址白名单', 'Private host allowlist'], placeholder: 'service.internal:8080', help: ['填写精确 host:port，多个用逗号分隔。', 'Exact host:port entries, separated by commas.'] },
    { key: 'webhook_timeout', label: ['回调超时秒数', 'Delivery timeout seconds'], type: 'number', min: 1, max: 60 },
    { key: 'webhook_max_attempts', label: ['最多投递次数', 'Maximum attempts'], type: 'number', min: 1, max: 20 },
  ]},
]
function showError(error: any) { message.value = error.response?.data?.detail || error.message || text('请求失败', 'Request failed') }
async function save() {
  busy.value = true
  try { const r = await apiClient.post('/api/v1/admin/optional-features', settings); Object.assign(settings, r.data.config); message.value = text('已保存', 'Saved') }
  catch (error) { showError(error) } finally { busy.value = false }
}
async function test(path: string) {
  busy.value = true
  try { const r = await apiClient.post('/api/v1/admin' + path); message.value = r.data.message }
  catch (error) { showError(error) } finally { busy.value = false }
}
async function loadRecords(kind: Kind) {
  try {
    const path = kind === 'audit' ? 'audit-logs' : 'webhook-deliveries'
    const r = await apiClient.get('/api/v1/admin/' + path, { params: { page: pages[kind], page_size: 20, action: kind === 'audit' ? auditAction.value || undefined : undefined } })
    records[kind] = r.data.items; totals[kind] = r.data.total
  } catch (error) { showError(error) }
}
onMounted(async () => {
  try { const r = await apiClient.get('/api/v1/admin/optional-features'); Object.assign(settings, r.data.config); loaded.value = true }
  catch (error) { showError(error) }
  await Promise.all([loadRecords('audit'), loadRecords('deliveries')])
})
</script>
