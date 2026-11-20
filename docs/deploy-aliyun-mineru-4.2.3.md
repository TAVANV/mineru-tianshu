# 阿里云镜像升级到 MinerU 4.2.3

本文用于在目标服务器的 `/root/tianshu` 原位升级。本次后端镜像标签为 `5`，前端为 `4`，后端内安装的 MinerU SDK 版本为 `4.2.3`。本文提供配置与执行步骤；是否已上线，以 Actions 产物、实际运行镜像和端到端验收记录为准。

软件版本与模型发布名分别管理：新版 SDK 是 `4.2.3`，小模型目录为 `MinerU-4_models_torch`，VLM 使用 `MinerU2.5-Pro-2605-1.2B`。VLM 名称中的 `2.5` 不代表仍在运行旧 SDK；旧 `2509` 权重保留用于兼容与回滚，新配置默认选择 Pro。

## 1. 发布与部署组合

发布前先把经过检查的修改推送到 `deploy/mineru-4.2.3`。现有 main 分支未包含本次修改，应在 Actions 中选择该升级分支，本次后端发布 `5`，前端代码没有变化，继续使用已发布的 `4`；后续构建填写未占用的整数或留空自动分配。也可在有仓库权限的联网终端执行：

```bash
gh workflow run aliyun-backend.yml --ref deploy/mineru-4.2.3
# 只有前端变更时才需要再次发布；留空自动分配新整数。
gh workflow run aliyun-frontend.yml --ref deploy/mineru-4.2.3
```

确认本次使用的每个镜像构建成功，源码 SHA 对应各自的发布提交后再切换。版本分配器只接受大于等于 3 的整数，失败也占用版本号；若所填版本已被占用，按发布流程使用后续整数，并同步修改现有 `.env` 中的镜像地址。不要把 SDK 版本 `4.2.3` 填入 Actions 的版本输入，也不要覆盖既有标签。

部署固定使用以下四份文件，顺序不可交换，也不要叠加 `docker-compose.build.yml` 或旧的兼容补丁文件：

```bash
cd /root/tianshu
dc() {
  docker compose --project-name tianshu --project-directory /root/tianshu \
    --env-file /root/tianshu/.env \
    -f /root/tianshu/docker-compose.yml \
    -f /root/tianshu/docker-compose.external-models.yml \
    -f /root/tianshu/docker-compose.vllm.yml \
    -f /root/tianshu/docker-compose.registry.yml "$@"
}
```

后文的 `dc` 均指此函数。原 `.env` 加入下表的 `COMPOSE_FILE` 后，在 `/root/tianshu` 直接运行 `docker compose up -d` 也会按同一顺序加载四份编排，保持原有使用方式。服务器 Docker Compose `2.40.3` 支持叠加文件的 `!override` 与 `!reset`；前者避免新旧端口累计，后者移除原 RustFS 卷的 `driver` 字段以使用现有外部卷。`init-models`、API、Worker、Scheduler、MCP 使用同一后端镜像；运行应用源码来自镜像。vLLM 单独挂载 `scripts/vllm-model-entrypoint.py`，用于动态选择外置模型。

叠加文件保留 API、Worker、MCP、前端、RustFS、Scheduler 与 vLLM 的原有健康检查及启动宽限时间，维持现有可观测性。

## 2. 保留数据与设置变量

始终使用原有的单一 `.env`。`.env.registry.example` 仅作为非敏感变量的合并模板，完成第 4 节备份后，再由部署脚本或编辑器按键更新原 `.env`；禁止用 `cp` 覆盖原文件，也不创建活动 `.env.registry`。原 `.env` 的 JWT、管理员密码、RustFS 访问密钥、MCP 密钥、内部 API 凭据、`DATABASE_PATH`、网络配置和自定义地址沿用；不要从示例文件重建原 `.env`，也不要把现有密钥复制进文档或提交到 Git。

