<template>
  <div class="card">
    <!-- 标题栏 -->
    <div class="flex items-center justify-between mb-4">
      <div class="flex items-center gap-2">
        <Cpu class="w-5 h-5 text-primary-600" />
        <h2 class="text-base lg:text-lg font-semibold text-gray-900">{{ uiText('legacyUi.text18') }}</h2>
      </div>
      <button
        @click="refresh"
        :disabled="loading"
        class="text-sm text-primary-600 hover:text-primary-700 flex items-center gap-1 disabled:opacity-50"
      >
        <RefreshCw :class="{ 'animate-spin': loading }" class="w-4 h-4" />
        {{ $t('legacyUi.extra5') }}
      </button>
    </div>

    <!-- 加载中 -->
    <div v-if="loading && !data" class="py-6 text-center text-gray-400 text-sm">
      <LoadingSpinner :text="uiText('legacyUi.text14')" />
    </div>

    <!-- 错误 -->
    <div v-else-if="error" class="py-4 text-center text-red-500 text-sm">
      {{ error }}
    </div>

    <template v-else-if="data">
      <!-- 运行环境 + 核心包版本 -->
      <div class="grid grid-cols-1 sm:grid-cols-2 gap-3 mb-4">
        <!-- 核心包版本 -->
        <div class="bg-gray-50 rounded-lg p-3">
          <p class="text-xs font-medium text-gray-500 uppercase tracking-wide mb-2">{{ uiText('legacyUi.text19') }}</p>
          <dl class="space-y-1">
            <div
              v-for="(ver, pkg) in data.system_info.packages"
              :key="pkg"
              class="flex items-center justify-between"
            >
              <dt class="text-xs text-gray-600 font-mono">{{ pkg }}</dt>
              <dd>
                <span
                  :class="ver === 'N/A' ? 'bg-gray-100 text-gray-400' : 'bg-primary-50 text-primary-700'"
                  class="text-xs font-mono px-1.5 py-0.5 rounded"
                >{{ ver }}</span>
              </dd>
            </div>
          </dl>
        </div>

        <!-- 运行环境 -->
        <div class="bg-gray-50 rounded-lg p-3">
          <p class="text-xs font-medium text-gray-500 uppercase tracking-wide mb-2">{{ uiText('legacyUi.text20') }}</p>
          <dl class="space-y-1">
            <div class="flex items-center justify-between">
              <dt class="text-xs text-gray-600">Python</dt>
              <dd class="text-xs font-mono text-gray-800">{{ data.system_info.python }}</dd>
            </div>
            <div class="flex items-center justify-between">
              <dt class="text-xs text-gray-600">CUDA</dt>
              <dd>
                <span
                  :class="data.system_info.cuda === 'N/A' ? 'text-gray-400' : 'text-green-700 font-medium'"
                  class="text-xs font-mono"
                >{{ data.system_info.cuda }}</span>
              </dd>
            </div>
            <div class="flex items-center justify-between">
              <dt class="text-xs text-gray-600">GPU</dt>
              <dd class="text-xs text-gray-800 text-right max-w-[140px] truncate" :title="data.system_info.gpu">
                {{ data.system_info.gpu === 'N/A' ? uiText('legacyUi.text27') : data.system_info.gpu }}
              </dd>
            </div>
            <div v-if="data.system_info.gpu_memory_gb" class="flex items-center justify-between">
              <dt class="text-xs text-gray-600">{{ uiText('legacyUi.text21') }}</dt>
              <dd class="text-xs font-mono text-gray-800">{{ data.system_info.gpu_memory_gb }} GB</dd>
            </div>
          </dl>
        </div>
      </div>

      <!-- 可用引擎 -->
      <div>
        <p class="text-xs font-medium text-gray-500 uppercase tracking-wide mb-2">{{ uiText('legacyUi.text22') }}</p>
        <div class="flex flex-wrap gap-2">
          <template v-for="(group, category) in engineGroups" :key="category">
            <div
              v-if="group.engines.length > 0"
              class="flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-medium border"
              :class="group.style"
            >
              <component :is="group.icon" class="w-3.5 h-3.5" />
              <span>{{ group.label }}</span>
              <span class="opacity-70">× {{ group.engines.length }}</span>
            </div>
            <div
              v-else
              class="flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-medium border bg-gray-50 text-gray-400 border-gray-200"
            >
              <component :is="group.icon" class="w-3.5 h-3.5" />
              <span>{{ group.label }}</span>
              <span class="opacity-60">{{ uiText('legacyUi.text23') }}</span>
            </div>
          </template>
        </div>

        <!-- 引擎详细列表（折叠展开） -->
        <div class="mt-3">
          <button
            @click="showDetail = !showDetail"
            class="text-xs text-gray-500 hover:text-gray-700 flex items-center gap-1"
          >
            <ChevronDown :class="{ 'rotate-180': showDetail }" class="w-3.5 h-3.5 transition-transform" />
            {{ showDetail ? uiText('legacyUi.text24') : uiText('legacyUi.text28') }}
          </button>

          <div v-if="showDetail" class="mt-3 space-y-2">
            <template v-for="(group, category) in engineGroups" :key="category">
              <template v-if="group.engines.length > 0">
                <p class="text-xs font-medium text-gray-500 mt-2">{{ group.label }}</p>
                <div
                  v-for="engine in group.engines"
                  :key="engine.name"
                  class="flex items-center justify-between py-1.5 px-3 bg-gray-50 rounded-lg"
                >
                  <div class="flex items-center gap-2 min-w-0">
                    <span class="w-1.5 h-1.5 rounded-full bg-green-400 flex-shrink-0"></span>
                    <span class="text-xs text-gray-800 truncate">{{ engine.display_name }}</span>
                  </div>
                  <span
                    v-if="engine.version && engine.version !== 'N/A'"
                    class="text-xs font-mono text-gray-500 flex-shrink-0 ml-2"
                  >v{{ engine.version }}</span>
                </div>
              </template>
            </template>
          </div>
        </div>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { useI18n as useUiI18n } from 'vue-i18n'
