<template>
  <!-- 完全使用 Scalar 自身样式，不添加额外包装 -->
  <div class="scalar-container">
    <!-- 加载状态 -->
    <div v-if="isLoading" class="loading-state">
      <div class="loading-content">
        <div class="loading-spinner"></div>
        <p>{{ t('apiDocs.loading') }}</p>
      </div>
    </div>

    <!-- 错误状态 -->
    <div v-else-if="loadError" class="error-state">
      <div class="error-content">
        <svg class="error-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
        </svg>
        <h3>{{ t('apiDocs.loadError') }}</h3>
        <p>{{ loadError }}</p>
        <button @click="retryLoad" class="retry-button">{{ t('apiDocs.retry') }}</button>
      </div>
    </div>

    <!-- Scalar API Reference - 完全独立渲染 -->
    <ApiReference
      v-else
      :configuration="scalarConfig"
      @ready="onScalarReady"
    />
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, watch } from 'vue'
import { ApiReference } from '@scalar/api-reference'
import { useAuthStore } from '@/stores'
import { useI18n } from 'vue-i18n'

// 导入 Scalar 样式 - 但会在页面底部添加样式覆盖来保护我们的样式
import '@scalar/api-reference/style.css'

const authStore = useAuthStore()
const { locale, t } = useI18n()

// 响应式配置
const isLoading = ref(true)
const loadError = ref('')
const openApiSpec = ref<any>(null)