模板中的 `SERVER_HOST=your-server.example.com` 是占位符，URL 和白名单通过 `${SERVER_HOST}` 插值。真实地址仅写入服务器本地未跟踪的 `.env`，或在执行验收命令的本机 shell 设置；不要提交到仓库。模板网段使用上游通用示例，已有部署仍沿用实际原网络，不用示例覆盖。

| 配置 | 本次值或处理方式 |
| --- | --- |
| `COMPOSE_FILE` | `docker-compose.yml:docker-compose.external-models.yml:docker-compose.vllm.yml:docker-compose.registry.yml`，Linux 服务器使用冒号分隔 |
| `SERVER_HOST` | `your-server.example.com` 占位符，仅在本地配置真实地址 |
| `TIANSHU_BACKEND_IMAGE` / `TIANSHU_FRONTEND_IMAGE` | `registry.cn-chengdu.aliyuncs.com/tavan-ai/tianshu-backend:5` / `tianshu-frontend:4` |
| `VLLM_IMAGE` | `vllm/vllm-openai:v0.31.0-cu129`，可由变量覆盖 |
| `RUSTFS_IMAGE` | 保留现场 `rustfs/rustfs@sha256:0725587f6fcca83c1898f321424327d6e6da5e01ea20382905dd258ed5af3be4` |
| `OFFLINE_MODELS_PATH` | `/root/tianshu/models-offline-4.2.3` |
| `MINERU_VLM_MODEL_DIR` | 默认留空，由模型 `config.yaml` 选择 Pro；也可显式指定挂载根内的目录 |
| `TIANSHU_NETWORK_NAME` / `TIANSHU_SUBNET` / `TIANSHU_GATEWAY` | 沿用原网络；模板仅示例 `tianshu-network` / `172.28.0.0/16` / `172.28.0.1` |
| `API_PORT` / `FRONTEND_PORT` | `8100` / `8080` |
| `NGINX_CLIENT_MAX_BODY_SIZE` | `0`，沿用当前 Nginx 已生效的上传大小设置 |
| `ALLOWED_ORIGINS` | 显式传入后端，沿用原 CORS 白名单；模板加入 `http://${SERVER_HOST}:8080`，overlay 缺省仅允许本地调试地址 |
| `WORKER_PORT` / `MCP_PORT` / `VLLM_PORT` | `8001` / `8002` / `30024`，保留原全接口绑定 |
| `MCP_ALLOWED_HOSTS` | 逗号分隔，模板加入 `${SERVER_HOST}:8002`；overlay 缺省仅含本机回环地址 |
| `MCP_ALLOWED_ORIGINS` | 逗号分隔，模板加入 `http://${SERVER_HOST}:8080`、MCP 自身地址及回环来源；overlay 缺省仅含回环来源 |
| `RUSTFS_PORT` / `RUSTFS_CONSOLE_PORT` | `9000` / `9001`，保留原全接口绑定 |
| `RUSTFS_PUBLIC_URL` | 必须在本地 `.env` 显式提供，沿用历史图片公开地址；模板为 `http://${SERVER_HOST}:9000` |
| `RUSTFS_SECURE` / `REDIS_DB` | `false` / `0`，显式传入 API 与 Worker |
| `MAX_CONCURRENT_TASKS` / `MINERU_VIRTUAL_VRAM_SIZE` | `4` 个 Worker 进程 / 每进程规划值 `6` GiB |
| `MINERU_PROCESSING_WINDOW_SIZE` | `8`，降低每进程同时物化的页面图像数量 |
| `VLLM_GPU_MEMORY_UTILIZATION` / `VLLM_MAX_NUM_SEQS` | `0.2` / `32` |
| `WORKER_MEMORY_LIMIT` / `WORKER_MEMORY_SWAP_LIMIT` / `WORKER_MEMORY_RESERVATION` | `32G` 内存上限 / `40G` 内存与 swap 总量 / `8G` 软保留值 |
| `PDF_SPLIT_ENABLED` / `PDF_SPLIT_THRESHOLD_PAGES` / `PDF_SPLIT_CHUNK_SIZE` | `true` / 超过 `40` 页拆分 / 每片 `20` 页 |
| `SCHEDULER_MONITOR_INTERVAL` / `SCHEDULER_STALE_TASK_TIMEOUT` | `60` 秒 / `60` 分钟 |
| `LITSERVE_URL` | `http://worker:8001/predict`，保留 Scheduler 等待 Worker 就绪的行为 |
| `SCHEDULER_CLEANUP_OLD_FILES_DAYS` | `7` 天；当前实现同时删除过期 completed/failed 任务记录及相关文件，设为 `0` 关闭 |
| `FAIR_SCHEDULING_ENABLED` / `REDIS_QUEUE_ENABLED` | `true` / `false`，SQLite 任务队列 |

