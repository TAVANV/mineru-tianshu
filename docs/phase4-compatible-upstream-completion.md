# 第四批：保留现有工作模式的上游补齐

## 范围与约束

开发分支：`feat/upstream-integration-20260915`。保留前三批提交；`main` 仍为 `1438b59`。

本批按“旧入口可继续使用，新的运行方式显式选择”完成代码集成：

| 能力 | 实现及兼容策略 |
| --- | --- |
| 文件鉴权性能 | 独立路径索引＋SQLite 触发器，旧记录首次回填，之后只规范化变更路径。输出查询按目录祖先索引匹配，支持 ZIP → PDF → 分片；删除、清缓存和回滚同步维护。保留完整路径校验。 |
| API Key 级回调 | 移植上游字段、管理接口和回调弹窗；支持签名、Bearer、Basic、自定义鉴权头，管理员可查看和管理全部接入方。旧全局回调保留。 |
| 多接入方 MCP | 可选 `MCP_API_KEY_MAP` 将不同 MCP 访问密钥映射到不同后端 API Key，使用独立服务账户实现任务隔离，并向 SDK 提供身份以约束 SSE 会话归属。原 MCP_API_KEYS＋TIANSHU_API_KEY 方式继续有效。 |
| MinerU | 新构建固定 3.4.5；同步 VLM 工具、pypdf、SciPy 约束。PaddleOCR、音视频和原 CUDA/Paddle 安装路径保留。 |
| 新 VLM／离线下载 | 新增 `mineru_vlm_pro`，使用 `MinerU2.5-Pro-2605-1.2B`。保留旧 `mineru_vlm` 的含义、目录和默认选择；两条下载入口均可增量补充。 |
| Pipeline 模型升级 | 按 MinerU 3.4.5 实际资源配置校验 PP-OCRv6 与表格模型，避免把只有旧 OCRv5 的目录误判完整。补下载不删除旧权重。旧 en/latin/japan/chinese_cht 选项映射到新版 ch OCRv6 路径。 |
| Office 原生解析 | 新增 `office_parser=mineru`，支持 DOCX/XLSX/PPTX 后缀原样传递。默认 `compatible` 继续使用现有 XLSX/PPTX 特殊兼容解析；Office→PDF 开关保留。 |
| MPS | Worker 自动识别 Apple Silicon MPS；新增本地安装依赖和入口。MinerU 使用 MPS，辅助音视频使用 CPU；PaddleOCR 的 CUDA 路径保留并明确拒绝 MPS 冒充 CUDA。 |
| 部署便利性 | 新增统一入口、Pipeline 覆盖配置、显存/内存/并发预算、可选宿主机 UID 运行。旧部署脚本与生产/CPU/离线/镜像仓库配置均保留。 |
| 构建与缓存 | 支持 pip 镜像源、超时、重试和本地 wheel；常用模型缓存持久化。增加写权限预检，不对旧模型目录做递归 chmod/chown。 |
| 界面与英文 | 移植上游全局布局、回调弹窗和语言包，补齐 PaddleOCR、Office 和自定义预览的英文。设置中心增加导航，默认仍展示原有全部表单；旧页面 URL 不变。 |
| 其他文件防护 | 清理只能作用于配置的上传/输出目录，允许输出目录中的分片源文件；可选内容签名校验默认关闭，以兼容既有上传客户端。 |

**本次没有下载任何模型权重、启动 GPU Worker 或部署生产服务。** 模型下载回归使用临时文件与下载函数替身。依赖校验读取了包元数据和 MinerU 代码 wheel，未执行模型 SDK 下载。

## 离线模型：继续使用原来的目录

### 推荐的增量升级命令（以后执行，本次未执行）

已有模型位于 `/data/modelfiles` 时，一次补齐 Pipeline 3.4 所需资源和新 VLM，并显式启用新 VLM：

```bash
python backend/download_models.py \
  --output /data/modelfiles \
  --models mineru_pipeline,mineru_vlm_pro \
  --vlm-model mineru_vlm_pro \
  --strict

python backend/download_models.py \
  --output /data/modelfiles \
  --models mineru_pipeline,mineru_vlm_pro \
  --verify-only --strict
```

把目录换为现用的 `models-offline` 或其他目录即可；不要求迁移模型存储位置。

- 省略 `--vlm-model`：只补文件，不切换现有 VLM。
- `--models mineru_vlm`：仍表示旧的 `MinerU2.5-2509-1.2B`，没有偷换含义。
- 已验证文件继续跳过；缺少 OCRv6 时对原 Pipeline 仓库目录执行 SDK 增量下载。旧 OCRv5、旧 VLM 和其他模型不删除。
- 现有 manifest 的模型记录合并保留；配置和 manifest 原子写入。权重索引缺少分片、失败的下载或失败的配置替换不会把未完成内容当作可用模型，也不会切换当前 VLM。
- `mineru.json` 保留自定义字段与路径，新增明确的 `model-source: local`、`config_version: 1.3.2`。仅修正已知旧路径 `/app/models/PDF-Extract-Kit-1.0/models` 的重复 models 层级。
- `--verify-only` 不下载模型，仍按原脚本习惯写验证 manifest。