// API 文档翻译字典（中文 -> 英文）
const apiTranslations: Record<string, string> = {
  // 标签/分类
  '系统信息': 'System Info',
  '任务管理': 'Task Management',
  '队列管理': 'Queue Management',
  '系统管理': 'System Management',
  '文件服务': 'File Service',
  'Authentication': 'Authentication',

  // 通用描述
  'API根路径': 'API Root',
  '健康检查接口': 'Health Check',
  '列出所有可用的处理引擎': 'List All Available Processing Engines',
  '无需认证。返回系统中所有可用的处理引擎信息。': 'No authentication required. Returns information about all available processing engines in the system.',

  // 任务管理
  '提交文档解析任务': 'Submit Document Parsing Task',
  '需要认证和 TASK_SUBMIT 权限。': 'Requires authentication and TASK_SUBMIT permission.',
  '立即返回 task_id，任务在后台异步处理。': 'Returns task_id immediately, task is processed asynchronously in the background.',

  '查询任务状态和详情': 'Query Task Status and Details',
  '需要认证。用户只能查看自己的任务，管理员可以查看所有任务。': 'Requires authentication. Users can only view their own tasks, administrators can view all tasks.',
  '当任务完成时，会自动返回解析后的内容（data 字段）': 'When the task is completed, the parsed content will be automatically returned (data field)',

  '取消任务（仅限 pending 状态）': 'Cancel Task (pending status only)',
  '需要认证。用户只能取消自己的任务，管理员可以取消任何任务。': 'Requires authentication. Users can only cancel their own tasks, administrators can cancel any task.',

  // 队列管理
  '获取队列统计信息': 'Get Queue Statistics',
  '需要认证和 QUEUE_VIEW 权限。': 'Requires authentication and QUEUE_VIEW permission.',

  '获取任务列表': 'Get Task List',
  '需要认证。普通用户只能看到自己的任务，管理员/经理可以看到所有任务。': 'Requires authentication. Regular users can only see their own tasks, administrators/managers can see all tasks.',

  // 系统管理
  '清理旧任务（管理接口）': 'Clean Up Old Tasks (Admin)',
  '同时删除任务的结果文件和数据库记录。': 'Deletes both task result files and database records.',
  '需要管理员权限。': 'Requires administrator permission.',

  '重置超时的 processing 任务（管理接口）': 'Reset Stale Processing Tasks (Admin)',

  // 文件服务
  '提供输出文件的访问服务': 'Serve Output Files',
  '支持 URL 编码的中文路径': 'Supports URL-encoded Chinese paths',
  '注意：Nginx 代理会去掉 /api/ 前缀，所以这里不需要 /api/': 'Note: Nginx proxy removes /api/ prefix, so /api/ is not needed here',

  // 认证相关
  '用户注册': 'User Registration',
  '创建新用户账户。默认角色为 \'user\'，需要管理员才能创建其他角色。': 'Create a new user account. Default role is \'user\', administrator permission required to create other roles.',

  '用户登录': 'User Login',
  '使用用户名和密码登录，返回 JWT Access Token。': 'Login with username and password, returns JWT Access Token.',

  '获取当前登录用户信息': 'Get Current User Info',
  '需要认证。返回当前用户的详细信息。': 'Requires authentication. Returns detailed information about the current user.',

  '更新当前用户信息': 'Update Current User Info',
  '用户可以更新自己的邮箱和全名，不能更新角色。': 'Users can update their own email and full name, but cannot update their role.',

  '创建 API Key': 'Create API Key',
  '为当前用户创建一个新的 API Key。API Key 只会在创建时返回一次，请妥善保管。': 'Create a new API Key for the current user. API Key is only returned once during creation, please keep it safe.',

  '列出当前用户的所有 API Key': 'List All API Keys for Current User',
  '返回 API Key 列表，不包含完整的 key，只显示前缀。': 'Returns API Key list, does not include full key, only shows prefix.',

  '删除 API Key': 'Delete API Key',
  '删除指定的 API Key。只能删除自己的 API Key。': 'Delete the specified API Key. Can only delete your own API Keys.',

  '列出所有用户': 'List All Users',
  '需要管理员权限。返回用户列表。': 'Requires administrator permission. Returns user list.',

  '创建用户 (管理员)': 'Create User (Admin)',
  '管理员可以创建任意角色的用户。': 'Administrators can create users with any role.',

  '更新用户信息 (管理员)': 'Update User Info (Admin)',
  '管理员可以更新任意用户的信息，包括角色和状态。': 'Administrators can update any user\'s information, including role and status.',

  '删除用户 (管理员)': 'Delete User (Admin)',
  '管理员可以删除用户。不能删除自己。': 'Administrators can delete users. Cannot delete yourself.',

  '检查 SSO 是否启用': 'Check if SSO is Enabled',
  '返回 SSO 配置状态。': 'Returns SSO configuration status.',

  'SSO 登录入口': 'SSO Login Entry',
  '重定向到 SSO 提供者进行认证。': 'Redirects to SSO provider for authentication.',

  'SSO 回调接口': 'SSO Callback',
  '处理 SSO 提供者的回调，创建或获取用户，返回 JWT Token。': 'Handles SSO provider callback, creates or retrieves user, returns JWT Token.',

  // 参数描述
  '文件: PDF/图片/Office/HTML/音频/视频等多种格式': 'File: PDF/Image/Office/HTML/Audio/Video and other formats',
  '处理后端: auto (自动选择) | pipeline/paddleocr-vl (文档) | sensevoice (音频) | video (视频) | fasta/genbank (专业格式)': 'Processing backend: auto (auto-select) | pipeline/paddleocr-vl (document) | sensevoice (audio) | video (video) | fasta/genbank (specialized formats)',
  '语言: auto/ch/en/korean/japan等': 'Language: auto/ch/en/korean/japan etc.',
  '解析方法: auto/txt/ocr': 'Parsing method: auto/txt/ocr',
  '是否启用公式识别': 'Enable formula recognition',
  '是否启用表格识别': 'Enable table recognition',
  '优先级，数字越大越优先': 'Priority, higher number means higher priority',
  '视频处理时是否保留提取的音频文件': 'Whether to keep extracted audio files during video processing',
  '是否启用视频关键帧OCR识别（实验性功能）': 'Enable video keyframe OCR recognition (experimental feature)',
  '关键帧OCR引擎: paddleocr-vl': 'Keyframe OCR engine: paddleocr-vl',
  '是否保留提取的关键帧图像': 'Whether to keep extracted keyframe images',
  '是否启用水印去除（支持 PDF/图片）': 'Enable watermark removal (supports PDF/images)',
  '水印检测置信度阈值（0.0-1.0，推荐 0.35）': 'Watermark detection confidence threshold (0.0-1.0, recommended 0.35)',
  '水印掩码膨胀大小（像素，推荐 10）': 'Watermark mask dilation size (pixels, recommended 10)',

  '是否上传图片到MinIO并替换链接（仅当任务完成时有效）': 'Whether to upload images to MinIO and replace links (only valid when task is completed)',
  '返回格式: markdown(默认)/json/both': 'Return format: markdown(default)/json/both',

  '筛选状态: pending/processing/completed/failed': 'Filter status: pending/processing/completed/failed',
  '返回数量限制': 'Return quantity limit',

  '清理N天前的任务': 'Clean up tasks older than N days',
  '超时时间（分钟）': 'Timeout (minutes)',

  // 应用描述
  '天枢 - 企业级 AI 数据预处理平台 | 支持文档、图片、音频、视频等多模态数据处理 | 企业级认证授权': 'Tianshu - Enterprise AI Data Preprocessing Platform | Supports multimodal data processing including documents, images, audio, video | Enterprise-grade authentication and authorization',
  '直接访问后端（推荐用于 API 测试）': 'Direct backend access (recommended for API testing)',
}