48 GiB 显卡上的 `6` GiB 是 MinerU 的规划参数，不能强制隔离各进程显存；小模型 PDF 批量行为也不能仅用此值约束。vLLM 的 `0.2`、处理窗口和序列上限共同降低峰值，最终容量仍需以四进程混合任务实测为准。`40G` 的内存与 swap 总量不会自动为宿主机创建 swap。

公平调度先按任务优先级排序，再在同优先级中优先选择活动任务少的用户，最后按提交时间排序。它不提供等待时间老化或严格防饥饿保证，也不等于每个用户固定配额；持续高优先级流量仍可能延后低优先级任务。

MCP 的 Host/Origin 白名单显式传入容器，避免远程客户端访问服务器地址时因默认仅允许监听地址及回环而返回 HTTP 421。若原 `.env` 已配置实际域名或来源白名单，合并时沿用原值；不要用示例默认值覆盖已有集成所需的域名。

## 3. 准备模型与升级文件

将同一升级提交的四份 Compose、`.env.registry.example` 和 `scripts/vllm-model-entrypoint.py` 暂存到 `/root/tianshu-release-4.2.3`，保持相对目录结构。先不要覆盖正在使用的旧编排。新模型目录需要提前准备完成，至少包含：

- `config.yaml`：`model.source: local`、`model.small_backend: torch`、`model.base_dir` 位于容器 `/app/models` 内，`tianshu.vllm_model_dir` 指向已校验的 VLM 权重。
- `MinerU-4_models_torch/` 与配置选中的 Pro VLM 权重，以及实际使用的 SenseVoice、说话人、去水印等模型。
- `huggingface_cache/`、`modelscope_cache/`、`.locks/`、`torch_cache/`、`ultralytics_cfg/`，供子挂载读写。

保留旧的 `models/`、`models-offline/` 及其缓存和模型；新模型目录应使用独立的普通文件副本，避免修改硬链接影响旧权重。模型准备可参考 [完整离线升级文档](upgrade-4.2.3-offline.md)。部署时 `init-models` 仅校验外置权重，不负责下载。

新配置固定 `MINERU_CONFIG=/app/models/config.yaml`，并启用 `HF_HUB_OFFLINE=1`、`TRANSFORMERS_OFFLINE=1`、`MODELSCOPE_OFFLINE=1`；同时设置 `YOLO_OFFLINE=true`、`YOLO_AUTOINSTALL=false`，关闭 YOLO 的在线探测和自动安装依赖。整个模型根只读，上述五类缓存与锁目录按目标路径覆盖为可写；HF 与 ModelScope 自身的锁也位于各自可写缓存目录中。

升级后的应用镜像包含正式媒体依赖和本地音频加载支持。旧的 `PYTHONPATH`、`runtime-packages`、`video_engine.py`、`/app/backend/iic` 兼容挂载和 `MINERU_TOOLS_CONFIG_JSON` 不再加入新编排；完成备份后可从 `.env` 清理旧音频配置和仅供运行时补包使用的 `PYTHONPATH` 等无效变量。旧文件先保留，供旧实例回滚使用。Worker 的 `WORKER_GPUS`、`MAX_BATCH_SIZE` 和前端的无效运行时 `VITE_API_BASE_URL` 使用普通 `null` 显式取消继承值，按 Docker 的 unset 语义不传入容器。Scheduler 显式使用 `/app/data/output` 和 `/app/data/uploads`，与原目录挂载一致。

