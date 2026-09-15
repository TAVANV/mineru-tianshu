# 第一批上游改进移植

## 范围

基于当前主线定制，逐项移植；没有整体 cherry-pick，也没有升级 MinerU、替换 Office 引擎、移除 PaddleOCR 或引入第二批功能。

| 项目 | 实施与兼容处理 |
| --- | --- |
| API 非阻塞 | 任务查询、图片交接、文件路径检查等同步工作由 FastAPI 线程池执行；上传采用 AnyIO 分块写入，入库与失败清理由线程池执行；JWT/API Key 数据库查询改为同步依赖。参数与响应不变。 |
| SQLite WAL | 任务、认证、系统配置连接统一 WAL 和 30 秒锁等待；保留 FULL 同步级别，未采用上游 NORMAL，以保留提交持久性。 |
| PDF 批量子任务 | 单事务插入并累加完整子任务计数，提交后入 Redis；保留按大小拆分、旧子任务清理、先初始化父任务以及所有图片参数继承；不再二次初始化父任务。 |
| Redis 对账 | 调度器逐轮按 SQLite pending 状态补回遗漏任务；读取失败跳过，写入失败不计为成功；NX 入队保护既有排序，清理 processing 时与 SQLite 认领串行，批量入队在事务结束后执行。重试/恢复后重新入队，保留现有心跳、PID 检测、重试上限和父任务回收。启动时连接失败保留客户端，允许 Redis 后续恢复。 |
| EXIF 容错 | img2pdf 使用 Rotation.ifvalid，保持有效方向标记的处理。现有要求 img2pdf>=0.6.0 不变；本次使用 0.6.3 验证无效 EXIF。 |
| 图片预览 | 使用现有 source_url 显示原图；无源预览时展示结果，避免空白预览栏。 |
| 文档与代理 | 规范地址 /api/v1/openapi.json；保留 /openapi.json、/api/openapi.json 和旧代理 /v1/* 兼容。Nginx 保留 /api 前缀，Scalar 服务器基址不再重复附加 /api。 |
| 构建缓存 | 仅移除 Compose 自引用 cache_from；保留镜像、部署及离线模型挂载。Redis 内存策略改为 noeviction，避免队列被当作缓存淘汰。 |

## WAL 部署和备份

仓库配置把数据库目录绑定到宿主机 `./data/db`，本次开发工作区在本地磁盘 `/dev/disk3s5`；尚未连接生产宿主机核实实际存储。部署时须确保数据库及其 `-wal`、`-shm` 文件位于同一宿主机的本地可写目录，不能放在 NFS/CIFS。该限制来自 [SQLite WAL 文档](https://sqlite.org/wal.html)。

`make backup-db` 已使用 SQLite 在线备份接口，并通过 Compose 服务名复制快照，兼容不同容器名称。也可直接执行：

```bash
python backend/backup_db.py backups/snapshot.db --source data/db/mineru_tianshu.db
```

备份包含已提交到 WAL 的内容，先生成临时快照再替换目标。不要在线仅复制 `.db` 文件。恢复时停止 API、调度器与 Worker，再替换数据库；不要把快照和旧实例的 WAL/SHM 混用。

## 验证

自动化测试位于 `tests/test_phase1.py`，本次 14 项全部通过（FastAPI 0.115.6）。使用安装了后端 API 依赖、pytest、img2pdf、fakeredis 的环境运行：

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/test_phase1.py -q
```

覆盖上传时事件循环仍可推进、RustFS None/True/False、图片交接与权限、源文件下载、批量插入回滚及提交后可见性、按大小拆分和旧子任务清理、快速完成子任务时的计数、WAL 并发读取和在线备份、Redis 全量丢失/读写故障/首次连接失败恢复、FIFO 保留、无 Redis 包回退，以及超时/心跳/重试上限。

已执行前端 `npm run build`、三套生产/CPU/离线 Compose 的 `config --quiet` 和 Python 静态检查。Compose 校验提示本机未配置 JWT_SECRET_KEY、RUSTFS_PUBLIC_URL，未启动部署服务。

前端已有锁文件与 package.json 不一致，`npm ci` 失败；本次使用 `npm install --no-package-lock --no-audit --no-fund` 后构建通过，与现有镜像的 npm install 流程一致。未更新依赖声明或锁文件。构建仍有第三方注释、PDF.js eval 和大包告警。

## 仍需部署环境验收

自动化测试使用临时数据库、模拟 Redis 和隔离的 Worker 拆分方法，未启动 GPU Worker、真实 RustFS 或真实 Redis 服务，也未完成浏览器交互验收。上线前仍需用真实样本验证：

- RustFS 环境默认开启/关闭与任务三态开关组合，图片交接端到端。
- Strict XLSX、带图 Office 和完全断网的现有引擎解析。
- 大 PDF 的实际拆分、解析、合并与图片路径。
- 杀死真实 Worker 后恢复，心跳续租和重试封顶。
- 现有 API/MCP 客户端以及新旧前端代理路径。

## 上游参考

- [API 性能 1c91833](https://github.com/magicyuan876/mineru-tianshu/commit/1c91833)
- [数据库与批量子任务 e729b9e](https://github.com/magicyuan876/mineru-tianshu/commit/e729b9e)
- [队列对账 c13aab1](https://github.com/magicyuan876/mineru-tianshu/commit/c13aab1)
- [文档路径 f0c7e55](https://github.com/magicyuan876/mineru-tianshu/commit/f0c7e55)：仅提取文档修复，没有带入 api_key_id/Webhook 依赖。
