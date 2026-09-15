<template>
  <div class="h-[calc(100vh-4rem)] flex flex-col">
    <div class="flex items-center justify-between mb-4 px-1 flex-shrink-0">
      <div class="flex items-center gap-4">
        <button @click="$router.back()" class="text-sm text-gray-600 hover:text-gray-900 flex items-center transition-colors">
          <ArrowLeft class="w-4 h-4 mr-1" /> {{ $t('legacyUi.extra0') }}
        </button>
        <div class="h-4 w-px bg-gray-300"></div>
        <h1 class="text-xl font-bold text-gray-900 truncate max-w-md" :title="task?.file_name">{{ task?.file_name || uiText('legacyUi.text42') }}</h1>
        <StatusBadge v-if="task" :status="task.status" />
      </div>

      <div class="flex items-center gap-3">
        <template v-if="task">
            <button v-if="['pending', 'processing', 'paused'].includes(task.status)" @click="initiateAction('cancel')" :disabled="actionLoading" class="btn btn-secondary btn-sm">{{ $t('task.cancelTask') }}</button>
            <button v-if="task.status === 'failed'" @click="initiateAction('retry')" :disabled="actionLoading" class="btn btn-white text-blue-600 border-gray-200 hover:bg-blue-50 btn-sm flex items-center shadow-sm transition-all disabled:opacity-50">
              <RotateCw :class="{'animate-spin': actionLoading && currentAction === 'retry'}" class="w-4 h-4 mr-1.5" />
              <span>{{ uiText('legacyUi.text32') }}</span>
            </button>
            <button v-if="['completed', 'failed'].includes(task.status) && task.result_path !== 'CLEARED'" @click="initiateAction('clearCache')" :disabled="actionLoading" class="btn btn-white text-orange-600 border-gray-200 hover:bg-orange-50 btn-sm flex items-center shadow-sm transition-all disabled:opacity-50">
              <Eraser :class="{'animate-pulse': actionLoading && currentAction === 'clearCache'}" class="w-4 h-4 mr-1.5" />
              <span>{{ uiText('legacyUi.text33') }}</span>
            </button>
            <button @click="initiateAction('delete')" :disabled="actionLoading" class="btn btn-white text-red-600 border-gray-200 hover:bg-red-50 btn-sm flex items-center shadow-sm transition-all disabled:opacity-50" :title="uiText('legacyUi.text35')">
              <Trash2 class="w-4 h-4 mr-1.5" />
              <span class="hidden sm:inline">{{ uiText('legacyUi.text34') }}</span>
            </button>
        </template>

        <div v-if="task?.status === 'completed' && (pdfUrl || imageUrl) && task?.result_path !== 'CLEARED'" class="flex items-center bg-gray-100 rounded-lg p-1">
          <button @click="setMode('single')" :class="['px-3 py-1.5 text-xs font-medium rounded-md transition-all flex items-center', layoutMode === 'single' ? 'bg-white text-gray-900 shadow-sm' : 'text-gray-500 hover:text-gray-700']">
            <FileText class="w-3.5 h-3.5 mr-1.5" /> {{ $t('legacyUi.extra1') }}
          </button>
          <button @click="setMode('split')" :class="['px-3 py-1.5 text-xs font-medium rounded-md transition-all flex items-center', layoutMode === 'split' ? 'bg-white text-primary-600 shadow-sm' : 'text-gray-500 hover:text-gray-700']">
            <Columns class="w-3.5 h-3.5 mr-1.5" /> {{ $t('legacyUi.extra2') }}
          </button>
        </div>

        <button @click="refreshTask()" :disabled="loading" class="btn btn-secondary btn-sm shadow-sm"><RefreshCw :class="{ 'animate-spin': loading }" class="w-4 h-4" /></button>
      </div>
    </div>

    <div v-if="loading && !task" class="flex-1 flex items-center justify-center"><LoadingSpinner size="lg" :text="uiText('legacyUi.text14')" /></div>
    <div v-else-if="error" class="card bg-red-50 border-red-200 mx-1 p-4 mb-4"><div class="flex items-center text-red-800"><AlertCircle class="w-6 h-6 mr-3" /> {{ error }}</div></div>

    <div v-else-if="task" class="flex-1 min-h-0 relative">
      <div v-if="['pending', 'processing', 'paused'].includes(task.status)" class="max-w-3xl mx-auto mt-16 space-y-6 px-4">
         <div class="card p-10 text-center shadow-sm">
            <h2 class="text-xl font-semibold text-gray-900 mb-2">{{ uiText('legacyUi.text38') }}</h2>
            <div class="mt-8 flex justify-center"><LoadingSpinner size="lg" /></div>
            <div v-if="task.subtask_progress" class="mt-4 space-y-2 text-sm">
              <p>{{ task.subtask_progress.completed }} / {{ task.subtask_progress.total }} ({{ task.subtask_progress.percentage }}%)</p>
              <progress :value="task.subtask_progress.completed" :max="task.subtask_progress.total" class="w-full" />
              <ul class="max-h-64 overflow-auto text-left divide-y">
                <li v-for="child in task.subtasks" :key="child.task_id" class="py-2 flex justify-between gap-3">
                  <span class="truncate">{{ child.file_name || child.task_id }}</span><StatusBadge :status="child.status" />
                </li>
              </ul>
            </div>
         </div>
      </div>
      <div v-else-if="['failed', 'cancelled'].includes(task.status)" class="max-w-3xl mx-auto mt-10 space-y-6 px-4">
         <div class="card p-8 text-center border-red-100 bg-red-50/50">
            <div class="flex justify-center mb-4"><div class="p-3 bg-red-100 rounded-full text-red-500"><AlertCircle class="w-8 h-8" /></div></div>
            <h2 class="text-xl font-semibold text-red-700 mb-2">{{ task.status === 'cancelled' ? $t('task.cancelledLabel') : uiText('legacyUi.text41') }}</h2>
            <div class="text-red-600 bg-white p-4 rounded-lg border border-red-200 font-mono text-sm text-left overflow-auto max-h-64 break-all shadow-sm">{{ task.status === 'cancelled' ? $t('task.cancelledHelp') : task.error_message || uiText('legacyUi.text45') }}</div>
         </div>
      </div>

      <div v-else class="h-full w-full flex flex-row gap-4">

        <div v-if="showPdf" :class="['card p-0 flex flex-col h-full border border-gray-200 relative shadow-sm min-w-0 transition-all duration-300', layoutMode === 'split' ? 'flex-1 basis-1/2' : 'flex-1 basis-full']">
          <div class="bg-gray-50 px-3 py-2 border-b border-gray-200 flex justify-between items-center shrink-0">
            <span class="text-xs font-semibold text-gray-500 uppercase tracking-wider">{{ imageUrl ? uiText('legacyUi.text46') : uiText('legacyUi.text47') }}</span>
          </div>

          <div class="flex-1 relative overflow-auto min-h-0 bg-gray-200">
            <img v-if="imageUrl" :src="imageUrl" :alt="task.file_name" referrerpolicy="no-referrer" class="max-w-full h-auto mx-auto" />
            <VirtualPdfViewer
              v-if="!imageUrl && pdfUrl"
              ref="pdfViewerRef"
              :src="pdfUrl"
              :layout-data="layoutData"
              @block-click="handlePdfBlockClick"
            />
          </div>
        </div>

        <div v-if="showMarkdown" :class="['card p-0 flex flex-col h-full shadow-sm border border-gray-200 min-w-0 transition-all duration-300', layoutMode === 'split' ? 'flex-1 basis-1/2' : 'flex-1 basis-full']">
          <div class="bg-gray-50 px-3 py-2 border-b border-gray-200 flex justify-between items-center shrink-0">
            <div class="flex items-center bg-gray-200 rounded p-0.5">
              <button @click="activeTab = 'markdown'" :class="['tab-btn', activeTab==='markdown' ? 'active' : '']">{{ uiText('legacyUi.text39') }}</button>
              <button @click="activeTab = 'sync'" :class="['tab-btn flex items-center gap-1', activeTab==='sync' ? 'active' : '']">
                {{ $t('legacyUi.extra3') }}
                <span v-if="activeBlockId" class="w-2 h-2 rounded-full bg-red-500 animate-pulse"></span>
              </button>
              <button @click="activeTab = 'json'" :class="['tab-btn', activeTab==='json' ? 'active' : '']">JSON</button>
            </div>
            <button @click="downloadMarkdown" class="text-xs text-primary-600 hover:underline flex items-center">
              <Download class="w-3 h-3 mr-1"/> {{ $t('legacyUi.extra4') }}
            </button>
          </div>

          <div :class="[
            'flex-1 min-h-0 relative bg-white',
            activeTab === 'json' ? 'flex flex-col overflow-hidden' : 'overflow-y-auto overflow-x-hidden custom-scrollbar p-6 scroll-smooth'
          ]">

            <div v-if="activeTab === 'markdown'" class="w-full">
               <MarkdownViewer :content="task.data?.content || ''" />
            </div>

            <div v-else-if="activeTab === 'sync'" class="w-full max-w-[800px] mx-auto">
              <div v-if="layoutData.length > 0" class="flex flex-col gap-3">
                <div class="text-xs text-gray-500 bg-blue-50 p-2.5 rounded-lg mb-3 border border-blue-100">
                  {{ $t('legacyUi.extra8') }}
                </div>

                <div
                  v-for="block in layoutData"
                  :key="block.id"
                  :id="`md-block-${block.id}`"
                  @click="handleMarkdownBlockClick(block)"
                  :class="['p-3 rounded-lg transition-all cursor-pointer border break-words w-full text-[14px] leading-relaxed relative',
                           activeBlockId === block.id
                             ? 'bg-yellow-50 border-yellow-400 shadow-sm ring-2 ring-yellow-200'
                             : 'bg-white border-gray-100 hover:bg-gray-50 hover:border-gray-300']"
                  :title="uiText('legacyUi.text48')"
                >
                  <div v-if="block.type === 'image'" class="text-blue-500 text-xs font-semibold mb-1 flex items-center gap-1 select-none"><Image class="w-3.5 h-3.5"/> {{ uiText('legacyUi.text36') }}</div>
                  <div v-else-if="block.type === 'table'" class="text-green-500 text-xs font-semibold mb-1 flex items-center gap-1 select-none"><Table class="w-3.5 h-3.5"/> {{ uiText('legacyUi.text37') }}</div>
                  <div v-else-if="block.type === 'doc_title'" class="text-lg font-bold text-gray-900 mb-1 border-b pb-1">{{ block.text }}</div>

                  <div v-if="block.type === 'table'" class="w-full overflow-x-auto mt-2 markdown-table-override">
                    <MarkdownViewer :content="block.text" />
                  </div>
                  <div v-else-if="block.type !== 'doc_title'" class="whitespace-pre-wrap font-mono text-gray-600">{{ block.text }}</div>
                </div>
              </div>
              <div v-else class="text-gray-500 text-sm italic text-center mt-10">{{ uiText('legacyUi.text40') }}</div>
            </div>

            <div v-else class="h-full w-full flex-1 flex min-h-0">
               <JsonViewer :data="task.data?.json_content || {}" />
            </div>

          </div>
        </div>

      </div>
    </div>

    <ConfirmDialog v-model="showConfirm" :title="confirmTitle" :message="confirmMessage" :type="confirmType" @confirm="executeAction" />
  </div>