## 4. 记录与备份

以下为服务器上的 Bash 命令。先停止新任务提交并等待父任务、PDF/ZIP 子任务完成。确认备份位置可容纳 SQLite、上传与结果文件以及 RustFS 对象的完整副本；`docker inspect` 和解析后的 Compose 可能含密钥，只保存在权限受限的本地备份目录。

```bash
set -euo pipefail
umask 077
cd /root/tianshu
release_dir=/root/tianshu-release-4.2.3
backup_dir="/root/tianshu-backup-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$backup_dir/config"
df -h /root/tianshu
docker compose version
nvidia-smi
docker volume inspect tianshu-rustfs-data tianshu-rustfs-logs >/dev/null

cp -a .env "$backup_dir/config/"
find . -maxdepth 1 -type f -name 'docker-compose*.yml' \
  -exec cp -a -- {} "$backup_dir/config/" \;
if [ -f scripts/vllm-model-entrypoint.py ]; then
  cp -a scripts/vllm-model-entrypoint.py "$backup_dir/config/"
fi
docker inspect tianshu-backend tianshu-worker tianshu-scheduler \
  tianshu-mcp tianshu-frontend tianshu-rustfs tianshu-vllm-mineru \
  > "$backup_dir/containers.previous.json"
docker tag "$(docker inspect --format '{{.Image}}' tianshu-backend)" \
  tianshu-backend:rollback-before-4
docker tag "$(docker inspect --format '{{.Image}}' tianshu-frontend)" \
  tianshu-frontend:rollback-before-4

old_compose_files=$(docker inspect --format \
  '{{index .Config.Labels "com.docker.compose.project.config_files"}}' tianshu-backend)
test -n "$old_compose_files"
IFS=, read -r -a old_files <<< "$old_compose_files"
old_dc=(docker compose --project-name tianshu --project-directory /root/tianshu \
  --env-file /root/tianshu/.env)
for compose_file in "${old_files[@]}"; do old_dc+=(-f "$compose_file"); done
"${old_dc[@]}" --profile manual --profile redis config > "$backup_dir/compose.previous.yml"
```

检查记录中的旧编排是否覆盖实际部署使用的全部文件。若旧部署还使用了额外 `--env-file` 或 shell 环境覆盖项，重建 `old_dc` 时带上它们，再保存配置。保留旧 Compose 引用的配置、脚本、源码和兼容包文件。

## 5. 预检、切换与启动

在同一个 Bash 会话中继续执行，确保上一节的 `release_dir`、`backup_dir`、`old_dc` 仍存在。先安装新编排并拉取镜像，此时旧容器继续运行：

```bash
cp -a "$release_dir/docker-compose.yml" \
  "$release_dir/docker-compose.external-models.yml" \
  "$release_dir/docker-compose.vllm.yml" \
  "$release_dir/docker-compose.registry.yml" /root/tianshu/
mkdir -p scripts
cp -a "$release_dir/scripts/vllm-model-entrypoint.py" scripts/
cp -a "$release_dir/.env.registry.example" /root/tianshu/.env.registry.example
```

先按 `.env.registry.example` 将本次非敏感设置逐项合并到现有 `.env`，特别核对 `COMPOSE_FILE`、镜像、端口、模型路径和公开 URL。保留原密钥、数据库路径、原网络及已配置的 CORS/MCP 白名单；原 `.env` 不做整文件替换。未启用的 Redis 服务仍参与基础文件插值，若旧 `REDIS_PASSWORD` 为空，部署合并步骤仅为满足必填校验生成随机值，并保持 `REDIS_QUEUE_ENABLED=false`，不启动 Redis。重新定义第 1 节的 `dc` 函数，再执行：