// 翻译函数
Object.assign(apiTranslations, {
  "修改当前用户密码": "Change current password",
  "用户需要提供旧密码和新密码。SSO 用户不能修改密码。": "Provide the current and new passwords. SSO users cannot change passwords here.",
  "读取 Key 级 webhook 回调配置": "Read API Key callback configuration",
  "使用该 Key 提交的任务进入终态时，向此地址推送通知（优先于全局 webhook）。": "Send terminal task notifications to this key's callback, ahead of the global default.",
  "敏感字段只回显掩码。": "Sensitive values are returned as masks.",
  "更新 Key 级 webhook 回调配置（Key 所有者自助，管理员可改任意 Key）": "Update a key's callback configuration (owner or administrator)",
  "敏感字段传掩码或缺省表示保持原值，传空字符串表示清除。": "Omit sensitive fields or send their mask to keep existing values; send an empty string to clear them.",
  "用该 Key 已保存的回调配置立即投递一条 webhook.test 事件": "Send a test notification using the key's saved callback configuration",
  "不走投递队列表，同步投递并返回结果；目标 URL 同样过 SSRF 校验。": "Deliver immediately and return the result, with the same destination validation as queued deliveries.",
  "列出全部 API Key（仅管理员）": "List all API Keys (administrator only)",
  "含归属用户名与 webhook 回调配置摘要，用于管理员掌握各对接方的回调配置情况。": "Includes owner names and callback summaries for administrators.",
  "获取系统配置": "Get system configuration",
  "公开接口，无需认证。返回系统名称、Logo、GitHub Star 显示、注册开关等配置。": "Public endpoint returning system branding, registration availability and display settings.",
  "更新系统配置 (管理员)": "Update system configuration (administrator)",
  "需要管理员权限。可以更新系统名称、Logo、GitHub Star 显示、注册开关等配置。": "Administrators can update branding, registration availability and display settings.",
  "请求体示例:": "Example request body:",
  "上传系统 Logo (管理员)": "Upload system logo (administrator)",
  "需要管理员权限。上传 Logo 图片文件到 RustFS，支持 PNG、JPG、SVG 等格式。": "Administrators can upload a logo to RustFS, including PNG, JPG and SVG files.",
  "【已废弃】图片已自动上传到 RustFS": "Deprecated: image storage is handled by the configured processing workflow",
  "【重构】彻底删除任务及其本地文件": "Permanently delete a task and its local files",
  "不仅取消 pending 的任务，还会物理抹除文件和数据库记录。": "Removes files and database records, rather than only cancelling a pending task.",
  "获取任务解析结果中实际被引用的图片列表": "List images referenced by the task's parsed result",
  "返回图片文件名和下载 URL，供外部系统（如 SuperRAG）下载图片到本地存储。": "Return filenames and authenticated download URLs for downstream image handoff.",
  "仅在任务状态为 completed 时返回图片信息。": "Image information is available when the task is completed.",
  "下载 URL 格式：/api/v1/files/output/{相对路径}": "Download URL: /api/v1/files/output/{relative-path}",
  "【重构】一键清理所有失败的任务，包含物理清除文件": "Delete all failed tasks and their local files",
  "重试失败的任务": "Retry a failed task",
  "暂停任务": "Pause a task",
  "恢复任务": "Resume a task",
  "清理任务缓存：仅删除 output 文件夹": "Clear cached output while retaining the task and source file",
  "筛选状态": "Filter by status",
  "页码": "Page number",
  "每页数量": "Items per page",
  "筛选后端引擎": "Filter by backend",
  "搜索文件名或任务ID": "Search filename or task ID",
  "提供上传源文件的访问服务": "Serve an authenticated source-file download or preview",
  "创建 API Key 请求": "API Key creation request",
  "API Key 创建响应 (只返回一次完整 key)": "API Key creation response (the full key is shown once)",
  "Key 级 webhook 回调配置（敏感字段回传掩码表示不修改）": "Key callback configuration; sending masks preserves sensitive values",
  "处理后端: pipeline, hybrid-auto-engine, vlm-auto-engine, hybrid-http-client, vlm-http-client, paddleocr-vl, etc.": "Processing backend: pipeline, hybrid-auto-engine, vlm-auto-engine, hybrid-http-client, vlm-http-client, paddleocr-vl, etc.",
  "语言: ch/en/auto...": "Language: ch/en/auto...",
  "起始页码（从0开始）": "Starting page (zero-based)",
  "结束页码": "Ending page",
  "[兼容旧版] 是否强制使用OCR": "Legacy option: force OCR",
  "远程服务器地址 (仅 Client 模式需要)": "Remote server URL (client modes)",
  "绘制布局边框 (_layout.pdf)": "Draw layout boxes (_layout.pdf)",
  "绘制文本边框 (_span.pdf)": "Draw text boxes (_span.pdf)",
  "输出 Markdown": "Write Markdown output",
  "输出中间 JSON": "Write intermediate JSON",
  "输出模型原始数据": "Write raw model output",
  "输出内容列表": "Write the content list",
  "保存原始/截取 PDF": "Save the source or selected-page PDF",
  "[兼容旧版] 是否绘制布局边框": "Legacy option: draw layout boxes",
  "[兼容旧版] 是否绘制文本Span边框": "Legacy option: draw text span boxes",
  "是否启用说话人分离": "Enable speaker diarization",
  "是否启用水印去除": "Enable watermark removal",
  "水印检测置信度阈值": "Watermark detection confidence threshold",
  "水印掩码膨胀大小": "Watermark mask dilation size",
  "是否将 Office 文件转换为 PDF 后再处理": "Convert Office documents to PDF before processing",
  "文档方向分类": "Classify document orientation",
  "文档去弯曲": "Unwarp document images",
  "是否启用版面分析": "Enable layout analysis",
  "是否启用图表识别": "Enable chart recognition",
  "是否启用印章识别": "Enable seal recognition",
  "是否对图像块进行OCR": "Run OCR on image blocks",
  "是否合并表格": "Merge tables",
  "是否重构标题层级": "Reconstruct heading levels",
  "版面形状模式": "Layout shape mode",
  "提示词标签": "Prompt label",
  "重复惩罚": "Repetition penalty",
  "温度": "Temperature",
  "最小像素": "Minimum image pixels",
  "最大像素": "Maximum image pixels",
  "是否启用版面 NMS": "Enable layout NMS",
  "是否重构页面": "Reconstruct pages",
  "忽略的标签 (逗号分隔)": "Ignored labels (comma-separated)",
  "是否上传图片到 RustFS 对象存储（任务级开关）。不传=由 RUSTFS_ENABLED 环境变量决定（向后兼容）；true=强制上传；false=保留图片在本地，通过 /files/output 下载": "Task-level RustFS option: omitted follows RUSTFS_ENABLED; true uploads images; false keeps local images available through authenticated /files/output downloads.",
  "可选任务回调地址；私网目标需管理员配置 host:port 白名单": "Optional task callback URL; private destinations require an administrator host:port allowlist entry.",
  "修改密码请求": "Password change request",
  "JWT Token 响应": "JWT response",
  "用户模型": "User model",
  "创建用户请求": "User creation request",
  "用户登录请求": "Login request",
  "用户角色枚举": "User roles",
  "更新用户请求": "User update request",
  "可选功能": "Optional features"
})

