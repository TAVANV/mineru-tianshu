# 第三批：认证、文件、MCP 防护与 vLLM 运行时

## 分支与来源

在 `feat/upstream-integration-20260915` 上继续，保留 `b319ec4`、`aef598c`。`main` 保持在 `1438b59`。2026-09-15 拉取核对，上游最新为 `648e5d6`（9 月 14 日）。本批按函数/代码块移植，未整体 cherry-pick。

| 来源 | 移植与适配 |
| --- | --- |
| [523e480](https://github.com/magicyuan876/mineru-tianshu/commit/523e480) | JWT jti 吊销表、token_epoch 改密失效、API Key scope/90 天默认期限、文件灵活认证和安全响应、上传文件名白名单、MCP 入站密钥与后端凭据。保留本地 WAL/FULL、ADMIN_PASSWORD 初始化和审计。 |
| [3bfd08e](https://github.com/magicyuan876/mineru-tianshu/commit/3bfd08e) | 吸收公开信息收敛原则：MCP 健康响应最小化，API 异常不回显内部异常；引擎信息改为登录后可读，API 健康接口不返回队列统计。 |
| [e740406](https://github.com/magicyuan876/mineru-tianshu/commit/e740406) | 四种本地 vLLM 后端冷启动、外部 server_url 不操作容器、缺失容器回退外部托管、地址解析集中到 engine。保留 PaddleOCR/MinerU 互斥。 |

MCP 模块以拉取到的上游版本为基础移植；重新加入 PaddleOCR 后端选项。MinerU/Office/模型升级、Apple Silicon、Pipeline-only、API Key 级 Webhook 和整套界面重构不在本批。

## 认证与 API Key

- 新 JWT 带随机 jti；注销只吊销该 JWT，并持久化到 SQLite。无 jti 的历史 JWT 注销时递增用户 token_epoch，使该用户旧会话全部失效。
- 修改密码与递增 token_epoch 在同一事务。登录使用**校验密码时读取的代次**，避免改密与登录并发时旧密码换取新代次 JWT。
- JWT 默认有效期由 24 小时调整为上游的 60 分钟，`JWT_EXPIRE_MINUTES` 可覆盖。旧令牌自身的 exp 不变。拒绝上游列出的已知默认密钥。
- 新 API Key 默认 90 天，最长 3650 天，不再接受 `expires_days: null`；既有无限期 Key 不强制失效。修改密码不吊销 API Key，需另行删除 Key。
- `scopes` 为 Permission 值数组，未指定/NULL 跟随账户权限，空数组拒绝所有受权限控制的操作。账户角色权限与 scope 取交集。前端新增“跟随账户 / 只读自己的任务与文件 / 提交和管理自己的任务”选择。
- 任务详情、图片交接、文件服务、任务列表与任务变更都检查对应权限。受限 Key 不能借创建子 Key 扩大 scope，也不能修改账户资料或密码。
- 修正上游两处边界：损坏 scope 按拒绝处理；ISO `T` 格式到期时间使用 julianday 比较，避免当天已过期 Key 仍有效。

创建只读 Key 的请求示例：

```json
{"name":"image-reader","expires_days":90,"scopes":["task:view:own"]}
```

## 文件与预览兼容

- 下载/预览支持 `Authorization: Bearer <JWT>`、`X-API-Key`，浏览器内嵌预览支持上游的 `?token=<JWT>`。查询参数仅用于文件路由，API Key 仅通过请求头传递。
- 沿用图片交接返回的 download_url/source_url；API 客户端对后续下载请求继续携带凭据即可，不把 Key 写入返回 URL。
- 所有者须有 `task:view:own`，跨用户访问须有 `task:view:all`；孤儿文件仅后者可访问。反查按规范化完整路径匹配，并选最深结果目录，兼容 ZIP → PDF → 分片，未采用上游 basename/LIKE 模糊兜底。
- 保留目录逃逸/符号链接检查。仅 PDF 和常见图片允许内联；HTML 等强制附件下载，响应带 nosniff、CSP sandbox、private/no-store、no-referrer。
- 前端只给**同源的本地文件服务**加预览 JWT，RustFS/外部链接不加凭据。Markdown 使用 DOMPurify 清理后再设置图片与链接；下载的 Markdown 原文不写入预览凭据。
- API 中间件将文件查询 JWT 移入请求内部 state 并从 query_string 去掉，避免后端访问日志记录；镜像 Nginx 的访问日志只记录 `$uri`，不记录查询串或 Referer。自建外层代理也需使用不含查询凭据的日志格式。
- 上传使用上游文件名净化与格式白名单，保留本项目 EPUB/ZIP、音视频及 Office 格式。流式执行 `MAX_FILE_SIZE` 字节上限，超限返回 413 并清理临时文件；0 仍表示不限制。白名单校验不是文件内容真实性检测，格式解析由现有引擎负责。
- 跨域只允许 `ALLOWED_ORIGINS` 中显式列出的来源；空值仅支持同源使用，`*` 不生效。

## MCP 配置

```dotenv
# 客户端 -> MCP，可逗号分隔配置多个访问密钥
MCP_API_KEYS=<随机访问密钥>
# MCP -> 后端：在天枢中给专用服务账户创建 API Key
TIANSHU_API_KEY=<后端API Key>
# 外部请求的实际 Host，包含端口（SDK 支持 :*）
MCP_ALLOWED_HOSTS=mcp.example.com:*,localhost:*,127.0.0.1:*
# 浏览器来源按需列出；不要填任意来源通配符
MCP_ALLOWED_ORIGINS=https://app.example.com
```

- 入站密钥通过 `X-API-Key` 或 Bearer 传递。未配置 MCP_API_KEYS 时，`/sse`、`/messages` 及其尾斜杠/子路径一律拒绝访问。健康端点公开。
- 后端 Key 建议使用专用普通账户，scope 包括 `task:submit`、`task:view:own`、`queue:view`；需要变更任务时再授予 `task:delete:own`。所有 MCP 客户端共享该后端服务账户，**不是按 MCP 密钥隔离的多租户服务**。
- Compose 显式注入 MCP_HOST、密钥、Host/Origin 白名单和 API_BASE_URL；保留原 `.env` 的 API_URL 作为 Compose 后端地址配置。直接启动 Python 时使用 API_BASE_URL。示例客户端端口修正为 8002。
- 上游先校验 DNS、后重新解析的实现存在重绑定窗口：本批在 aiohttp resolver 中再次校验实际连接地址，拒绝非公网/组播地址，禁止重定向。下载不携带后端服务 Key。URL 下载及 Base64 输入有大小上限。
- MCP 路由使用原生 ASGI endpoint 对象，避免 SDK 已发送响应后 Starlette 再发送一次响应。

## vLLM 行为

`vlm-auto-engine`、`hybrid-auto-engine`、`vlm-http-client`、`hybrid-http-client` 在配置本地服务且未传 server_url 时都会触发本地冷启动。engine 统一转换 auto 后端与补齐 HTTP 地址，仅移除末尾 `/v1`。

传入外部 server_url 时不操作本地 MinerU/PaddleOCR 容器。控制器先查询目标容器，确认存在后才停冲突容器；目标不存在、Docker 初始化不可达或未安装 Docker SDK 时视为外部托管。真实 start/stop/访问异常不吞掉。就绪仍由引擎健康检查负责。

## 依赖与验证

为使用上游 MCP Host/Origin 防护，同步其 Web/认证依赖：FastAPI 0.141.1、Starlette >=1.3.1、MCP 1.30.0、Pydantic >=2.11,<3，并解除旧 pydantic-core 硬锁。未升级 MinerU、PaddleOCR、Office 引擎或模型。添加 DOMPurify 时一并重新生成原先与 package.json 不一致的前端锁文件，因此锁文件差异较大。

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests -q
ruff check backend tests
python -m compileall -q backend
git diff --check
cd frontend && npm run build
```

本批新增 22 项用例，总计 **82 项通过**：真实 HTTP 登录/注销/改密、并发登录代次、API Key 受限/派生/到期、跨用户文件下载、预览查询 JWT、嵌套结果、上传清理、MCP 原生路由/Host/访问密钥、DNS 重绑定，以及四类 vLLM 冷启动和外部地址绕过。原测试夹具同步补上 own scope 与新增 Worker 常量，保留原回归断言。

前端生产构建、npm ci 锁文件 dry-run、Ruff、Python 编译、diff 检查和生产/CPU/离线/开发四套 Compose 配置检查通过。测试有上游 datetime/Starlette 弃用提示；前端有依赖 CSS、PDF.js eval、注释和大包提示。Compose 提示本机未设置 JWT_SECRET_KEY/RUSTFS_PUBLIC_URL；未启动生产服务。

仍需部署环境验收：真实 GPU 冷启动/切换与显存释放、完整后端镜像依赖组合、RustFS 和浏览器预览、MCP 客户端 SSE 长连接端到端。自动化中的 vLLM 容器与模型使用隔离替身，MCP 路由测试覆盖 HTTP 鉴权与 SDK 错误响应，不等同于真实 GPU 或客户端验收。