```bash
dc config --quiet
docker compose config --quiet
docker compose config --format json > "$backup_dir/compose.default.json"
dc config --format json > "$backup_dir/compose.explicit.json"
cmp -s "$backup_dir/compose.default.json" "$backup_dir/compose.explicit.json"
dc --profile manual config --images
# 私有仓库按现有凭据登录，交互输入密码，避免出现在命令历史中。
docker login registry.cn-chengdu.aliyuncs.com
dc --profile manual pull

test -s /root/tianshu/models-offline-4.2.3/config.yaml
mkdir -p /root/tianshu/models-offline-4.2.3/{huggingface_cache,modelscope_cache,.locks,torch_cache,ultralytics_cfg}
dc run --rm --no-deps init-models
dc run --rm --no-deps --entrypoint python backend -c \
  'from importlib.metadata import version; assert version("mineru") == "4.2.3"; print("MinerU", version("mineru"))'
```

默认 `docker compose config` 必须与显式四文件 `dc config` 相同；`cmp` 成功后再继续，确保后续直接执行 `docker compose up -d` 使用本次完整配置。`config --images` 应显示阿里云后端 `:5`、前端 `:4`、固定 RustFS 摘要和 cu129 vLLM，不应包含旧 GHCR 镜像。完整 `config` 会含密钥，上述两个 JSON 文件仅保存在受限备份目录：不得出现应用源码挂载、运行时补包挂载或旧音频兼容挂载；应保留 Docker socket、MCP 配置和 vLLM 模型选择脚本挂载。确认 RustFS 原命名卷存在，`9000` 仍对外开放。

预检成功后，停止旧容器，使 SQLite、结果与 RustFS 备份相互一致。下面的备份使用已拉取的后端镜像执行 `tar`，不会启动应用或加载 GPU：

```bash
docker compose --project-name tianshu --project-directory /root/tianshu \
  -f "$backup_dir/compose.previous.yml" --profile manual --profile redis stop
tar -C /root/tianshu -czf "$backup_dir/data-logs.tgz" data logs
backend_image=$(dc config --format json | python3 -c \
  'import json,sys; print(json.load(sys.stdin)["services"]["backend"]["image"])')
for volume in tianshu-rustfs-data tianshu-rustfs-logs; do
  docker run --rm --network none --user 0:0 --entrypoint tar \
    -v "$volume:/volume:ro" -v "$backup_dir:/backup:rw" "$backend_image" \
    -C /volume -czf "/backup/$volume.tgz" .
done
sha256sum "$backup_dir/"*.tgz > "$backup_dir/SHA256SUMS"
sha256sum -c "$backup_dir/SHA256SUMS"

dc --profile manual create --no-build --pull never vllm-mineru
dc up -d --no-build --pull never
dc --profile manual ps -a
```

`vllm-mineru` 保持 `manual` profile 和 `restart: no`，预创建后等待 Worker 在 VLM/hybrid 任务到来时冷启动。启动时先卸载 cu129 镜像中依赖 CUDA 13 的可选音频解码包 `torchcodec`，再运行 Python 模型选择入口；不会把模型路径硬编码为启动命令。不要对该服务执行常驻启动或改回 `0.6` 的显存利用率。

数据仍来自原 `data/db`、`data/uploads`、`data/output`。RustFS 使用 `external: true` 的 `tianshu-rustfs-data`、`tianshu-rustfs-logs`，卷缺失时直接报错，避免静默创建空卷。升级步骤不删除原模型、SQLite、上传文件、结果、密钥或原对象卷。

## 6. 验收

先在当前本机 shell 设置 `SERVER_HOST` 为实际服务器地址；以下命令仅演示占位值，执行前替换，真实值不写入受版本控制的文件。

```bash
SERVER_HOST=your-server.example.com
dc --profile manual ps -a
curl --fail "http://${SERVER_HOST}:8100/api/v1/health"
curl --fail "http://${SERVER_HOST}:8002/health"
curl --fail --head "http://${SERVER_HOST}:8080/"
dc exec -T backend python -c \
  'import os,sqlite3; p=os.environ["DATABASE_PATH"]; c=sqlite3.connect("file:"+p+"?mode=ro",uri=True); print(c.execute("PRAGMA quick_check").fetchone()[0])'
docker inspect --format '{{.Config.Image}} {{.Image}}' \
  tianshu-backend tianshu-worker tianshu-scheduler tianshu-mcp tianshu-frontend tianshu-vllm-mineru
dc logs --tail 100 backend worker scheduler mcp-server
```