### 原宿主机脚本也保留

原命令继续有效，音频的 `modelscope_cache`、PaddleX、YOLO/LaMa 和字体目录保持原布局：

```bash
python backend/prepare_offline_models.py /data/modelfiles
```

该命令仍运行原来的各下载阶段，并通过 ModelScope 补齐 Pipeline 仓库；不自动切换到 Pro VLM。新增只补 Pro VLM 的选项：

```bash
python backend/prepare_offline_models.py /data/modelfiles \
  --only mineru-vlm-pro --vlm-model mineru_vlm_pro
```

**只补 VLM 不等于完成 Pipeline 升级。** 如果没运行原完整准备命令，应另外补 `--models mineru_pipeline`，或使用上面的推荐组合命令。

原 `scripts/build-offline.sh` 打包整个模型目录的流程不变，新模型会随现有目录一起打包。镜像仓库部署的外部模型只读挂载也不变。

### 运行时选择

- API/Worker 离线校验读取 `mineru.json` 的当前 VLM 路径。
- 基础 Compose 的 vLLM 启动适配器也读取同一配置，将 `/app/models/...` 对应到 vLLM 容器的 `/models/...`。
- `MINERU_VLM_MODEL_DIR` 可显式覆盖选择；空值跟随配置。没有配置时仍回退旧目录。
- 旧模型包在容器中只读挂载时，配置升级写入运行用户 HOME 下的副本，不修改模型包源文件。
- 已存在的 vLLM 容器需要重建一次以采用新的启动入口；此命令只创建/更新容器，不启动模型服务：

```bash
docker compose --profile manual up --no-start --no-deps vllm-mineru
```

之后仍由原 Worker 的互斥与冷启动逻辑启停，PaddleOCR 容器不被移除。

## API Key 回调的规则

优先级：**任务显式 webhook_url → 启用的 Key 级回调 → 原管理员全局默认回调**。Key 级配置关闭时继续继承全局默认，不改变原有默认回调工作方式。

入口：个人资料 → API Token 管理 → 回调按钮；管理员在系统配置 → 接入方回调查看全部 Key。

```text
GET  /api/v1/auth/apikeys/{key_id}/webhook
PUT  /api/v1/auth/apikeys/{key_id}/webhook
POST /api/v1/auth/apikeys/{key_id}/webhook/test
GET  /api/v1/auth/admin/apikeys
```

配置字段与上游一致：`enabled/url/secret/auth_type/auth_token/auth_username/auth_password/auth_header_name/auth_header_value`。缺省或 `********` 保留敏感字段，空字符串清除。受限 API Key 不能修改其他凭据的回调设置，管理界面使用登录会话。

投递仍使用第二批的报文、`X-Tianshu-Signature`、`X-Tianshu-Event-Id` 和至少一次语义，不切换成上游另一套协议。配置在提交时快照，后续改 Key 或删除 Key 不改变已经提交任务的通知。任务和投递记录保留来源 Key。

任务覆盖到不同目标时不继承 Key 或全局凭据；即使覆盖目标碰巧与全局地址相同，也不把全局凭据带给 Key 的任务覆盖。内网 host:port 白名单、DNS 固定、无重定向、重试与三层任务终态处理继续沿用。自定义鉴权头禁止改写 Host、Content-Length、传输控制或天枢签名头，拒绝换行注入。

## 可选部署与原生运行

所有新入口默认只输出计划，不启动服务：

```bash
./setup.sh --mode full --action plan
./setup.sh --mode pipeline --auto-budget --action plan
./scripts/deploy-pipeline.sh --action plan
```

明确部署时使用 `--action config/build/up/status/check/down`。`check` 检查直接 API 与前端代理的健康地址。先沿用现有 `.env` 中的真实 JWT 配置；本脚本不覆盖 `.env` 或自动替换现有密钥。

Pipeline 模式只准备 Pipeline 模型、不启用 vLLM 参数；API/Worker 明确拒绝此模式下的 VLM/音视频任务，避免意外加载未准备的模型。全功能模式的引擎选择不变。

`--auto-budget` 参考上游计算 GPU 并发、每进程显存预算、Worker 内存上限；已有 `MAX_CONCURRENT_TASKS`、`WORKER_MEMORY_LIMIT` 等显式配置优先。小内存机器的预算不超过宿主机内存。它是配置预算，不预分配内存，也不保证任意文档都能在该预算内处理。

可选 `--run-as-user` 叠加 `docker-compose.user.yml`，使用宿主机 UID/GID 和独立 runtime-home；适用于基础/流水线部署。Docker socket 的补充组使用实际 GID。默认仍为原运行用户；既有 CPU/离线/registry 挂载方式不被切换。已有只读或异主数据目录应先按现有运维方式处理权限，脚本不会递归改旧目录权限。

