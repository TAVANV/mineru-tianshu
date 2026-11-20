# MinerU 4.2.3 离线完整重新部署

从 3.4.5 升级时，在联网机用全新检出构建离线包，在服务器用新目录独立部署。旧目录保留升级前的 SQLite、上传文件和结果文件，供回滚使用。

## 1. 核对运行环境

- 目标机使用 Linux x86_64、NVIDIA GPU 和 NVIDIA Container Toolkit。离线后端使用 PyTorch 2.10.0 cu126。随包提供的 vLLM `v0.31.0-cu129` 使用 CUDA 12.9 Update 1，原生驱动至少为 **575.57.08**。在目标机运行 `nvidia-smi --query-gpu=name,driver_version --format=csv,noheader` 核对驱动。[NVIDIA CUDA 12.9 Update 1 发布说明](https://docs.nvidia.com/cuda/archive/12.9.1/cuda-toolkit-release-notes/index.html)
- 指定的 vLLM 镜像携带了依赖 CUDA 13 的 torchcodec。离线编排启动 vLLM 时会移除容器内的可选音频解码包，供 MinerU Pro 的图像推理正常启动。[vLLM 上游问题 #60406](https://github.com/vllm-project/vllm/issues/60406)
- 记录旧部署的 `RUSTFS_VERSION`。构建与部署使用相同的 RustFS 镜像标签。确认 `VLLM_MINERU_DEVICE` 所指 GPU 的显存足以容纳 Pro 模型与 Worker 的小模型。

## 2. 在联网机打包

在升级分支的全新检出目录执行以下命令。联网机需要 Docker Buildx、Python 3 和 venv。把路径换成已经备份到联网机的模型目录，可重复指定 `--reuse-from` 来复用旧的模型目录和缓存目录。

```bash
bash setup.sh --mode offline-build --network global \
  --reuse-from /backup/tianshu-3.4.5/models-offline \
  --reuse-from /backup/tianshu-3.4.5/models
```

没有可复用的目录时省略 `--reuse-from`。使用国内镜像时指定 `--network cn`。旧部署的 RustFS 标签若有自定义，构建时显式传入同一个 `RUSTFS_VERSION`。

脚本准备 4.x Torch 小模型、固定提交的 MinerU Pro 权重和音频缓存。已有的 Pro 权重会按固定提交校验，内容一致的权重可复用。离线包包含后端、前端、RustFS、vLLM、Redis 镜像、权重、部署配置和 `SHA256SUMS`，输出在 `docker-images/`。完整传输该目录的内容到服务器的新目录，例如 `/opt/tianshu-4.2.3`。

## 3. 保留旧部署并切换

以下命令在目标服务器执行。示例假定旧目录为 `/opt/tianshu-3.4.5`，旧离线 Compose 使用 `tianshu-backend`、`tianshu-frontend` 和 `tianshu-rustfs` 容器名。先停止接收任务，等待旧任务及其 PDF、ZIP 子任务处理完。

```bash
old_dir=/opt/tianshu-3.4.5
new_dir=/opt/tianshu-4.2.3

docker tag "$(docker inspect --format '{{.Image}}' tianshu-backend)" tianshu-backend:3.4.5-backup
docker tag "$(docker inspect --format '{{.Image}}' tianshu-frontend)" tianshu-frontend:3.4.5-backup

docker inspect --format '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Name}}{{end}}{{end}}' tianshu-rustfs
```

最后一条命令通常返回 `tianshu-rustfs-data`。这份命名卷中的对象对应历史结果里的图片链接。若旧部署使用了其他命名卷，在新目录的 `.env` 设置 `TIANSHU_RUSTFS_VOLUME` 为实际卷名，并用 `docker volume inspect` 确认它存在。保留命名卷有助于升级和回滚复用现有对象。

```bash
cd "$old_dir"
docker compose --profile manual --profile redis down

cd "$new_dir"
mkdir -p data
rsync -a "$old_dir/data/" data/
cp -p "$old_dir/.env" .env
sha256sum -c SHA256SUMS
bash setup.sh --mode offline-deploy --yes
```

复制后的 `.env` 沿用旧 `JWT_SECRET_KEY`、`RUSTFS_*` 和 `DATABASE_PATH` 值。自定义挂载的 SQLite 或输出目录也要复制到新部署的对应位置，确保旧实例与新实例使用独立的目录。升级与回滚都使用 `docker compose down`，以保留 RustFS 和 Redis 命名卷。

部署脚本会先校验离线包，再加载镜像并检查 GPU。脚本会创建 vLLM 容器，Worker 会在收到 vLLM 或 hybrid 解析任务时启动容器。

## 4. 验收

```bash
cd /opt/tianshu-4.2.3
docker compose --profile manual ps -a
bash setup.sh --mode offline-deploy --smoke-only
```

- 验证旧账号或 API Key、历史任务及其 RustFS 图片可访问。
- 确认 PDF 自检成功，然后分别提交带图的 PDF、带页范围的 PDF 和一个需要 vLLM 的样例。检查 Markdown、图片链接和源 PDF 定位。执行 vLLM 样例前，`tianshu-vllm-mineru` 应处于已创建、待启动状态。
- 使用音频或去水印的部署，打开流量前用对应样例验收。首次启动的小模型与 vLLM 加载时间计入任务处理时限。

MinerU 4.2.3 的公开 SDK 支持 `basic`、`standard`、`advanced` 档位，分别承接原 `pipeline`、`vlm-*`、`hybrid-*`。API 的页范围仍从 0 开始，指定页范围的 PDF 直接以源文件为基准解析。预览显示源 PDF，图片使用转换得到的 PDF。SDK 没有语言、公式或表格开关及布局、文本框 PDF 导出参数。

## 5. 回滚到 3.4.5

先停止接收 4.2.3 的新任务。保留新目录以便后续取回升级后新产生的任务和产物。

```bash
cd /opt/tianshu-4.2.3
docker compose --profile manual --profile redis down

docker tag tianshu-backend:3.4.5-backup tianshu-backend:latest
docker tag tianshu-frontend:3.4.5-backup tianshu-frontend:latest

cd /opt/tianshu-3.4.5
docker compose up -d --pull never
docker compose ps
```

旧部署若使用其他后端或前端镜像标签，恢复到旧 Compose 指定的标签。启动后验收旧实例读取的是旧目录的 SQLite 和结果，以及原 RustFS 命名卷。