</template>

<script setup lang="ts">
import { useI18n as useUiI18n } from 'vue-i18n'
const { t: uiText } = useUiI18n()
import { authenticatedFileUrl } from '@/api/fileUrl'
import { ref, computed, onMounted, onUnmounted, nextTick } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { useTaskStore } from '@/stores'
import { ArrowLeft, AlertCircle, RefreshCw, FileText, Columns, Download, RotateCw, Eraser, Pause, Image, Table, Trash2 } from 'lucide-vue-next'
import StatusBadge from '@/components/StatusBadge.vue'
import LoadingSpinner from '@/components/LoadingSpinner.vue'
import MarkdownViewer from '@/components/MarkdownViewer.vue'
import JsonViewer from '@/components/JsonViewer.vue'
import ConfirmDialog from '@/components/ConfirmDialog.vue'
import VirtualPdfViewer from '@/components/VirtualPdfViewer.vue'

const { t } = useI18n()
const router = useRouter()
const route = useRoute()
const taskStore = useTaskStore()

const taskId = computed(() => route.params.id as string)
const task = computed(() => taskStore.currentTask)
const loading = ref(false)
const actionLoading = ref(false)
const error = ref('')

const activeTab = ref<'markdown' | 'sync' | 'json'>('markdown')
const layoutMode = ref<'split' | 'single'>('split')