function translateText(text: string, lang: string): string {
  if (lang === 'zh-CN' || !text) {
    return text
  }
  return apiTranslations[text] || text.split('\n').map(line => apiTranslations[line.trim()] || line).join('\n')
}

// 递归翻译 OpenAPI schema
function translateOpenApiSpec(spec: any, lang: string): any {
  if (!spec || lang === 'zh-CN') {
    return spec
  }

  const translated = JSON.parse(JSON.stringify(spec))

  // 翻译基本信息
  if (translated.info) {
    if (translated.info.title) {
      translated.info.title = translateText(translated.info.title, lang)
    }
    if (translated.info.description) {
      translated.info.description = translateText(translated.info.description, lang)
    }
  }

  // 翻译服务器描述
  if (translated.servers) {
    translated.servers = translated.servers.map((server: any) => ({
      ...server,
      description: translateText(server.description, lang),
    }))
  }

  // 翻译标签
  if (translated.tags) {
    translated.tags = translated.tags.map((tag: any) => ({
      ...tag,
      name: translateText(tag.name, lang),
      description: tag.description ? translateText(tag.description, lang) : undefined,
    }))
  }

  // 翻译路径
  if (translated.paths) {
    Object.keys(translated.paths).forEach(path => {
      const pathItem = translated.paths[path]
      Object.keys(pathItem).forEach(method => {
        if (typeof pathItem[method] === 'object') {
          const operation = pathItem[method]

          // 翻译 summary 和 description
          if (operation.summary) {
            operation.summary = translateText(operation.summary, lang)
          }
          if (operation.description) {
            operation.description = translateText(operation.description, lang)
          }

          // 翻译标签
          if (operation.tags) {
            operation.tags = operation.tags.map((tag: string) => translateText(tag, lang))
          }

          // 翻译参数
          if (operation.parameters) {
            operation.parameters = operation.parameters.map((param: any) => ({
              ...param,
              description: param.description ? translateText(param.description, lang) : undefined,
            }))
          }

          // 翻译请求体
          if (operation.requestBody?.content) {
            Object.keys(operation.requestBody.content).forEach(contentType => {
              const content = operation.requestBody.content[contentType]
              if (content.schema?.properties) {
                Object.keys(content.schema.properties).forEach(propName => {
                  const prop = content.schema.properties[propName]
                  if (prop.description) {
                    prop.description = translateText(prop.description, lang)
                  }
                })
              }
            })
          }
        }
      })
    })
  }

  // Include referenced component schemas; preserve protocol defaults, enum values and examples.
  const translateMetadata = (value: any) => {
    if (!value || typeof value !== 'object') return
    for (const [key, child] of Object.entries(value)) {
      if (['default', 'enum', 'const', 'example', 'examples'].includes(key)) continue
      if (['title', 'summary', 'description'].includes(key) && typeof child === 'string') value[key] = translateText(child, lang)
      else translateMetadata(child)
    }
  }
  translateMetadata(translated)
  return translated
}

