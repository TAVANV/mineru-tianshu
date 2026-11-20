# MinerU Tianshu 后端

企业级 AI 数据预处理平台后端，基于 FastAPI + LitServe 构建。

支持文档、图片、音频、视频等多模态数据处理，GPU 负载均衡，任务队列管理。

## 🚀 快速开始

### 安装依赖

```bash
cd backend
pip install -r requirements.txt
```

### 启动服务

**一键启动所有服务（推荐）:**

```bash
python start_all.py

# 启用 MCP 协议支持（用于 AI 助手调用）
python start_all.py --enable-mcp
```

**自定义配置启动:**

```bash
python start_all.py --workers-per-device 2 --devices 0,1
```

**分别启动各服务:**

```bash
# 1. 启动 API Server
python api_server.py

# 2. 启动 LitServe Workers
python litserve_worker.py --workers-per-device 2

# 3. 启动 Task Scheduler (可选)
python task_scheduler.py --enable-scheduler
```

### 任务调度

`FAIR_SCHEDULING_ENABLED` 默认为 `true`。Worker 先领取优先级更高的任务，同优先级时优先选择当前执行任务最少的用户，占用相同则先领取创建更早的任务。没有用户 ID 的任务按同一个匿名用户统计。执行中的子任务计入所属用户的占用，等待子任务完成的父任务不计入占用。

只有一个用户有任务待处理时，可以用满 Worker 名额。公平调度在 Worker 领取下一个任务时生效。正在执行的任务会继续运行，新用户要等到有空闲名额。

Redis 有序集合的分数由优先级和时间决定。为使排序反映各用户的实时占用量，公平模式在同一个 SQLite 事务内统计占用并认领任务。此模式下任务直接在 SQLite 入队，Worker 清理旧的 Redis 待处理和执行中索引。队列状态以 SQLite 为准。

设为 `false` 可恢复原优先级和时间顺序。若同时启用了 `REDIS_QUEUE_ENABLED`，Worker 会先从 SQLite 补入 Redis 缺失的待处理任务。切换时使用同一配置重启 API 和 Worker。原生部署修改 `backend/.env`，Compose 部署修改仓库根目录的 `.env`。

## 📁 项目结构

```
backend/
├── api_server.py           # FastAPI API 服务器
├── task_db.py              # SQLite 数据库管理
├── litserve_worker.py      # LitServe Worker Pool
├── task_scheduler.py       # 任务调度器 (可选)
├── mcp_server.py           # MCP 协议服务器 (可选)
├── start_all.py            # 统一启动脚本
├── client_example.py       # Python 客户端示例
├── requirements.txt        # Python 依赖
├── README.md               # 后端文档
└── MCP_GUIDE.md            # MCP 协议详细指南
```

## 📡 API 接口

完整 API 文档: <http://localhost:8000/docs>

### 任务管理

#### 提交任务

```
POST /api/v1/tasks/submit

参数:
  - file: 文件 (必需)
  - backend: pipeline | vlm-transformers | vllm-engine (默认: pipeline)
  - lang: ch | en | korean | japan (默认: ch)
  - method: auto | txt | ocr (默认: auto)
  - formula_enable: boolean (默认: true)
  - table_enable: boolean (默认: true)
  - priority: 0-100 (默认: 0)
  - password: 加密文档打开密码（可选，仅加密 PDF / Office / ZIP 需要；
    解密失败任务报错，可通过重试补填正确密码；密码不出现在任何查询接口响应中）

返回:
  {
    "success": true,
    "task_id": "uuid",
    "status": "pending",
    "message": "Task submitted successfully",
    "file_name": "document.pdf",
    "created_at": "2024-01-01T00:00:00"
  }
```

#### 查询任务状态