const activeBlockId = ref<string | number | null>(null)
const pdfViewerRef = ref<InstanceType<typeof VirtualPdfViewer> | null>(null)

const pdfUrl = computed(() => task.value?.data?.pdf_path ? authenticatedFileUrl(`/api/v1/files/output/${task.value.data.pdf_path}`) : null)
const imageUrl = computed(() => /\.(png|jpe?g|gif|webp|bmp|tiff?)$/i.test(task.value?.file_name || '')
  ? (task.value?.source_url ? authenticatedFileUrl(task.value.source_url) : null) : null)
const showPdf = computed(() => Boolean(imageUrl.value || pdfUrl.value))
const showMarkdown = computed(() => layoutMode.value === 'split' || !showPdf.value)

const layoutData = computed(() => {
  const jsonContent = task.value?.data?.json_content
  if (!jsonContent) return []

  let flatBlocks: any[] = []

  // 从 MinerU content_list_v2 嵌套 content 对象中提取纯文本
  const extractV2Text = (block: any): string => {
      if (block.text) return block.text
      const c = block.content
      if (!c) return ''
      const lists = c.title_content ?? c.paragraph_content ?? c.table_content ?? c.list_content ?? []
      if (Array.isArray(lists)) return lists.map((item: any) => item.content ?? '').join('')
      if (typeof c === 'string') return c
      return ''
  }

  if (Array.isArray(jsonContent)) {
      if (jsonContent.length > 0 && Array.isArray(jsonContent[0])) {
          // MinerU content_list_v2.json: 外层按页分组的嵌套数组 [[page0_blocks], [page1_blocks], ...]
          flatBlocks = (jsonContent as any[][]).flatMap((page: any[], pIdx: number) =>
              page.map((b: any, i: number) => ({ ...b, _page_idx: pIdx, _idx: i }))
          )
      } else if (jsonContent.length > 0 && (jsonContent[0].parsing_res_list || jsonContent[0].blocks)) {
          // 按页分组的对象格式
          flatBlocks = jsonContent.flatMap((p: any, pIdx: number) => {
              const blocks = p.parsing_res_list || p.blocks || [];
              const pageIdx = p.page_index ?? p.page_id ?? p.page_no ?? pIdx;
              return blocks.map((b: any, i: number) => ({ ...b, _page_idx: pageIdx, _idx: i, _page_width: p.width }))
          })
      } else {
          // 扁平列表格式 content_list_v1：每个 block 自带 page_idx
          flatBlocks = jsonContent.map((b: any, i: number) => ({ ...b, _idx: i }))
      }
  }
  else if (jsonContent.pages && Array.isArray(jsonContent.pages)) {
      flatBlocks = jsonContent.pages.flatMap((p: any, pIdx: number) => {
          const blocks = p.blocks || p.parsing_res_list || [];
          const pageIdx = p.page_index ?? p.page_id ?? p.page_no ?? pIdx;
          return blocks.map((b: any, i: number) => ({ ...b, _page_idx: pageIdx, _idx: i, _page_width: p.width }))
      })
  }
  else if (jsonContent.parsing_res_list) {
      const pageIdx = jsonContent.page_index ?? 0;
      flatBlocks = jsonContent.parsing_res_list.map((b: any, i: number) => ({ ...b, _page_idx: pageIdx, _idx: i, _page_width: jsonContent.width }))
  }

  const formattedBlocks = flatBlocks.map((b, globalIdx) => {
      const pIdx = b.page_idx ?? b._page_idx ?? 0;
      const uniqueId = `block-${pIdx}-${globalIdx}`;

      return {
          id: uniqueId,
          orig_id: b.id ?? b.block_id,
          page_idx: pIdx,
          bbox: b.bbox ?? b.block_bbox ?? b.layout_bbox ?? [],
          text: extractV2Text(b),
          type: b.type ?? b.block_label ?? 'text',
          order: b.order ?? b.block_order ?? null,
          _page_width: b._page_width ?? null
      }
  })

  formattedBlocks.sort((a, b) => {
     if (a.page_idx !== b.page_idx) return a.page_idx - b.page_idx;

     const aHasOrder = a.order !== null && a.order !== undefined;
     const bHasOrder = b.order !== null && b.order !== undefined;

     if (aHasOrder && bHasOrder) return a.order - b.order;
     if (aHasOrder && !bHasOrder) return -1;
     if (!aHasOrder && bHasOrder) return 1;

     return 0;
  });

  return formattedBlocks;
})