按以下清单完成真实推理后再恢复全部流量：

1. 旧账号可登录，旧 API Key 与 MCP 远程客户端继续工作；历史任务、下载、源 PDF 预览和图片均可访问，历史图片仍使用原 `.env` 中的公开地址。
2. 单个小 PDF、图片与带页范围 PDF 解析成功；超过 40 页的 PDF 产生 20 页分片并合并，父子任务计数正确；ZIP 批量流程可用。
3. 提交 VLM/hybrid PDF，确认 vLLM 从待启动状态进入运行并加载选中的 Pro 权重，再核对 Markdown、JSON 与图片。仅健康检查成功不能证明模型已加载。
4. 实际运行音频、视频与所使用的去水印样例，确认本地模型与新镜像中的依赖可用，无联网下载或旧补丁挂载依赖。
5. 四进程并发混合 PDF、VLM、音视频负载，观察 `nvidia-smi`、`docker stats --no-stream tianshu-worker tianshu-vllm-mineru` 与失败任务。确认无 CUDA OOM、内存终止或超时重置；GPU 规划参数不能替代这一步。
6. 核对 Scheduler 的 60 秒监控、60 分钟超时与 7 天清理日志；检查离线缓存写入权限。需要长期保留历史任务时先将清理天数改为 `0`，再重新创建 Scheduler。

MinerU 4.2.3 SDK 使用 `basic`、`standard`、`advanced` 档位承接旧 backend 路由。API 的页范围仍从 0 开始。SDK 忽略旧选项 `lang`、`formula_enable`、`table_enable`、`draw_layout_bbox`、`draw_span_bbox`、`draw_layout`、`draw_span`、`dump_model_output`；这些选项留在旧任务参数中不表示新 SDK 仍执行对应开关或布局导出。

## 7. 回滚

停止新任务提交，保留升级后的任务与结果供后续取回。以下命令使用前面同一备份目录；开启新终端后先把 `backup_dir` 设为实际备份绝对路径，并重新定义 `dc`：

```bash
cd /root/tianshu
test -s "$backup_dir/compose.previous.yml"
sha256sum -c "$backup_dir/SHA256SUMS"
dc --profile manual --profile redis stop
# 保留新数据库和产物，恢复停止旧容器后保存的完整 data 副本。
mv /root/tianshu/data "$backup_dir/data-after-4.2.3"
tar -C /root/tianshu -xzf "$backup_dir/data-logs.tgz" data
cp -a "$backup_dir/config/"docker-compose*.yml /root/tianshu/
cp -a "$backup_dir/config/.env" /root/tianshu/.env
if [ -f "$backup_dir/config/vllm-model-entrypoint.py" ]; then
  cp -a "$backup_dir/config/vllm-model-entrypoint.py" /root/tianshu/scripts/
fi
docker compose --project-name tianshu --project-directory /root/tianshu \
  -f "$backup_dir/compose.previous.yml" --profile manual \
  create --no-build --pull never vllm-mineru
docker compose --project-name tianshu --project-directory /root/tianshu \
  -f "$backup_dir/compose.previous.yml" up -d --no-build --pull never
```

回滚编排从受限备份中恢复原 `.env`、配置与脚本，使用升级前的数据与原 RustFS 卷。原模型、兼容包和镜像 `:3` 保留到新版本验收结束，禁止在此阶段执行镜像清理或带卷删除。通常保留原 RustFS 卷及新增对象即可，不直接覆盖对象卷；对象卷损坏时使用经过校验的独立快照另行恢复。回滚完成后再次验收旧账号、API Key、历史结果和图片，并核对恢复后的默认 `docker compose config` 指向原部署。