```
GET /api/v1/tasks/{task_id}?upload_images=false

返回:
  {
    "success": true,
    "task_id": "uuid",
    "status": "completed",
    "file_name": "document.pdf",
    "backend": "pipeline",
    "priority": 0,
    "error_message": null,
    "created_at": "2024-01-01T00:00:00",
    "started_at": "2024-01-01T00:00:10",
    "completed_at": "2024-01-01T00:01:00",
    "worker_id": "tianshu-worker-1",
    "retry_count": 0,
    "data": {
      "markdown_file": "document.md",
      "content": "# Document\n\n...",
      "images_uploaded": false,
      "has_images": true
    }
  }
```

#### 取消任务

```
DELETE /api/v1/tasks/{task_id}

返回:
  {
    "success": true,
    "message": "Task cancelled successfully"
  }
```

#### 重试失败任务

```
POST /api/v1/tasks/{task_id}/retry

参数:
  - password: 可选表单参数，仅对 failed 状态任务生效。加密文档因密码错误失败时，
    传入正确密码会覆盖任务 options 中的原密码后重新入队；留空表示沿用原密码。

返回:
  {
    "success": true,
    "message": "Task submitted for retry",
    "retried_subtasks": 0
  }
```

#### 获取任务列表

```
GET /api/v1/queue/tasks?status=pending&limit=100

返回:
  {
    "success": true,
    "count": 10,
    "tasks": [...]
  }
```

### 队列管理

#### 获取队列统计

```
GET /api/v1/queue/stats

返回:
  {
    "success": true,
    "stats": {
      "pending": 5,
      "processing": 2,
      "completed": 100,
      "failed": 3,
      "cancelled": 1
    },
    "total": 111,
    "timestamp": "2024-01-01T00:00:00"
  }
```

#### 重置超时任务

```
POST /api/v1/admin/reset-stale?timeout_minutes=60

返回:
  {
    "success": true,
    "reset_count": 2,
    "message": "Reset tasks processing for more than 60 minutes"
  }
```

#### 清理旧任务

```
POST /api/v1/admin/cleanup?days=7

返回:
  {
    "success": true,
    "deleted_count": 50,
    "message": "Cleaned up tasks older than 7 days"
  }
```

#### 健康检查

```
GET /api/v1/health

返回:
  {
    "status": "healthy",
    "timestamp": "2024-01-01T00:00:00",
    "database": "connected",
    "queue_stats": {...}
  }
```

## 🔧 配置说明

### 启动参数

```bash
python start_all.py [选项]

选项:
  --output-dir PATH                 输出目录 (默认: /tmp/mineru_tianshu_output)
  --api-port PORT                   API端口 (默认: 8000)
  --worker-port PORT                Worker端口 (默认: 9000)
  --accelerator TYPE                加速器类型: auto/cuda/cpu/mps (默认: auto)
  --workers-per-device N            每个GPU的worker数 (默认: 1)
  --devices DEVICES                 使用的GPU设备 (默认: auto)
  --poll-interval SECONDS           Worker拉取任务间隔 (默认: 0.5秒)
  --enable-scheduler                启用可选的任务调度器
  --monitor-interval SECONDS        调度器监控间隔 (默认: 300秒)
  --cleanup-old-files-days N        清理N天前的结果文件 (默认: 7天)
  --enable-mcp                      启用 MCP 协议服务器
  --mcp-port PORT                   MCP 服务器端口 (默认: 8001)
```

### 环境变量

```bash
# API Server 端口
export API_PORT=8000

# MCP Server 配置 (可选)
export MCP_PORT=8001
export MCP_HOST=0.0.0.0
export API_BASE_URL=http://localhost:8000

# MinIO 配置 (可选)
export MINIO_ENDPOINT="your-endpoint.com"
export MINIO_ACCESS_KEY="your-access-key"
export MINIO_SECRET_KEY="your-secret-key"
export MINIO_BUCKET="your-bucket"

# MinerU 显存配置
export MINERU_VIRTUAL_VRAM_SIZE=6
```

### 任务取消与超时回收