const handlePdfBlockClick = (block: any) => {
  if (!block) return
  activeBlockId.value = block.id

  const isSwitchingTab = activeTab.value !== 'sync';
  if (isSwitchingTab) {
    activeTab.value = 'sync';
  }

  const delay = isSwitchingTab ? 150 : 50;

  setTimeout(() => {
    const el = document.getElementById(`md-block-${block.id}`)
    if (el) {
      el.scrollIntoView({ behavior: 'smooth', block: 'center' })
    }
  }, delay)
}

const handleMarkdownBlockClick = (block: any) => {
  if (!block) return
  activeBlockId.value = block.id

  if (pdfViewerRef.value && typeof pdfViewerRef.value.highlightBlock === 'function') {
    const pageIndex = (typeof block.page_idx === 'number' ? block.page_idx : block.page_id) + 1
    pdfViewerRef.value.highlightBlock(pageIndex, block.bbox)
  }
}

const setMode = (mode: 'split' | 'single') => { layoutMode.value = mode }
let stopPolling: (() => void) | null = null

async function refreshTask() {
  loading.value = true; error.value = '';
  try { await taskStore.fetchTaskStatus(taskId.value, false, 'both') }
  catch (err: any) { error.value = err.message || t('task.loadFailed') }
  finally { loading.value = false }
}