const { t: uiText } = useUiI18n()
import { ref, computed, onMounted } from 'vue'
import { getEnginesInfo } from '@/api/systemApi'
import type { EnginesResponse } from '@/api/types'
import LoadingSpinner from './LoadingSpinner.vue'
import {
  Cpu,
  RefreshCw,
  ChevronDown,
  FileText,
  ScanText,
  Mic,
  Video,
  Dna,
  FilePen,
} from 'lucide-vue-next'

const loading = ref(false)
const error = ref<string | null>(null)
const data = ref<EnginesResponse | null>(null)
const showDetail = ref(false)

const engineGroups = computed(() => {
  if (!data.value) return {}
  const { engines } = data.value
  return {
    document: {
      label: uiText('legacyUi.text25'),
      icon: FileText,
      engines: engines.document,
      style: 'bg-blue-50 text-blue-700 border-blue-200',
    },
    ocr: {
      label: 'OCR',
      icon: ScanText,
      engines: engines.ocr,
      style: 'bg-purple-50 text-purple-700 border-purple-200',
    },
    audio: {
      label: uiText('legacyUi.text31'),
      icon: Mic,
      engines: engines.audio,
      style: 'bg-green-50 text-green-700 border-green-200',
    },
    video: {
      label: uiText('legacyUi.text30'),
      icon: Video,
      engines: engines.video,
      style: 'bg-orange-50 text-orange-700 border-orange-200',
    },
    format: {
      label: uiText('legacyUi.text26'),
      icon: Dna,
      engines: engines.format,
      style: 'bg-teal-50 text-teal-700 border-teal-200',
    },
    office: {
      label: 'Office',
      icon: FilePen,
      engines: engines.office,
      style: 'bg-yellow-50 text-yellow-700 border-yellow-200',
    },
  }
})

async function refresh() {
  loading.value = true
  error.value = null
  try {
    data.value = await getEnginesInfo()
  } catch (e: any) {
    error.value = e?.message || uiText('legacyUi.text29')
  } finally {
    loading.value = false
  }
}

onMounted(refresh)
</script>