每个 LitServe worker 只保留一个活动引擎解释器。引擎在独立启动的 Python 进程中初始化 CUDA/MPS 和模型，正常任务之间复用引擎缓存；另一个不加载模型的监督进程负责停止和回收整个执行树。通过 exec 启动新解释器，不继承 LitServe 进程中的 CUDA 运行状态，引擎子进程也不受 LitServe daemon 进程不能创建 multiprocessing 子进程的限制。

取消 API 仍立即返回 `cancelled`。取消、手动重试和调度器超时重排都会撤销当前 `execution_id`，worker 每 `TASK_EXECUTION_POLL_INTERVAL` 秒（默认 0.25 秒）检测一次；发现撤销后，先向该执行树发送 SIGTERM，等待 `TASK_EXECUTION_TERMINATE_GRACE` 秒（默认 2 秒），再向仍存活的进程发送 SIGKILL。监督进程最多再等待 5 秒确认退出；在操作系统正常响应信号时，从数据库撤销到确认回收的窗口不超过约 7.25 秒，另加少量进程通信与调度开销。不可中断的内核 I/O 等情况若仍无法确认退出，该 worker 停止领取任务并保留执行锁，不会把未停止的进程当作空闲容量。

上述两个配置接受大于 0 且不超过 60 的秒数。原生运行配置 `backend/.env`；Compose 部署配置根 `.env`。超时起点和自动重试上限仍由已有任务调度器及系统配置决定；这里的回收窗口从调度器实际撤销执行时开始计算，`WORKER_TIMEOUT` 不是任务执行时限。

数据库事务内校验执行令牌，防止迟到结果、失败回调和拆分操作覆盖取消或新一轮尝试。数据库旁的 `.execution-locks/` 使用 POSIX 文件锁，并把锁描述符传给监督进程和引擎；同一任务在其他 worker 的重试必须等旧执行实际退出才能认领，因此不会并发写同一输出目录。数据库和锁目录需要与原 SQLite 文件一起位于本机共享挂载，不能放在 NFS/CIFS；零字节锁文件保留以维持 inode 身份，不应在 worker 运行时删除。

父任务等待子任务期间不占执行槽位、不参与普通超时；最后一个子任务完成后，父任务重新入队并独立认领合并执行，合并也支持取消和超时。Linux 监督进程启用 subreaper，回收被杀执行遗留的子孙进程，包括另建 session 的转换工具；LitServe 进程意外退出时，监督进程通过控制连接断开停止执行树。远程 vLLM/外部模型服务不会被停止，已经发出的远端请求是否中止取决于服务端；本地进程和槽位可回收。

需要中断执行进程的取消或超时会丢弃该 worker 的引擎内存缓存，下一任务需要重新加载模型；宿主机已有模型权重和下载缓存继续复用。正常完成和普通引擎异常不重建解释器，并发上限仍由 `MAX_CONCURRENT_TASKS` 控制。CPU 回归验证覆盖真实阻塞、占用 GIL 的正则、SIGTERM 不响应、独立 session 子进程、超时重试、父子合并和多 worker 隔离；CUDA/MPS 实机、LitServe 完整服务和远端服务取消仍需部署前验证。

### 数据库

项目使用 SQLite 数据库 (`mineru_tianshu.db`),自动创建,无需手动配置。

## 🎯 核心功能

### 支持的解析引擎

| 引擎 | 名称 | 特点 | 适用场景 |
|------|------|------|----------|
| `pipeline` | MinerU Pipeline | 完整文档解析，支持表格、公式 | 通用文档处理 |
| `sensevoice` | SenseVoice | 音频转文字，说话人识别 | 音频处理 |
| `video` | Video Engine | 视频处理，关键帧 OCR | 视频分析 |
| `fasta` | FASTA Engine | 生物序列格式解析 | 生物信息学 |
| `genbank` | GenBank Engine | 基因序列格式解析 | 生物信息学 |

### Worker 主动拉取模式