function startPolling() {
  if (stopPolling) stopPolling()
  stopPolling = taskStore.pollTaskStatus(taskId.value, 3000, async (updatedTask) => {
    if (['completed', 'failed', 'cancelled'].includes(updatedTask.status)) {
      stopPolling()
      await refreshTask()
    }
  })
}

const downloadMarkdown = () => {
  if (!task.value?.data?.content) return
  const blob = new Blob([task.value.data.content], { type: 'text/markdown' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = task.value.data.markdown_file || `${taskId.value}.md`
  a.click()
  URL.revokeObjectURL(url)
}

const showConfirm = ref(false)
const confirmTitle = ref('')
const confirmMessage = ref('')
const confirmType = ref<'info' | 'warning' | 'danger'>('info')
const currentAction = ref<'retry' | 'clearCache' | 'delete' | 'cancel' | null>(null)

function initiateAction(action: 'retry' | 'clearCache' | 'delete' | 'cancel') {
  currentAction.value = action
  if (action === 'cancel') {
    confirmTitle.value = t('task.cancelTask'); confirmMessage.value = t('task.cancelGroupConfirm'); confirmType.value = 'warning'
  } else if (action === 'retry') {
    confirmTitle.value = uiText('legacyUi.text32'); confirmMessage.value = uiText('legacyUi.text50'); confirmType.value = 'info'
  } else if (action === 'clearCache') {
    confirmTitle.value = uiText('legacyUi.text33'); confirmMessage.value = uiText('legacyUi.text49'); confirmType.value = 'warning'
  } else if (action === 'delete') {
    confirmTitle.value = uiText('legacyUi.text43'); confirmMessage.value = uiText('legacyUi.text44'); confirmType.value = 'danger'
  }
  showConfirm.value = true
}

async function executeAction() {
  if (!currentAction.value) return
  actionLoading.value = true
  try {
    if (currentAction.value === 'cancel') {
      await taskStore.cancelTask(taskId.value); await refreshTask();
    } else if (currentAction.value === 'retry') {
      await taskStore.retryTask(taskId.value); await refreshTask(); startPolling();
    } else if (currentAction.value === 'clearCache') {
      await taskStore.clearTaskCache(taskId.value); await refreshTask();
    } else if (currentAction.value === 'delete') {
      await taskStore.deleteTask(taskId.value); router.back();
    }
  } catch (err: any) { error.value = err.message || 'Action failed' }
  finally { actionLoading.value = false; currentAction.value = null }
}

onMounted(async () => {
  await refreshTask()
  if (task.value && ['pending', 'processing'].includes(task.value.status)) startPolling()
})
onUnmounted(() => { if (stopPolling) stopPolling() })
</script>

<style scoped>
.tab-btn { @apply text-xs px-3 py-1.5 rounded transition-all text-gray-500 font-medium whitespace-nowrap; }
.tab-btn.active { @apply bg-white text-primary-600 shadow-sm border border-gray-100; }
.custom-scrollbar::-webkit-scrollbar { width: 8px; height: 8px; }
.custom-scrollbar::-webkit-scrollbar-track { background: transparent; }
.custom-scrollbar::-webkit-scrollbar-thumb { background: #d1d5db; border-radius: 4px; background-clip: content-box;}
.custom-scrollbar::-webkit-scrollbar-thumb:hover { background: #9ca3af; }

.markdown-table-override :deep(.card) {
  padding: 0;
  border: none;
  box-shadow: none;
  background: transparent;
}
.markdown-table-override :deep(.markdown-viewer) {
  max-height: none;
}
</style>