// 加载并翻译 OpenAPI 规范
async function loadOpenApiSpec() {
  try {
    const response = await fetch(`${window.location.origin}/api/v1/openapi.json`)
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}: ${response.statusText}`)
    }
    const spec = await response.json()
    openApiSpec.value = translateOpenApiSpec(spec, locale.value)
    isLoading.value = false
  } catch (error: any) {
    loadError.value = `${t('apiDocs.loadError')}: ${error.message}`
    isLoading.value = false
    console.error('OpenAPI 文档加载失败:', error)
  }
}

// 监听语言变化，重新翻译
watch(locale, async () => {
  if (openApiSpec.value) {
    // 重新加载并翻译
    await loadOpenApiSpec()
  }
})

// Scalar 配置（响应式）
const scalarConfig = computed(() => ({
  // OpenAPI 规范（使用翻译后的版本）
  spec: {
    content: openApiSpec.value,
  },

  // 主题配置 - 使用 Scalar 默认主题
  theme: 'default',

  // 布局配置 - 使用现代布局
  layout: 'modern' as 'modern',

  // 显示配置
  showSidebar: true,
  darkMode: false,

  // 隐藏不需要的元素
  hiddenClients: [], // 可以隐藏特定语言的客户端示例

  // 默认 HTTP 客户端
  defaultHttpClient: {
    targetKey: 'javascript',
    clientKey: 'fetch',
  },

  // 认证配置
  authentication: {
    preferredSecurityScheme: 'bearerAuth',
    http: {
      bearer: {
        token: authStore.token || '',
      },
    },
    apiKey: {
      token: authStore.token || '',
    },
  },

  // 服务器配置 - 提供前端代理和后端直连两种方式
  servers: [
    {
      url: window.location.origin,
      description: '通过前端代理访问（推荐）',
    },
    {
      url: `${window.location.protocol}//${window.location.hostname}:8000`,
      description: '直接访问后端（用于 API 测试）',
    },
  ],

  // 其他配置
  searchHotKey: 'k',
  withDefaultFonts: true,
}))