弱网构建可设置 `PIP_MIRROR/PIP_TIMEOUT/PIP_RETRIES`，或在构建时传对应 build-arg；`backend/wheels/` 可放匹配当前平台/ABI 的 wheel。常规依赖优先查本地 wheel，FlashAttention 无本地 wheel 时保留原 URL 回退。离线分发仍采用原构建/打包脚本；没有声称 apt/pip 的首次镜像构建可以完全断网。

原生 Apple Silicon 安装入口（本次未执行安装）：

```bash
./scripts/install-native.sh /path/to/venv
```

安装文件只含原生解析/API 所需依赖，不安装 CUDA/PaddleGPU 或下载模型。准备原生模型配置时，使用 `download_models.py --runtime-model-root /绝对模型目录`；通过绝对路径的 `MINERU_TOOLS_CONFIG_JSON` 指向该配置。使用 `start_all.py --accelerator mps`，或使用 `auto` 自动识别。原生精简安装建议设置 `TIANSHU_DEPLOY_MODE=pipeline`；完整 CUDA/Paddle/音视频部署继续使用现有容器。

## 依赖审查中的必要适配

不能直接照搬上游 `mineru[core]`：3.4.5 的 core 会引入 Gradio，而其允许的 Gradio 版本要求 Starlette <1.0，与第三批安全版本冲突。本批使用 `mineru[pipeline,vlm,s3]==3.4.5` 保留实际解析/S3 能力，继续使用天枢现有 Vue 界面。独立 vLLM 服务继续保留，没有把 CUDA 推理依赖塞回 Worker。

同时根据 3.4.5 的发布包资源配置核对 OCRv6 文件名与旧语言路由，而不是只替换版本号。依据：[MinerU 3.4.5 官方发布包](https://pypi.org/project/mineru/3.4.5/)、该版本 wheel 内的 `models_config.yml` 与 `enum_class.py`。

## 来源与差异

- [11d4bb3](https://github.com/magicyuan876/mineru-tianshu/commit/11d4bb3)、[f331ce0](https://github.com/magicyuan876/mineru-tianshu/commit/f331ce0)：复用 Key 字段、接口、凭据头与弹窗；保留本地全局回调、内网配置、快照和可靠投递模块。
- [34617d7](https://github.com/magicyuan876/mineru-tianshu/commit/34617d7)：移植全局布局和英文包，保留 PaddleOCR/RustFS/Office 自定义项；设置导航包裹原表单，未覆盖成不同配置协议。
- [612036d](https://github.com/magicyuan876/mineru-tianshu/commit/612036d)、[3e9cb65](https://github.com/magicyuan876/mineru-tianshu/commit/3e9cb65)：Pipeline 覆盖配置和预算策略；统一入口负责分发现有部署文件，未替换旧部署方案或设备绑定。
- [c5cddf0](https://github.com/magicyuan876/mineru-tianshu/commit/c5cddf0)、[648e5d6](https://github.com/magicyuan876/mineru-tianshu/commit/648e5d6)：本地 wheel、网络参数与模型目录写权限预检；不采用对既有数据递归开放所有用户写权限的做法。
- [c7bb1d5](https://github.com/magicyuan876/mineru-tianshu/commit/c7bb1d5)、[e7d37b1](https://github.com/magicyuan876/mineru-tianshu/commit/e7d37b1)：MinerU/Office/MPS 和离线配置适配；保留旧下载脚本、旧模型、特殊 Office、PaddleOCR 与容器互斥。
- 路径索引、两种离线目录布局的衔接、MCP 每会话服务凭据隔离为本地适配，原因是上游实现未覆盖这些定制需求。

## 验证

- **123 项自动化测试通过**。新增覆盖 Key 回调快照和隔离、索引触发器/回滚、模型补下载与旧缓存保留、OCRv5→v6 增量补齐、原子配置写失败、下载中断、原生 Office 选择和后缀、MPS 自动识别、MCP 服务凭据隔离、可选上传签名、目录清理边界、中英文键与英文完整性、MCP 跨客户端会话拒绝。
- Playwright 使用临时数据库验证英文设置/个人资料、Key 回调保存与掩码、390px 移动端弹窗、源图与 Markdown 图片鉴权预览。测试图片成功加载，注入的 onerror 未执行；没有模型调用或生产数据操作。当前 OpenAPI 描述、标题及摘要也已验证英文转换后无遗漏中文。
- 前端生产构建、Ruff、Python 编译、Shell 语法与 diff 检查通过。
- 9 组 Compose 配置校验通过，包括生产、CPU、离线、开发、Pipeline、宿主机用户组合和两套 registry 配置。
- 依赖解析：Apple Silicon 133 包、Linux 共用环境 224 包、含现有 PaddleOCR/Torch 约束的 Linux 环境 259 包均有可解结果。这不是实际 CUDA 镜像构建或 GPU 推理验收。

仍需使用真实权重验证 MinerU/PaddleOCR 推理、CUDA 镜像与厂商 wheel、MPS 内存行为、RustFS 服务、MCP 客户端长连接和生产回调。按本次约束，这些没有通过真实下载或生产部署执行；代码与离线升级命令已准备完成。