- Workers 持续循环拉取任务,无需调度器触发
- 默认 0.5 秒拉取间隔,响应速度极快
- 空闲时自动休眠,不占用 CPU 资源

### 并发安全

- 使用 `BEGIN IMMEDIATE` 和原子操作
- 防止任务重复处理
- 支持多 Worker 并发拉取

### 多解析器支持

- **MinerU**: 完整文档解析，支持 PDF、图片、DOCX/XLSX/PPTX 及旧版 Office 转换 (GPU 加速)
- **MarkItDown**: 处理 HTML、文本、CSV 等轻量格式 (快速处理)

### 自动清理

- 自动清理旧结果文件 (默认 7 天)
- 保留数据库记录供查询
- 可配置清理周期或禁用

## 🐍 Python 客户端示例

```
from client_example import TianshuClient
import asyncio

async def main():
    client = TianshuClient('http://localhost:8000')

    async with aiohttp.ClientSession() as session:
        # 提交任务
        result = await client.submit_task(
            session,
            file_path='document.pdf',
            backend='pipeline',
            lang='ch',
            formula_enable=True,
            table_enable=True
        )

        task_id = result['task_id']

        # 等待完成
        final_status = await client.wait_for_task(session, task_id)

        print(f"Task completed: {final_status}")

if __name__ == '__main__':
    asyncio.run(main())
```

## 🔍 故障排查

### Worker 无法启动

检查 GPU:

```bash
nvidia-smi
```

检查依赖:

```bash
pip list | grep -E "(mineru|litserve|torch)"
```

### 任务一直 pending

检查 Worker 是否运行:

```bash
# Windows
tasklist | findstr python

# Linux/Mac
ps aux | grep litserve_worker
```

检查 Worker 健康状态:

```bash
curl -X POST http://localhost:9000/predict \
  -H "Content-Type: application/json" \
  -d '{"action":"health"}'
```

### 显存不足

减少 worker 数量:

```bash
python start_all.py --workers-per-device 1
```

设置显存限制:

```bash
export MINERU_VIRTUAL_VRAM_SIZE=6
python start_all.py
```

## 🔌 MCP 协议支持

### 什么是 MCP？

Model Context Protocol (MCP) 是 Anthropic 推出的开放协议，让 AI 助手（如 Claude Desktop）可以直接调用外部工具和服务。

### 启用 MCP Server

```bash
python start_all.py --enable-mcp
```

MCP Server 将在 `http://localhost:8001` 启动，提供以下端点：

- `/sse` - SSE 连接端点（MCP 客户端连接）
- `/messages` - POST 消息端点
- `/health` - 健康检查端点

### 可用工具

1. **parse_document** - 解析文档为 Markdown 格式
2. **get_task_status** - 查询任务状态和结果
3. **list_tasks** - 列出最近的任务
4. **get_queue_stats** - 获取队列统计信息

### 详细文档

完整的 MCP 配置和使用指南，请参考：

- [MCP_GUIDE.md](MCP_GUIDE.md) - MCP 详细指南
- [主 README](../README.md#mcp-协议集成) - 快速配置指南

## 📄 许可证

遵循 MinerU 主项目许可证

### MCP 外部地址白名单

`MCP_HOST` 控制监听地址。通过非回环地址或反向代理访问 `/sse`、`/messages` 时，还需配置逗号分隔的 `MCP_ALLOWED_HOSTS`（实际请求的 Host，含端口）及 `MCP_ALLOWED_ORIGINS`（浏览器客户端的 Origin），例如：

```bash
MCP_HOST=0.0.0.0
MCP_ALLOWED_HOSTS=tianshu.example.com:8002,localhost:*,127.0.0.1:*
MCP_ALLOWED_ORIGINS=https://tianshu.example.com,http://localhost:8080
```

未配置或为空时沿用监听地址与回环地址的默认白名单。DNS 重绑定防护始终开启，未列出的 Host 返回 421，未列出的 Origin 返回 403；访问密钥校验仍独立生效。