// Scalar 就绪回调
function onScalarReady() {
  console.log('✅ Scalar API Reference 已加载')
}

// 重试加载
function retryLoad() {
  isLoading.value = true
  loadError.value = ''
  loadOpenApiSpec()
}

// 组件挂载时初始化
onMounted(async () => {
  // 加载并翻译 OpenAPI 文档
  await loadOpenApiSpec()
})
</script>

<style scoped>
/* 最小化样式，让 Scalar 完全控制 */
.scalar-container {
  width: 100%;
  height: calc(100vh - 140px);
  position: relative;
  /* 隔离 Scalar 样式，防止污染全局 */
  isolation: isolate;
}

/* 加载状态样式 */
.loading-state {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 400px;
  background: #ffffff;
  border-radius: 8px;
  border: 1px solid #e5e7eb;
}

.loading-content {
  text-align: center;
}

.loading-spinner {
  width: 48px;
  height: 48px;
  margin: 0 auto 16px;
  border: 2px solid #e5e7eb;
  border-top-color: #3b82f6;
  border-radius: 50%;
  animation: spin 1s linear infinite;
}

@keyframes spin {
  to { transform: rotate(360deg); }
}

.loading-content p {
  color: #6b7280;
  font-size: 14px;
}

/* 错误状态样式 */
.error-state {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 400px;
  background: #ffffff;
  border-radius: 8px;
  border: 1px solid #e5e7eb;
}

.error-content {
  text-align: center;
  padding: 48px;
}

.error-icon {
  width: 64px;
  height: 64px;
  margin: 0 auto 16px;
  color: #ef4444;
}

.error-content h3 {
  font-size: 18px;
  font-weight: 600;
  color: #111827;
  margin-bottom: 8px;
}

.error-content p {
  color: #6b7280;
  margin-bottom: 16px;
}

.retry-button {
  padding: 8px 16px;
  background: #3b82f6;
  color: white;
  border: none;
  border-radius: 8px;
  cursor: pointer;
  font-size: 14px;
  font-weight: 500;
  transition: background 0.2s;
}

.retry-button:hover {
  background: #2563eb;
}
</style>

<style>
/* 全局样式保护：覆盖 Scalar 可能污染的样式 */
/* 使用高优先级选择器确保应用样式不被破坏 */

/* 保护 Tailwind 蓝色按钮 */
button.bg-blue-600,
a.bg-blue-600,
.bg-blue-600 {
  background-color: rgb(37 99 235) !important;
  color: white !important;
}

button.bg-blue-500,
a.bg-blue-500,
.bg-blue-500 {
  background-color: rgb(59 130 246) !important;
  color: white !important;
}

button.bg-blue-700,
a.bg-blue-700,
.bg-blue-700 {
  background-color: rgb(29 78 216) !important;
  color: white !important;
}

button.hover\:bg-blue-700:hover,
a.hover\:bg-blue-700:hover,
.hover\:bg-blue-700:hover {
  background-color: rgb(29 78 216) !important;
}

button.hover\:bg-blue-600:hover,
a.hover\:bg-blue-600:hover,
.hover\:bg-blue-600:hover {
  background-color: rgb(37 99 235) !important;
}

/* 保护 btn-primary 类 */
.btn-primary {
  background: linear-gradient(to right, rgb(37 99 235), rgb(29 78 216)) !important;
  color: white !important;
}

.btn-primary:hover {
  background: linear-gradient(to right, rgb(29 78 216), rgb(30 64 175)) !important;
}

/* 保护头像和其他圆形元素 */
.rounded-full {
  border-radius: 9999px !important;
}

/* 保护文本颜色 */
.text-white {
  color: white !important;
}

/* 保护背景渐变 */
.bg-gradient-to-r {
  background-image: linear-gradient(to right, var(--tw-gradient-stops)) !important;
}

.bg-gradient-to-br {
  background-image: linear-gradient(to bottom right, var(--tw-gradient-stops)) !important;
}

/* 保护 from-blue-* 和 to-blue-* 渐变颜色 */
.from-blue-500 {
  --tw-gradient-from: rgb(59 130 246) !important;
  --tw-gradient-to: rgb(59 130 246 / 0) !important;
  --tw-gradient-stops: var(--tw-gradient-from), var(--tw-gradient-to) !important;
}

.from-blue-600 {
  --tw-gradient-from: rgb(37 99 235) !important;
  --tw-gradient-to: rgb(37 99 235 / 0) !important;
  --tw-gradient-stops: var(--tw-gradient-from), var(--tw-gradient-to) !important;
}

.to-blue-500 {
  --tw-gradient-to: rgb(59 130 246) !important;
}

.to-blue-600 {
  --tw-gradient-to: rgb(37 99 235) !important;
}

.to-blue-700 {
  --tw-gradient-to: rgb(29 78 216) !important;
}

.to-blue-800 {
  --tw-gradient-to: rgb(30 64 175) !important;
}

/* 保护其他渐变颜色（用于角色标签） */
.from-red-500 {
  --tw-gradient-from: rgb(239 68 68) !important;
  --tw-gradient-to: rgb(239 68 68 / 0) !important;
  --tw-gradient-stops: var(--tw-gradient-from), var(--tw-gradient-to) !important;
}

.to-red-600 {
  --tw-gradient-to: rgb(220 38 38) !important;
}

.from-yellow-500 {
  --tw-gradient-from: rgb(234 179 8) !important;
  --tw-gradient-to: rgb(234 179 8 / 0) !important;
  --tw-gradient-stops: var(--tw-gradient-from), var(--tw-gradient-to) !important;
}

.to-yellow-600 {
  --tw-gradient-to: rgb(202 138 4) !important;
}
</style>
