# 天枢 (Tianshu) 全面代码审核报告

> 审核日期：2026-06-18
> 审核范围：后端 ~17,330 行 Python（30 模块）+ 前端 ~10,400 行 Vue/TS（47 文件）
> 审核方法：从核心调度并发、处理引擎、认证安全、前端、工程化测试五个维度并行深入；叠加 MinerU 升级影响评估与推理层解耦架构专项分析。所有结论均有 `文件:行号` 证据支撑。

本报告包含三大主题：

- **主题一：全面代码质量与架构审核**（5 维度）
- **主题二：MinerU 3.0.9 → 3.3.1 升级影响评估**
- **主题三：模型推理层解耦剥离架构分析**

文末附**任务安排建议**（整合三大主题的整改路线图）。

---

# 总览结论

**代码功能完整、工程纪律扎实、有明显的实战打磨痕迹（B+ 级代码质量），但被三块短板严重拉低生产成熟度：**

1. 几个可被直接利用的安全漏洞（公开注册可成 admin、文件接口无鉴权、前端 XSS）；
2. 完全零自动化测试且 CI 仅做 lint；
3. Redis/SQLite 双写一致性与父任务合并存在并发裂缝。

**当前状态不宜在不可信网络环境直接上线。**

代码规模与主要文件：
- 后端 top 文件：`litserve_worker.py`(1412) / `task_db.py`(1231) / `api_server.py`(1196) / `sensevoice_engine.py`(827) / `mcp_server.py`(728)
- 前端 top 文件：`TaskSubmit.vue`(688) / `TaskList.vue`(646) / `zh-CN.ts`(634) / `ApiDocsScalar.vue`(576) / `AppLayout.vue`(500)

---

# 主题一：全面代码质量与架构审核

## 1.1 核心任务调度与并发架构

审核文件：`task_db.py` / `redis_queue.py` / `litserve_worker.py` / `api_server.py` / `start_all.py` / `task_scheduler.py`

### 🔴 严重问题

**1. Redis 与 SQLite 双写不一致：`create_task` 入库与入队非原子，崩溃即丢任务**
- 文件：`task_db.py:166-186`
- `create_task` 先在事务里 INSERT（167-174 行 commit），**事务提交后**再调 `_enqueue_to_redis`（177-184 行），两步无原子性保障。
- 若进程在 INSERT 之后、enqueue 之前崩溃 → 任务在 SQLite 为 `pending`，但不在 Redis 队列。Redis 模式下 worker 只 `BZPOPMIN` Redis（`_get_next_task_redis` 命中后 return，`task_db.py:208-210`），永不回退扫 SQLite 的 pending 行 → **任务永久卡死**。
- SQLite 兜底扫描只在 `_get_next_task_redis` 返回 None 时发生，而 Redis 可用时返回的是"队列空"的 None，不代表 SQLite 没 pending。`reset_stale_tasks` 只重置 `processing`，不碰 `pending`，连超时兜底都救不了。
- 影响：Redis 模式下 API 进程崩溃/OOM 会静默丢失刚提交的任务，用户看到 `pending` 永不前进。
- 建议：(a) 增加周期性"SQLite pending 但不在 Redis 队列"对账任务(reconciler)，重新 enqueue 漏入队的 pending；或 (b) worker 在 Redis 队列空时低频回退扫描 SQLite pending 作兜底。

**2. SQLite 未启用 WAL / busy_timeout，多 worker + API 并发下写锁竞争严重**
- 文件：`task_db.py:65-77`（`_get_conn`，无任何 PRAGMA）
- 每次连接只设 `timeout=30.0`，**没有 `PRAGMA journal_mode=WAL`、`busy_timeout`、`synchronous` 调整**。默认 rollback-journal 模式下写操作持有全库排他锁，读写互斥。
- worker 每 0.5s 轮询认领（`litserve_worker.py:446` + `get_next_task` 的 `BEGIN IMMEDIATE`）会与 API 的 `list_tasks`、`get_task`、心跳（`update_heartbeat`，`task_db.py:761`）频繁争锁。
- 注：已实测确认 `BEGIN IMMEDIATE` 认领事务本身**正确**（Python autocommit 不会二次 BEGIN），认领原子性 OK，但吞吐与锁等待是真实瓶颈。
- 影响：并发 worker 数稍多（4+）即出现锁超时、任务认领抖动、API 偶发 500。
- 建议：在 `_init_db` 或每个连接执行 `PRAGMA journal_mode=WAL`（库级持久）+ `PRAGMA busy_timeout=30000` + `PRAGMA synchronous=NORMAL`。成本极低收益极大。

**3. 父任务合并 (`_merge_parent_task_results`) 存在并发重入与竞态**
- 文件：`litserve_worker.py:602-609`、`task_db.py:989-1040`
- `on_child_task_completed`（`task_db.py:1007-1014`）用 `child_completed = child_completed + 1` 自增（原子），但触发合并的判定与合并执行**分离且无锁**。
- 最后一个子任务完成的 worker 拿到 `parent_id_to_merge` 后调 `_merge_parent_task_results`（`litserve_worker.py:604-607`）。合并里有"完整性闸门"检查所有子任务是否 completed（`litserve_worker.py:1176-1183`），是好的防御。
- 但 `_merge_parent_task_results` 末尾才把父任务标 `completed`（`litserve_worker.py:1267`），整个合并（读分片、拷图、写 md/json）期间父任务仍是 `processing`。若此时 `reap_stale_parent_tasks` 因父任务超时介入（`task_db.py:831-847`，"子任务全 completed 但没合并"分支会 DELETE 所有子任务 + 把父任务重置为 pending），而合并 worker 仍在读子任务 `result_path`/图片 → **读到被删分片，合并出残缺结果或 FileNotFoundError**，且父任务被重新拆分，产生重复处理。
- 两条路径（worker 合并 vs scheduler reap）都没用"认领父任务"式的原子状态翻转互斥。
- 影响：超大 PDF 合并耗时长时与兜底回收线程相撞，导致结果残缺/重复拆分/异常。
- 建议：合并开始前用一次条件 UPDATE 原子"认领"父任务（如 `processing→merging WHERE status='processing'`，`rowcount==0` 则放弃），`reap_stale_parent_tasks` 排除 `merging` 状态。或给合并加心跳续租。

**4. 合并阶段无心跳续租，长合并会被超时误判**
- 文件：`litserve_worker.py:396-412`（心跳只续租 `self.current_task_id`），`:436`（`finally: self.current_task_id = None`）
- 心跳线程只续租 `current_task_id`（子任务 id）。进入 `_merge_parent_task_results`（`:604`）时仍在子任务 try 块内，但**父任务从不进入 `current_task_id`**，父任务 `started_at` 在拆分时刻就固定。对超大 PDF（几百分片），父任务从拆分到合并完成可能远超 `reap_stale_parent_tasks` 超时（默认 `stale_task_timeout*4`，`task_scheduler.py:172`）→ 触发问题 3 的竞态。
- 建议：合并期间对 parent_task_id 也做心跳续租。

### 🟡 中等问题

**5. Redis `recover_stale_tasks` 与 SQLite `reset_stale_tasks` 各自为政，双重恢复/状态漂移**
- 文件：`redis_queue.py:307-359` vs `task_db.py:666-759`
- Redis 有自己的超时回收（基于 `processing` hash 的 `claimed_at`，默认 5 分钟 `claim_visibility_seconds`，`redis_queue.py:49`），SQLite 也有基于 `started_at` 的回收（默认 60 分钟）。两者阈值不同、互不知晓。
- `recover_stale_tasks` 在 5 分钟后把任务重新 `zadd` 回队列，但 SQLite 里该任务仍是 `processing`。会被另一个 worker 重新 `BZPOPMIN` 取出，进入 `_get_next_task_redis`，其中 `UPDATE ... WHERE status='pending'`（`task_db.py:303-312`）因状态是 `processing` 而 `rowcount==0`，走 `redis_queue.fail(requeue=False)`（`task_db.py:316`）丢弃 → **正在正常处理（只是超 5 分钟）的长任务被静默放弃**。
- 注：`recover_stale_tasks` 在代码库里**似乎没有被任何调度循环调用**（scheduler 只调 SQLite 的 `reset_stale_tasks`/`reap_stale_parent_tasks`，`task_scheduler.py:166-172`），所以暂未引爆。但 `claim_visibility_seconds=300` 默认值 + 心跳续租仅作用于 SQLite 而非 Redis processing hash（worker 心跳走 `update_heartbeat` 改 SQLite，**不**调 `redis_queue.heartbeat`），说明 Redis 可靠投递层处于"半接线"状态，职责划分不清晰。
- 建议：明确"Redis 只做排队、SQLite 是状态唯一真相"契约，**删除或禁用** `recover_stale_tasks`，并在文档/代码写清；或让心跳同时续租两边。

**6. `get_cursor` 上下文管理器无差别 `commit`，与显式事务语义混用**
- 文件：`task_db.py:79-91`
- `get_cursor` 退出时总是 `conn.commit()`。`get_next_task`（`:217`）在其中又显式 `BEGIN IMMEDIATE`。两者叠加虽不报错（已实测），但语义混乱：只读方法（`get_task`、`get_queue_stats`、`list_tasks`）也触发无意义 commit，非 WAL 模式下读操作也可能短暂取锁，放大问题 2。
- 建议：区分只读/读写连接；只读路径用 `isolation_level=None` 或不 commit。

**7. 异常被静默吞掉的多处，掩盖数据问题**
- `litserve_worker.py:1249-1250`：合并 JSON 解析 `except Exception: pass` —— 分片 JSON 损坏时静默丢页，最终 JSON 缺页但任务仍 `completed`。
- `api_server.py:436-437`：父任务子任务列表组装 `except Exception: pass`。
- `litserve_worker.py:1275-1276`：清理子文件 `except Exception: pass`。
- `redis_queue.py:349-350`：`json.JSONDecodeError` 时只 log 不从 processing 移除 → 坏数据永久滞留 processing hash。
- 影响：错误被掩盖，可能产出"看似成功实则残缺"的结果（尤其 1249 行）。
- 建议：至少 `logger.warning` 记录上下文；合并丢页应计数并在父任务结果里标注。

**8. `os.kill(pid, 0)` 死 worker 检测有 PID 重用风险**
- 文件：`task_db.py:609-623`
- 通过解析 worker_id 尾部 PID 再 `os.kill(pid, 0)` 判活。容器内 PID 空间小、重启频繁，PID 可能被新进程复用 → 已死 worker 的 PID 恰被复用，误判为"兄弟 worker 存活"而跳过重置，任务卡死直到 60 分钟超时兜底。有 `reset_stale_tasks` 兜底，故列中等。
- 建议：worker 注册时间 + PID 双重校验，或用 worker 心跳表替代 PID 探活。

**9. `_should_split_pdf` 拆分阶段崩溃窗口**
- 文件：`litserve_worker.py:1135-1152`
- `convert_to_parent_task`（标 is_parent=1）与逐个 `create_child_task` 是多个独立事务。若创建了部分子任务后崩溃：父任务 `child_count` 是已建数，但实际还有分片没建子任务。`reap_stale_parent_tasks` 的"全 completed 则重拆"分支（`task_db.py:831`）会基于不完整子任务集判断。有 `delete_child_tasks` 重拆清理（`litserve_worker.py:1131`）缓解，但窗口仍在。
- 建议：拆分+建子任务放单事务，或加"拆分完成"标记位。

**10. 心跳间隔 60s 对短超时窗口偏粗**
- 文件：`litserve_worker.py:404`（`heartbeat_interval = 60`）vs `:381`（启动恢复 `timeout_minutes=10`）。当前安全，但若 scheduler `--stale-task-timeout` 调到接近分钟级，60s 心跳 + 5min 监控周期（`task_scheduler.py:163`）判定粒度会失准。属配置脆弱性。

### 🟢 轻微问题

**11. 超大文件/超长函数**
- `litserve_worker.py` 1412 行，`_process_task`（452-616）和 `_merge_parent_task_results`（1164-1268）均百行级巨型函数。
- `api_server.py` `get_task_status`（371-521）150 行，嵌套 4-5 层。
- `submit_task` 表单参数 ~60 个（`api_server.py:216-275`），应收敛为 Pydantic 模型。

**12. 重复代码**
- `_convert_office_to_pdf` 与 `_convert_office_to_new_format`（`litserve_worker.py:985-1069`）几乎完全重复。
- 各 `_process_*` 引擎方法的 `output_dir = Path(self.output_dir)/stem; mkdir; normalize_output` 样板重复 7+ 次。
- RustFS 开关解析逻辑在 `_process_markdown`（820-829）和 `_process_office`（883-894）重复。
- API 各端点"查任务→权限检查"三段式重复 6+ 次，应抽依赖。

**13. 资源管理**
- 连接管理本身干净（`get_cursor` 的 `finally: conn.close()`，`task_db.py:91`）——做得好。
- `redis_queue.py` 单例 `_client` 从不关闭，但 redis-py 内部有连接池，影响小。
- `VLLMController._get_client` 每次 `docker.from_env()` 新建并在 finally 关闭（`litserve_worker.py:198-202`），OK。

**14. 架构层面**
- 轮询 vs 事件驱动：SQLite 模式 0.5s 忙轮询（`litserve_worker.py:446`），Redis 模式用 `BZPOPMIN` 阻塞（事件驱动，`redis_queue.py:191`）——Redis 路径更优，但因问题 1/5 可靠性未完全兑现。
- SQLite 作队列扩展性：单机单文件锁 + 无 WAL（#2），实际并发上限很低，与 README 宣称"10K+ QPS（Redis）"是两套世界。文档应明确 SQLite 模式仅适合低并发/单机。
- `start_all.py` 进程编排用固定 `sleep(3/5)` 等待就绪（`:113/152/173`），非健康探活，慢机器上可能误判启动失败。

**15. 日志质量**
- 大量 emoji 日志，调试友好但生产噪声大、不利结构化采集。
- `get_next_task` 失败重试用 `logger.exception`（`task_db.py:265`）在高并发锁竞争时刷大量栈，建议降级。

### 整体评价（调度并发）
功能完整、经历过多轮生产事故打磨——随处可见针对真实 bug 的修复（OOM 重启重拆、child_completed 误清零、孤儿文件、合并丢图、死 worker 回收），父子任务三层兜底恢复（`reset_tasks_from_dead_worker`/`reset_stale_tasks`/`reap_stale_parent_tasks` + 心跳续租）考虑周全，SQLite 认领原子性（`BEGIN IMMEDIATE` + 条件 UPDATE 的 `rowcount` 校验）**正确**。但架构有两个根本裂缝：(1) Redis 与 SQLite 双写一致性没从设计层解决（#1 丢任务、#5 职责不清），Redis 可靠投递层半接线；(2) SQLite 连接缺 WAL/busy_timeout 基础配置（#2）。父任务合并与兜底回收缺原子互斥（#3/#4）是超大 PDF 场景最易引爆的竞态。优先级：#2（一行 PRAGMA）→ #1/#5 → #3/#4。

---

## 1.2 处理引擎与工具模块

审核范围：`mineru_pipeline`、`paddleocr_vl(_vllm)`、`audio_engines`、`video_engines`、`format_engines`、`utils`、`storage`、`remove_watermark`、`output_normalizer`

### 🔴 严重问题

**S1. MinerU / PaddleOCR-VL 自动休眠监控与 `parse()` 存在 use-after-free 竞态**
- 证据：`mineru_pipeline/engine.py:89-106`（`_auto_sleep_monitor`）、`:298-349`（`parse` 中 `_load_pipeline` 后长时间 `do_parse`，全程不持锁）；`paddleocr_vl/engine.py:130-137`、`:185-202`（`cleanup` 置 `_pipeline=None`）、`:226`。
- 监控线程在 `if self.is_processing or self.is_offloaded: continue` 后即决定 `cleanup()`，"检查-动作"与 worker 线程写入间**无锁同步**。极端时序下任务刚进入 `parse`、置 `is_processing=True`、开始加载/推理时，监控线程已通过检查并执行 `cleanup()`，把 `_pipeline` 置 None → 正在消费的生成器/推理崩溃或显存损坏。
- 影响：偶发 NoneType 崩溃、显存损坏、任务失败，难复现。
- 建议：监控线程的"判断 + cleanup"整体放进 `self._lock`，锁内重新校验 `is_processing` 与 `last_active_time`；`_load_pipeline` 成功后应在锁内将 `is_offloaded=False`（PaddleOCR 的 `_load_pipeline` 从不复位该标志，`paddleocr_vl/engine.py:96,141-183`，状态机不自洽）。

**S2. VLLMController 容器互斥缺少跨 worker 锁 → 容器 stop/start 抖动**
- 证据：`litserve_worker.py:142-202`（`ensure_service`）、`:239`（每个 worker 各自 `VLLMController()`）、`:465-472`。
- 每个 GPU worker 独立持有 controller，`ensure_service` 内无分布式/文件锁。worker A 要 `paddleocr`、worker B 要 `mineru` 时互相 `stop()`/`start()`，产生容器抖动；`conflict.stop()` 后只 `time.sleep(2)`（`:173` 魔法数）就认为显存释放完成，不可靠。第 175-176 行 `except Exception: pass` 吞掉停止冲突容器的全部错误。
- 影响：混合 backend 负载下 vLLM 容器反复重启，任务大面积超时/失败。
- 建议：用跨进程文件锁串行化 `ensure_service`；轮询确认显存真正释放而非固定 sleep；缩小 except 范围并记日志。

**S3. 格式引擎注册表共享单例 + `parse()` 把 i18n 状态写到 `self` → 并发污染**
- 证据：`format_engines/base.py:88-89,104`（`_engines` 存的是实例）；`fasta_engine.py:80-81`、`genbank_engine.py:83`（`self.semantic_gen = ...`、`self.common_i18n = ...` 在 `parse()` 内赋值，后续 helper 读取）。
- 注册表每种格式仅一个共享实例，两个并发 `parse()` 在 `self.common_i18n` 等属性互相覆盖（A 设 zh、B 设 en，A 读到 B 的值）→ 输出语言错乱甚至 `AttributeError`。
- 建议：注册表改存"类"按需实例化，或把 i18n/semantic_gen 改为方法局部变量贯穿调用，使 `parse` 无状态。

**S4. 不可信上传文件导致 DoS/OOM（无大小上限）**
- 证据：FASTA `_find_orfs` 双层 while O(n²) 无序列长度上限 `fasta_engine.py:254-283`；`_find_repeats` `:341-389`；GenBank 全量载入 `str(record.seq)` + 全部 annotations/features 到内存且 `include_full` 默认 True `genbank_engine.py:107-143,233-234`。
- 影响：上传大基因组 FASTA（兆碱基）让 worker 长时间挂死；大 `.gbff` 直接 OOM。
- 建议：对解析序列长度/记录数设上限；大文件默认 `include_full=False`；ORF 改单次线性扫描。

### 🟡 中等问题

**M1. vLLM PaddleOCR 引擎健康检查不强制 + 每任务重载模型**
- 证据：`paddleocr_vl_vllm/engine.py:115-117`（健康检查失败只 `logger.error` 后继续构建 pipeline，不 raise）；`:166-167,360`（`parse` 起止各调一次 `cleanup()`，使"每进程加载一次"的 docstring 名不副实，实际每任务重建 `PaddleOCRVL`）。
- 建议：健康检查失败 `raise`；重新评估每任务重载必要性或修正 docstring。健康 URL 用 `urljoin` 而非 `.replace("/v1","")`（`:93-101` 对含 `v1` 子串的 host 会误伤）。

**M2. fitz / pikepdf / PIL 句柄在异常路径泄漏**
- 证据：`pdf_utils.py:46-90`（`fitz.open` 仅成功路径 close）、`:154-202`（`pikepdf.open` 异常泄漏；`chunk_pdf = pikepdf.new()` `:169` 从不 close）；`remove_watermark/pdf_watermark_handler.py:139-190,254-271`（`fitz.open` 无 try/finally）；`watermark_remover.py:360` 与 `keyframe_extractor.py:211`（`Image.open` 未 close）；`keyframe_extractor.py:83-131,146-165`（`cv2.VideoCapture` 循环中异常不 release()）。
- 影响：长生命周期 worker 累积原生句柄/FD 泄漏，Windows 下阻塞临时目录清理。
- 建议：统一 try/finally 或 with 包裹；`cap.release()`、`pix=None` 显式释放。

**M3. 临时文件在异常路径泄漏**
- 证据：`video_engine.py:135-139`（临时 WAV 写到 `OUTPUT_PATH` 根，仅成功路径 `:431` 删除，转写/OCR 失败 `:444` 直接 re-raise 不删）；`office_to_markdown.py:31-48`（`mkstemp` 创建的临时 xlsx 在 zip 重写失败时不删）。
- 建议：用 try/finally 清理；视频临时音频写入任务子目录而非共享根。

**M4. 编辑型 PDF 去水印的图片分支是空操作但报告成功**
- 证据：`pdf_watermark_handler.py:164-172`（`try: pass` 空循环，注释承认 PyMuPDF 不支持删图）；默认 `remove_images=True` 且无 keywords 时只删注解，日志却显示 `Removed 0 watermark objects` 并标记成功。
- 影响：用户拿回与原件相同（水印仍在）的"已处理"PDF，误导性极强。同类问题：扫描页处理逐页失败时把原始带水印图塞回输出（`:295-298`）仍报成功。
- 建议：删除死代码，实现真正的图片水印去除（redaction）或明确告警/回退到扫描模式。

**M5. perse_uitls.py 文件名拼写错误**
- 证据：`backend/utils/perse_uitls.py`（应为 `parse_utils.py`）；经 `__init__.py:6` re-export，调用方（`start_all.py:22`、`litserve_worker.py:94`）通过包名访问，但磁盘文件名错误仍在。
- 建议：重命名为 `parse_utils.py` 并更新 `__init__.py` import。

**M6. base_output_normalizer JSON 图片替换过宽 → 文本损坏**
- 证据：`output_normalizer/base_output_normalizer.py:257`（`if filename in value and STANDARD_IMAGE_DIR in value:` 用整串替换为 URL；`filename in value` 子串匹配可误中 `a.jpg`↔`ba.jpg`）。
- 影响：自由文本字段提到图片路径时整段被替换为裸 URL，丢失上下文。
- 建议：精确匹配 `images/<filename>` token，只替换该子串。

**M7. SenseVoice `_parse_result` 时间戳/词对齐逻辑错误**
- 证据：`audio_engines/sensevoice_engine.py:559-599,681-712`，`word_idx` 来自 `timestamp`，但用 `transcript.split()[word_idx]` 取词——对中文 `split()` 几乎不分词，`word_idx` 与按空格切的列表完全不对应，多数 segment 文本为空或错位。
- 影响：基础模式的"详细时间戳"段落文本基本无效。
- 建议：使用模型返回的字级 token 对齐，而非 `transcript.split()`。

**M8. DOCX/PPTX 表格未转义 `|`**
- 证据：`office_to_markdown.py:154,293`（仅 `replace("\n"," ")`，未转义 `|`；XLSX 路径 `:384` 已正确转义）。
- 建议：统一 `replace("|","\\|")`。

**M9. FFmpeg / ffprobe 无超时；DOCX zip 无防压缩炸弹**
- 证据：`video_engine.py:190-192`（`extract_audio` 的 `subprocess.run` 无 timeout）、`:486`（`ffprobe` 无 timeout，而 `check_ffmpeg` `:467` 有）；`docx_image_extractor.py:89-97` 与 `office_to_markdown.py:33-47`（zip 解压无大小上限）。
- 影响：损坏视频让 FFmpeg 永久挂死 worker；恶意 zip 炸弹耗尽磁盘/内存。
- 说明：命令注入本身**不存在**（全部用 argv 列表，无 `shell=True`，为优点）。
- 建议：subprocess 加 timeout 并捕获 `TimeoutExpired`；解压前校验 `ZipInfo.file_size` 总预算。

**M10. 去水印模型无锁加载 + 永不释放显存（违背项目自动休眠约定）**
- 证据：`watermark_remover.py:177-223`（`_load_yolo/_load_lama` 无 Lock，非线程安全双检）、`:399-412`（`cleanup` 存在但全流程无人调用）；worker `:358` 仅初始化一次。
- 影响：worker daemon 线程并发时可能重复加载 YOLO11x+LaMa 致 OOM；首次去水印后数 GB 显存被永久占用，与 MinerU 的 5 分钟自动休眠不一致，挤占共享 GPU。
- 建议：加 `threading.Lock` 双检；实现空闲释放或每任务后 `cleanup()`。

### 🟢 轻微问题
- **G1** `_clean_markdown` 调试逻辑混入业务：`mineru_pipeline/engine.py:166-167`，`if "117" in text or "LVEDd" in text` 硬编码字符串触发 debug 日志，是调试残留。`:174,177,180,186` 的暴力正则去重 `(\S+)(\s+)\1` 可能误删合法重复词。
- **G2** 单例 `get_engine` 忽略 device/model_name：`paddleocr_vl/engine.py:420-424`、`mineru_pipeline/engine.py:473-477`、`video_engine.py:499-507`、`rustfs_client.py:384-396` 均无锁懒初始化且二次调用静默返回首个实例，参数不一致时无告警。
- **G3** 硬编码容器路径：`watermark_remover.py:97-98`（`/app/models/...`）、`paddleocr_vl/engine.py:157`（`/root/.paddlex`）、`video_engine.py:135-137`（fallback `data/output`），本地开发不可移植。
- **G4** 魔法数字遍布：`keyframe_extractor.py:108(160,90),136(10),159(95),406(0.9)`、`video_engine.py:90(25.0)`、`paddleocr_vl_vllm/engine.py:240(595.28)`、FASTA 多处生物学阈值 `:254,300,345,394` 等。
- **G5** 异常吞没（`except: pass` / 只 debug）：`litserve_worker.py:175-176,199-202`、`paddleocr_vl_vllm/engine.py:152,233-234,280-281,286-293`、`keyframe_extractor.py:195-196,232-233,246-247,379-380`、`output_normalizer/*:73-74,150-151`、`docx_image_extractor.py:163-166,214-215`。
- **G6** 死代码：`docx_image_extractor.py:176-224`（`build_markdown_with_inline_images` 改了局部 `doc_content` 却返回未改的 `markdown_content`，纯无效）；`watermark_remover.py:23,121`（`LAMA_AVAILABLE=True` 恒真常量）；`video_engine.py:499-507`（`get_engine` 与 worker 直接构造冲突的死路径）。
- **G7** GenBank 序列化风险：`genbank_engine.py:114,230`，`dict(record.annotations)` 含 BioPython `Reference` 等非 JSON 对象，下游 `json.dumps` 会 `TypeError`；`:401-404,414-417` 对 `qualifiers[...][0]` 无空列表保护，单个畸形 feature 即令整文件解析失败。
- **G8** 超长函数：`mineru_pipeline/engine.py:218-466`（parse ~250 行）、`video_engine.py:236-456`（~220 行，含三处重复 JSON 序列化、两套 markdown 合并）。
- **G9** RustFS 公开桶策略：`storage/rustfs_client.py:182-195` 自动设桶为 `s3:GetObject` 全公开读，上传内容对匿名可读，需结合部署网络评估；`:121-125` 默认凭据 `rustfsadmin/rustfsadmin`。
- **G10** XLSX 静默截断：`office_to_markdown.py:373` `max_row=min(max_row,2000)` 静默丢弃 2000 行以外数据，无告警。

### 整体评价（引擎与工具）
功能完整、工程化程度高，针对实战踩坑的修复明显（SSRF 逐跳校验 `markdown_image_extractor.py:172-194`、LFI 子树约束 `:273-310`、中文路径 `cv2.imdecode/imencode`、临时英文目录绕过中文路径、PyTorch 2.6 `weights_only` 兼容、dead-worker 恢复）。subprocess 全用 argv 列表、**无命令注入**，`ast.literal_eval` 替代 `eval`，远程图片有大小/重定向/SSRF 防护，是亮点。主要短板：(1) 并发与状态机（S1-S3 都缺锁，最高优先级）；(2) 资源管理（M2/M3 只在成功路径释放，M10 不释放显存）；(3) 不可信输入资源上限缺失（S4/M9）。另有若干"报告成功实则降级/空操作"的误导性逻辑（M1/M4），建议优先处理。整改顺序：S1→S4→M10→M4→M2/M3。

---

## 1.3 认证授权与安全

审核范围：`backend/auth/*`、`backend/api_server.py`、`backend/mcp_server.py`

### 🔴 严重问题

**1. 用户注册接口存在权限提升（自助创建 admin）**
- 描述：`POST /api/v1/auth/register` 无需认证，直接接收 `UserCreate`，而 `UserCreate` 含 `role` 字段且无任何剥离。`auth_db.create_user()` 原样写入 `user_data.role.value`。任何匿名用户注册时可直接指定 `role: "admin"`。
- 证据：`auth/routes.py:41-50`（register 无鉴权依赖，直接 `auth_db.create_user(user_data)`）、`auth/models.py:105-112`（`UserCreate.role: UserRole = UserRole.USER` — 客户端可覆盖）、`auth/auth_db.py:190-207`（`create_user` 写入 `user_data.role.value`，不做角色校验）。
- 攻击场景：`curl -X POST /api/v1/auth/register -d '{"username":"evil","email":"e@e.com","password":"password123","role":"admin"}'` → 立即拥有 admin，越权读取/删除全部任务、改系统配置、管理所有用户。
- 修复：公开注册端点必须强制 `role = UserRole.USER`（忽略请求体 role），或为 register 单独定义不含 role 的 `UserRegister` 模型。仅 `POST /users`（admin 专用，`routes.py:246`）允许指定角色。

**2. 公开注册未受 `allow_registration` 开关约束**
- 描述：系统配置有 `allow_registration` 开关（`system_config.py:81`、`routes.py:451`），但 `register` 端点完全不读取，关闭注册无效。
- 证据：`auth/routes.py:40-52`。
- 修复：register 开头检查 `allow_registration`，为 false 时返回 403。

**3. CORS 配置 `allow_origins=["*"]` + `allow_credentials=True`**
- 描述：通配源叠加凭证。`.env.example` 定义了 `ALLOWED_ORIGINS` 但代码完全未使用，硬编码为 `*`。
- 证据：`api_server.py:83-89`。
- 说明：本系统 JWT 走 `Authorization: Bearer`（非 cookie），实际效果是允许任意站点用受害者浏览器中的脚本读取响应/触发接口。即便 token 在 localStorage，恶意第三方页面仍可对 API 发任意跨域请求并读取响应。
- 修复：用 `os.getenv("ALLOWED_ORIGINS")` 解析为白名单列表，不要与 `*` 混用 credentials。

**4. 文件服务接口与 MCP 接口完全无鉴权（IDOR / 任意结果下载）**
- 描述：`GET /api/v1/files/output/{file_path:path}` 和 `GET /api/v1/files/upload/{file_path:path}` 没有任何认证依赖。任何匿名用户只要知道（或枚举）路径即可下载任意用户的上传源文件与解析结果。`get_task_status` 把 `source_url`/`pdf_path`/图片 URL 直接返回，路径可预测（基于文件名 stem、images 目录），构成跨用户 IDOR。MCP server 的 `parse_document/get_task_status/list_tasks/get_queue_stats` 代理到 API 但所有代理请求都不带任何 Authorization/API Key，且 MCP 暴露在 `0.0.0.0:8002` 自身无认证。
- 证据：`api_server.py:1131-1156`（`serve_output_file` 无 Depends）、`:1158-1182`（`serve_upload_file` 无 Depends）、`mcp_server.py:358,400,533,585,623`（所有 session.post/get 均无 auth header）、`mcp_server.py:705`（`MCP_HOST` 默认 `0.0.0.0`）。
- 攻击场景：GET `/api/v1/files/output/<别人文件stem>/result.md` 拖取他人结果；或直连 MCP 8002 端口免认证调用 `parse_document`。
- 修复：为两个文件接口加 `Depends(get_current_active_user)` 并校验文件所属 task 的 `user_id == current_user.user_id`（或 `TASK_VIEW_ALL`）。MCP server 应要求自身认证并将调用方凭证透传到 API。

### 🟡 中等问题

**5. 上传文件名路径遍历风险**
- 描述：`unique_filename = f"{uuid.uuid4().hex}_{file.filename}"`，`file.filename` 未做 basename 化/清洗即拼到 `UPLOAD_DIR`。含 `../` 时会写到 UPLOAD_DIR 之外。
- 证据：`api_server.py:278-281`。
- 修复：`safe_name = Path(file.filename).name`，resolve 后校验 `is_relative_to(UPLOAD_DIR)`。

**6. MCP `file_url` 下载存在 SSRF**
- 描述：`parse_document` 接受任意 `file_url` 并由服务器直接 `session.get(url)` 下载，无协议/主机白名单、无内网地址过滤。
- 证据：`mcp_server.py:266-324`。
- 攻击场景：`file_url=http://169.254.169.254/latest/meta-data/...` 让服务器代请求云元数据，结果回流到解析输出。
- 修复：限制 scheme 为 http/https，解析目标 IP 拒绝私有/保留网段（含重定向后最终地址），加超时与大小限制。

**7. OIDC SSO 缺少 SessionMiddleware → state/nonce 无法存储（CSRF/实现破损）**
- 描述：authlib starlette client 依赖 `SessionMiddleware` 存储校验 OAuth `state`/`nonce`，代码中未注册 SessionMiddleware，OIDC 登录要么报错要么 state 校验被绕过。
- 证据：`auth/routes.py:367`（`authorize_redirect`）、`:396`（`authorize_access_token`），全后端无 SessionMiddleware。
- 修复：注册 `SessionMiddleware`（强随机 secret）；并复用单例 OAuth 实例（当前每次请求 new `OAuth()` 也会破坏 state 关联）。

**8. 错误信息泄露内部细节**
- 证据：`api_server.py:368`（`raise HTTPException(500, detail=str(e))`）、`:1128`（health check 返回 `str(e)`）、`auth/routes.py:573`（logo 上传返回 `str(e)`）、`:262`（`create_user` 把 `ValueError` 原文返回，可用于用户名/邮箱枚举）。
- 修复：对外返回通用错误信息，详细堆栈仅记录到日志。

**9. API Key 校验非恒定时间比较**
- 描述：`verify_api_key` 用 `hashlib.sha256` 哈希后 SQL `=` 比对（只 sha256 一次无 salt）；密码校验 `_verify_password` 用 Python `==` 比较 hash。应使用 `hmac.compare_digest` 防时序侧信道；全仓无任何 `compare_digest`。
- 证据：`auth/auth_db.py:172`、`:406-418`。
- 修复：密码/key hash 比较改用 `hmac.compare_digest`。

**10. 管理员可越权提权/改他人角色、删用户无防末位 admin**
- 描述：`PATCH /users/{user_id}`（admin）允许改任意用户 role/is_active，但无"防止删除/降级最后一个 admin"保护；`delete_user` 仅防删自己（`routes.py:306`）。
- 证据：`auth/routes.py:265-292`、`:295-315`。
- 修复：限制最后一名活跃 admin 不可被删除/降级。

### 🟢 轻微问题
- **11. JWT 无刷新机制、24h 长有效期、无 jti/吊销**：`jwt_handler.py:30,51-60`。用户被禁用后已签发 token 在过期前仍可用（`get_current_active_user` 会校验 is_active 部分缓解；但角色变更后旧 token 仍持原 role 直到过期）。建议缩短有效期 + refresh token，或加 token 版本号/jti 吊销。
- **12. 密码哈希用 PBKDF2-SHA256（可接受）**：`auth_db.py:159-174`，100k 迭代 + 16 字节 salt，合规且安全（远优于 md5/sha 裸哈希）。建议升级 bcrypt/argon2id。**正面**：JWT 密钥管理做得好——`jwt_handler.py:20-28` 拒绝默认/弱密钥并强制 ≥32 字符；默认 admin 密码取环境变量或随机生成（`auth_db.py:139-157`），未硬编码弱口令。
- **13. SQL 查询安全（无注入）**：`list_tasks`（`api_server.py:872-929`）动态 WHERE 全走参数化 `?`，列名硬编码；`update_user` 的 `set_clause` 由 `allowed_fields` 白名单生成列名（`auth_db.py:295-306`），安全。正面确认。
- **14. 第三方服务默认弱凭证**：`.env.example` 中 `RUSTFS_ACCESS_KEY=rustfsadmin / RUSTFS_SECRET_KEY=rustfsadmin`、`REDIS_PASSWORD=`（空）。部署文档应强调必须替换。

### 整体安全评价
核心认证基础不错：JWT 密钥强制非默认/足够长、密码用 PBKDF2、API Key 哈希存储、RBAC 清晰（`models.py` 的 `ROLE_PERMISSIONS`），任务级 IDOR 在 `/tasks/*` 有 `user_id` 归属校验，SQL 全参数化。但有两个致命缺陷使授权体系崩溃：(1) 公开注册可自助成 admin（#1）；(2) 文件服务与 MCP 完全无鉴权（#4）。加上 CORS 通配（#3）、上传文件名未清洗（#5）、SSRF（#6）、OIDC 缺 SessionMiddleware（#7），当前不适合不可信网络直接上线。修复优先级：#1 → #4 → #3/#2 → #5/#6/#7 → 其余。修复 #1、#2、#4 后整体风险可从"严重"降至"中等可控"。

---

## 1.4 前端代码质量与架构（Vue 3 + TS）

审核范围：`frontend/src/` 全量（约 6200 行 Vue 代码）+ 构建配置

### 🔴 严重问题

**1. Markdown 渲染存在 XSS 漏洞（无 HTML 净化）**
- 证据：`components/MarkdownViewer.vue:12` `v-html="renderedContent"`；`:50` `marked.parse(props.content)` 直接输出；`package.json:28` 仅有 `marked`，全仓库无 `dompurify`。
- 影响：`task.data.content`/`block.text`（后端解析内容）含 `<img onerror=...>`、`<script>`、`<a href="javascript:...">` 会原样注入 DOM。任意用户上传含恶意 HTML 的文档，其他查看者（含管理员）即被 XSS，可窃取 localStorage 中的 JWT。最高危。
- 建议：引入 `dompurify`，`DOMPurify.sanitize(html)`；或配置 marked 不放行原始 HTML。KaTeX 输出在净化白名单保留必要标签/属性。

**2. `marked@16` API 不兼容 —— `highlight` 配置是死代码，且 `parse` 返回类型已变**
- 证据：`MarkdownViewer.vue:32-45` 用 `marked.setOptions({ highlight, breaks, gfm })`，但 marked v5 已移除 `highlight` 选项（迁移到 `marked-highlight` 插件），项目用 `^16.4.1`。
- 影响：代码高亮**完全不生效**（hljs 是引入但回调从未被调用的死代码）。v16 `marked.parse()` 默认返回 string，但若触发 async 扩展返回 `Promise<string>`，`html.replace(...)` 会炸/渲染 `[object Promise]`。当前侥幸为 sync 但用法是错的。
- 建议：改用 `marked-highlight` 插件接入 hljs，或显式 `marked.parse(content, { async: false })` 并移除无效 highlight。

**3. i18n 当前语言在启动时未同步到 vue-i18n 实例**
- 证据：`locales/index.ts:51` 创建 i18n 时 locale 取 `getSavedLocale() || getBrowserLocale()`（正确）。但 `localeStore.ts:31 setLocale` 只改 store 的 `currentLocale` 与 localStorage，不动 i18n 的 `locale.value`；真正同步发生在 `AppLayout.vue:422-426 handleChangeLocale`（手动双写）。`main.ts` 与 `App.vue:1-7`（空壳）都没把 store 与 i18n 关联。
- 影响：当前因初值也读同一 localStorage key 表面正常；但 `localeStore.setLocale` 被任何非 AppLayout 路径调用时 UI 文案不会切换——存在隐性状态双源。
- 建议：在 `localeStore.setLocale` 内直接调用 i18n（或暴露统一 `useLocale` composable），删除 AppLayout 手动双写。

### 🟡 中等问题
- **4. 全局缺 HTML/路径净化与 CSP，token 存于 localStorage**：`authStore.ts:12,31`、`client.ts:74`。结合问题 1 的 XSS，JWT 完全暴露在 JS 可读区。建议理想用 httpOnly Cookie；至少先修 XSS + 缩短 token 有效期。
- **5. `Task['data']` 等大量 `any`，类型安全形同虚设**：`types.ts:279 json_content?: any`；`VirtualPdfViewer.vue:71 layoutData?: any[]`、`:76 block: any`；`TaskDetail.vue:179-256 layoutData` 里 `b: any` 反复；`JsonNode.vue:90 data: any`；所有 `catch (err: any)`。最易出 bug 的坐标换算/双向定位区全靠运行时 `??` 兜底。建议为 block 结构定义 union 类型；catch 用 unknown + 类型守卫。
- **6. `TaskDetail.vue` 的 `layoutData` 解析逻辑过重，应抽 composable**：`:179-256` 单个 computed 近 80 行处理 4~5 种后端 JSON schema，且与 `VirtualPdfViewer.vue:124-150` 的 bbox 推断高度耦合、各自实现一遍坐标推断。建议抽 `useDocumentLayout(jsonContent)` 共用。
- **7. 轮询/定时器管理分散，存在潜在重复轮询**：`TaskDetail.vue:351-355`（正确清理但 `taskStore.ts:252 pollTaskStatus` 完成时双重停止冗余）；`queueStore.ts:64-94`（自适应轮询 clearInterval+重建分散）；`AppLayout.vue:454-463 handleVisibilityChange` 可见时 `startAutoRefresh(5000)` 会重置自适应间隔回 5s 使退避失效；`TaskList.vue:608` 固定 5s 轮询，切后台不暂停。建议统一 `usePolling(fn, { interval, pauseOnHidden })`。
- **8. 路由守卫每次导航可能阻塞式加载系统配置**：`router/index.ts:95-97`，只要 `config.system_name` 仍是默认值就 `await systemStore.loadConfig()`；`AppLayout.vue:467` onMounted 也调一次。若后端配置就叫默认名 `MinerU Tianshu`，则每次导航都重复请求（判断条件永真）。建议用 `loaded` 标志位幂等化。
- **9. 错误反馈不一致**：alert/console/toast/页面横幅四种混用。`TaskList.vue:481,560 alert`、`:629` 复制注释建议替换；`JsonViewer.vue:196 alert`；`FileUploader.vue:119 alert`；而 authStore 用 showToast，TaskDetail/TaskSubmit 用页面横幅。建议统一走 `utils/toast.ts`。
- **10. 登录忽略 `redirect` 查询参数**：`router/index.ts:127-130` 携带 `query: { redirect: to.fullPath }`，但 `Login.vue:112-118` 成功后硬编码 `router.push('/')`。建议 `router.push((route.query.redirect as string) || '/')`。
- **11. `apiClient.timeout = 300000` 固定值，无上传进度**：`client.ts:47` 5 分钟固定超时；`taskApi.ts:73 submitTask` 用 FormData POST 但无 `onUploadProgress`；`TaskSubmit.vue:660-670` 串行 for 循环逐个 await。大文件/弱网无进度，超 5 分钟直接超时。建议加 `onUploadProgress`、有限并发、上传类请求单独设更长超时。

### 🟢 轻微问题
- **12.** `client.ts:33-40,61-71` 反复正则剥离 `/api/v1`，`:54,82` 调试 log 入生产；`taskApi.ts` 又写死全路径与拦截器互相打补丁。建议统一约定 baseURL 含 `/api/v1`，各 api 只写相对路径。
- **13.** 魔法字符串散落：`'auth_token'`/`'token'`（`client.ts:74` 清两个 vs `authStore.ts:12,118` 只清 `auth_token`，埋登出不彻底隐患）；`'task_submit_config'`、`'task_list_auto_refresh'`、`'locale'`；`result_path === 'CLEARED'` 散布 `types.ts:263`、`TaskDetail.vue:19,29`、`TaskList.vue:209,276`；完成态数组 `['completed','failed','cancelled']` 多处重复。建议集中 `constants.ts`。
- **14.** 列表渲染用 index 作 key：`FileUploader.vue:32`、`TaskSubmit.vue:407`、`JsonNode.vue:59`。`removeFile` 后可能 DOM 复用错位。Task 列表用了 task_id（正确）。
- **15.** 权限模型前端硬编码且基本 dead code：`authStore.ts:201-239 hasPermission` 把权限清单硬编码，实际视图几乎未调用（路由守卫只用 isAdmin）。建议以后端为准或删除。
- **16.** 其它：`toast.ts:77-83` 生产每次 toast 都 console.log；`JsonNode.vue` 递归组件对超大 JSON 无虚拟化，`expandAll`（`JsonViewer.vue:143`）可能卡死；`TaskSubmit.vue:631-639 resetForm` 清空 uploader 是空注释（而 `FileUploader.vue:140` 已 `defineExpose({ clearFiles })` 未接上）；`formatFileSize`（`format.ts:47`）未处理负数/NaN；`TaskDetail.vue:150` 导入 `Pause` 未使用（`noUnusedLocals` 应报错——说明 tsc 类型检查可能未在 CI 真正拦截，值得核实 build 是否通过）。

### 整体评价（前端）
功能完整、UI 打磨精细（Tailwind 设计、虚拟 PDF + 双向定位、自适应轮询、i18n 双语、预设配置持久化），`VirtualPdfViewer` 的虚拟滚动 + Canvas 回收 + bbox 坐标换算实现用心。但有一个必须立即修复的高危 XSS（问题 1），结合 JWT 存 localStorage（问题 4）构成完整可利用的 token 窃取链路。`marked@16` 的 highlight 是死代码（问题 2）。代码质量主要短板：类型安全松弛（any 集中在最易错区）、逻辑复用不足（轮询/文档 schema 解析/错误提示重复造轮子）、API 客户端历史补丁残留。优先级：P0 修 XSS（DOMPurify）→ 修 marked 高亮 → 统一 token 存储/清理一致性 → 抽轮询与文档解析 composable → 收敛 any 与魔法字符串。

---

## 1.5 测试覆盖、工程化与可维护性

### 1. 测试 — 🔴 严重
**整个仓库零测试。** 遍历确认无 `test_*.py`/`*_test.py`/`*.spec.*`/`*.test.*`/`conftest.py`；`requirements.txt` 与 `package.json` 均无 pytest/vitest/jest/@testing-library。CLAUDE.md 已诚实声明"no test framework"。

对本系统的风险（按代码复杂度排序）：

| 模块 | 行数 | 风险点 | 证据 |
|---|---|---|---|
| `task_db.py` | 1231 | `get_next_task` 用 `BEGIN IMMEDIATE` + 重试做原子认领（`:201-246`）；父子计数/合并（`:945-1054`）；SQLite/Redis 双路径。竞态/`database is locked`/重复认领/合并丢任务只在并发下出现，无测试几乎不可能回归捕获。最近 commit `b25935e` 正是修"分片合并丢图" | `:217 BEGIN IMMEDIATE`、`:246 retrying` |
| `litserve_worker.py` | 1412 | `_process_task` 按 backend+文件类型分发 9+ 引擎（`:452`），`_merge_parent_task_results`（`:1164`）、`_should_split_pdf`（`:1093`）、vLLM 容器互斥（`VLLMController:145`）。圈复杂度极高 | — |
| `auth/`(jwt/auth_db/routes) | ~1100 | 鉴权与 token 校验完全无测试；登录/注册/API-key 一旦回归即安全事故 | — |
| `redis_queue.py` | 490 | 优先级队列 + 降级回退，分布式语义无验证 | — |
| `output_normalizer/`、`format_engines/` | ~1000 | 纯函数式，**最易测、ROI 最高** | — |

无测试 + 178 处 `except Exception`（吞异常普遍）意味着错误极易被静默吞掉而无回归网兜底。

### 2. CI/CD — 🟡 中等
- CI 仅做 lint，不做任何测试/构建/安全扫描。唯一 workflow `.github/workflows/pylint.yml`（名为 pylint 实际跑 pre-commit）只执行 pre-commit。无 `npm run build`（前端 tsc 未进 CI）、无 Docker 构建验证、无 `pip-audit`/`npm audit`/Trivy/CodeQL。
- 🟡 工作流文件名 `pylint.yml` 与实际内容（pre-commit）不符。
- pre-commit 覆盖度尚可（`.pre-commit-config.yaml`）：基础文件检查、ruff-format/lint、shellcheck、markdownlint、detect-private-key、大文件检查。但：ruff 只开 `E,F` 且 ignore `E402,E501`（等于只查语法和未使用变量，不查复杂度/安全/import 排序）；shellcheck 排除了 `docker-entrypoint.sh`/`docker-setup.sh`（恰是部署关键脚本）；前端无 ESLint/Prettier。

### 3. 依赖管理 — 🟡 中等
**后端 `requirements.txt`：**
- 版本固定不一致：少数核心包硬钉（`numpy==1.26.4`、`fastapi==0.115.6`、`pydantic==2.10.5`、`litserve==0.2.16`），大量包 `>=` 浮动（`mineru[all]>=3.0.9`、`requests>=2.32.5`、`ultralytics>=8.3.0`），7 个完全无版本约束：`psutil`、`py-cpuinfo`、`reportlab`、`prettytable`、`ujson`、`chardet`、`ruamel.yaml`。无 lock 文件，构建不可复现。
- 可疑/未来日期约束：`pdfminer.six>=20251230`、`regex>=2026.1.15`，浮动下限过激进。
- 重型依赖体积巨大：`mineru[all]`、`paddleocr[all]`、`paddlepaddle-gpu`、`torch==2.10.0+cu130`、`ultralytics`、`funasr`、`transformers`、`libreoffice`——镜像数 GB 到十几 GB。`numpy==1.26.4` 硬钉与 `pandas>=1.3.0`、`scikit-image>=0.25.0` 存在潜在 ABI 冲突。
- 正面：注释明确标注"为何显式声明"，意图清晰。

**前端 `package.json`：** 全部 `^`/`~` 范围；`pdfjs-dist` 钉死 `3.11.174`（合理）。
**dify_plugin：** `yarl` 无版本约束。

### 4. 可维护性 — 🟡 中等
- **🔴 Dockerfile/文档 CUDA 版本三方打架**：`Dockerfile`（CUDA 13.0.2 + torch 2.10.0+cu130 + flash-attn cu130）vs `Dockerfile.offline`（CUDA 12.6.2）vs `CLAUDE.md/README`（CUDA 12.1）。三处互不一致，GPU/torch/flash-attn/paddle 极敏感，文档脱节会导致按错文档部署 GPU 不可用。
- Dockerfile 分层：多阶段构建 + pip cache 合理（正面）；`--break-system-packages` 全程使用（未用 venv）；主 Dockerfile 装 libreoffice 全套 + 多套 CJK 字体显著增大体积；`requirements.txt` 在 Step5 用 sed 删行过滤重型包再装（逻辑脆弱）；两个 RUN step 都注释为 "Step 5"（`Dockerfile:91,96` 编号重复）。
- 可观测性：有 `/health`（`api_server.py:1116`）；但健康检查不一致——`docker-compose.cpu.yml`/`docker-compose.offline.yml` 有 healthcheck，**主 `docker-compose.yml` 和 `docker-compose.dev.yml` 没有**；无 Prometheus/`/metrics`、无 OpenTelemetry、无 Sentry；日志混用——33 文件用 logging/loguru，仍有 43 处裸 `print()`。
- 配置管理：`.env.example`（41 键）、`.env.cpu` 齐全，启动强校验（缺 `.env` 即退出）；JWT 安全把关到位（`jwt_handler.py:21-28`）；`rustfs_client.py:123` 默认 secret `"rustfsadmin"` 硬编码兜底。
- 脚本质量：`scripts/` 14 个脚本无任何测试，shellcheck 还排除关键 entrypoint。
- 代码卫生（正面）：0 个 bare `except:`、0 个 TODO/FIXME 残留，无 `eval/exec/os.system/shell=True`（唯一 `eval()` 是 `model.eval()` PyTorch API）。

### 优先补测试清单（按 ROI 排序）

| # | 目标 | 类型 | 理由 |
|---|---|---|---|
| 1 | `task_db.get_next_task` 并发认领 + 父子合并 | 集成（多线程/进程命中同一 SQLite） | 最高价值：系统正确性核心，竞态 bug 历史已发生（b25935e），人工不可复现。先建 pytest + 临时 DB fixture |
| 2 | `output_normalizer/` + `format_engines/`（FASTA/GenBank/office_to_markdown） | 纯单元 | 最高 ROI：纯函数、零 GPU 依赖，投入小覆盖快，立刻把 ~1500 行拉进回归网 |
| 3 | `auth/`（jwt 校验、登录/注册/API-key） | 单元 + API 集成（TestClient） | 安全关键路径，回归即事故 |
| 4 | `_should_split_pdf` / `_merge_parent_task_results` | 单元（mock 引擎） | 历史 bug 高发区（丢图/丢分片） |
| 5 | `redis_queue.py` 优先级队列 + 降级回退 | 集成（fakeredis） | 分布式语义验证 |
| 6 | API 端到端冒烟（submit→status→result）+ `/health` | E2E（TestClient + mock worker） | 挡接口契约回归 |
| 7 | 前端：`api/client.ts` baseURL 解析 + 401 拦截器、`stores/taskStore` | Vitest 单元 | 拦截器/store 逻辑易测且常改 |

配套 CI 改进：CI 增加 pytest（先跑 #1#2）+ 前端 `npm run build`；增加安全扫描（pip-audit/npm audit + 可选 Trivy/CodeQL）；主 `docker-compose.yml` 补 healthcheck + 统一三份 Dockerfile 与文档的 CUDA 版本；生成并提交依赖 lock + 给 7 个无约束包补下限。

### 整体评价（工程化）
代码本身扎实——纪律好（无裸 except、无 TODO 债、无危险调用）、安全意识在位（JWT 强校验、私钥检测 hook）、架构清晰、文档异常详尽，国内开源属上游水准。但工程化成熟度被一个致命短板拉低：完全没有自动化测试，CI 仅 lint。对核心卖点是"并发队列+多引擎+父子合并+鉴权"的平台，所有正确性保障依赖人工和生产暴露，而 git 历史已证明并发/合并出过 bug。配合三份 Dockerfile CUDA 不一致、主编排缺健康检查、零可观测性指标、依赖未锁定，部署可复现性与运行时可诊断性偏弱。**成熟度评级：代码质量 B+ / 工程效能 D。**

---

# 主题二：MinerU 3.0.9 → 3.3.1 升级影响评估

## 2.1 检查方法
逐一核对代码对 MinerU 的**全部 API 依赖面**（仅 3 处，依赖很窄）против 3.3.1 最新源码签名：

| 代码引用 | 位置 | 3.3.1 是否仍存在 | 结论 |
|---|---|---|---|
| `from mineru.cli.common import do_parse` | `engine.py:122` | ✅ 存在 | 路径未变 |
| `do_parse(output_dir=..., 18 个关键字参数)` | `engine.py:330-348` | ✅ 全部参数仍在 | **全用关键字传参** |
| `from mineru.utils.model_utils import clean_memory` | `engine.py:201` | ✅ `clean_memory(device='cuda')` | 兼容 |
| `from mineru.utils.model_utils import get_vram` | `litserve_worker.py:313` | ✅ `get_vram(device)` | 兼容 |

3.3.1 的 `do_parse` 签名：现有 18 个参数 **+ 3 个新增参数（`image_analysis=True`、`client_side_output_generation=False`、`effort=DEFAULT_HYBRID_EFFORT`）+ `**kwargs`**，全部带默认值。

## 2.2 核心结论：调用层不会崩，但有"静默行为变更"
由于项目**全部用关键字调用**（非位置参数），升级后**不会出现 `TypeError`/签名不匹配**。这是直接升级最大的风险点，而它恰好是安全的。

3.0.9 → 3.3.1 各版本要点（来源：GitHub Releases）：
- **3.3.1**（2026-06-11）：Hybrid 性能优化，新 `effort` 参数（medium/high）；medium 比 high 仅降 0.13 分但提速 35%-220%；**默认 backend 用 `effort=medium`，medium 不支持 image analysis**；VLM 升级为 MinerU2.5-Pro-2605-1.2B。
- **3.3.0**（2026-06-11）：开发分支合并。
- **3.2.3**（2026-06-04）：上下标检测/输出；post-OCR fallback。
- **3.2.2**（2026-06-02）：增强 PDF 处理与并发管理；跳过损坏 PDF 页。
- **3.2.1**（2026-05-28）：vLLM 上界放宽到 0.21.0；默认 NVIDIA Docker 基础镜像升 vLLM 0.21.0 + CUDA 13（sm121）。
- **3.2.0**（2026-05-26）：Gradio 改进；依赖优化；**VLM 模型升 2605**；稳定性修复。
- **3.1.15**（2026-05-19）：Gradio 预览/上传体验；Markdown/HTML 图片用 URL 而非 base64；Office 解析健壮性（DOCX 表格/XML/XLSX）；OCR/公式异步。
- **3.1.14/3.1.13/3.1.12**：分类管线优化、Office 解析可靠性、bug 修复。

## 2.3 风险清单

**🔴 风险 1：hybrid 后端默认 `effort=medium`，悄悄丢失图像分析能力**
- 3.3 给 hybrid 后端新增 `effort`，**默认变为 medium**。官方明确 medium 不支持 image analysis，要图像分析或最高精度必须 `effort=high`。
- 项目用 `hybrid-*` 后端（CLAUDE.md 已列），`do_parse` 调用里**没传 `effort`**（旧版本没这参数），升级后落到新默认 medium。
- 后果：**解析结果静默变化**——速度快但精度降 ~0.13 分且丢图像分析，无任何报错。
- 建议：在 `engine.py:330` 的 `do_parse` 调用显式加 `effort=...`，默认设 `high` 保旧行为，或作为可配置项。

**🟡 风险 2：VLM 模型 2509 已被 2605-Pro 取代，离线清单需全量更新**
- 3.2.0 起 VLM 升级到 2605，3.3.1 进一步为 MinerU2.5-Pro-2605-1.2B。项目把 `MinerU2.5-2509-1.2B` **硬编码在约 10 处**：`download_models.py:58,59,352,391`、`prepare_offline_models.py:23,92,176,234,257`、`docker-entrypoint.sh:169,172`、`init-models.sh:65`、`deploy-offline.sh:173`、`docs/MODELS_MANIFEST.md:35,40,184,231`、`docs/OFFLINE_DEPLOYMENT.md`、`OFFLINE_EXTERNAL_MODELS_*.md`。
- 2509 仍可用（不会坏），但不享受新模型精度/多语言 OCR 提升。
- 升级模型需同步改这 ~10 处 + 重新下载 ~2.3GB + 重建离线镜像。

**🟡 风险 3：`fast-langdetect<0.3.0` 与 vLLM 约束需复核**
- `requirements.txt:29-30` 注释写"mineru 3.0 仍约束 `<0.3.0`"，需确认 3.3.1 是否放宽，否则可能阻塞依赖求解。
- 3.2.1 起 MinerU 把 vLLM 上界放宽到 0.21.0 + CUDA 13。项目 `Dockerfile` 已用 CUDA 13.0.2 + `torch==2.10.0+cu130`，**方向一致**（利好），但需核对 vLLM pin 是否落新区间。

**🟢 已自然兼容**
- `content_list_v2.json`（3.0 新增）代码已做"v2 优先、v1 回退"（`engine.py:382,429`），无需改动。
- DOCX 原生解析路径（`engine.py:312-314`）依赖文件名后缀识别，未受影响。

## 2.4 升级验证的致命掣肘
**项目零自动化测试。** MinerU 是核心引擎，升级主要风险又是"不报错的行为漂移"（effort=medium 丢图像分析、模型换代输出变化），这类变更**没有测试无法回归发现**，只能人工逐文档目视比对。**升级 MinerU 前，至少应先为 `mineru_pipeline/engine.py` 输出建立黄金样本（golden file）回归测试。**

## 2.5 升级动作清单（按顺序）
1. 将 `mineru[all]>=3.0.9` 改为钉死 `mineru[all]==3.3.1`（与"依赖应锁定"一致）。
2. `do_parse` 调用显式传 `effort`（建议默认 high 保行为，或暴露为配置项）。
3. 决策 VLM 模型：保留 2509（可用）还是迁移 2605-Pro（改 ~10 处引用 + 重下模型）。
4. 复核 `fast-langdetect` 与 vLLM 版本约束是否需放宽。
5. 升级前先建 `engine.py` 输出的 golden-file 回归测试，升级后比对 md/content_list 差异。

来源：
- MinerU Releases (3.1.12 → 3.3.1)：https://github.com/opendatalab/MinerU/releases
- do_parse 源码 (cli/common.py)：https://github.com/opendatalab/MinerU/blob/master/mineru/cli/common.py
- Advanced CLI Parameters（effort 说明）：https://opendatalab.github.io/MinerU/usage/advanced_cli_parameters/
- MinerU2.5-Pro-2604/2605 模型卡：https://huggingface.co/opendatalab/MinerU2.5-Pro-2604-1.2B

---

# 主题三：模型推理层解耦剥离架构分析

## 3.1 结论
**能，而且项目其实已经走了一半路——但目前走得"不彻底、不默认、不通用"。** 真正障碍不在模型调用本身（耦合很浅），而在"共享文件系统 + 同主机"这个隐式契约。把这层契约换成对象存储交接（项目已有 RustFS 和 `docs/task-level-rustfs-and-image-handoff.md` 在铺路），推理层就能干净剥离。

## 3.2 耦合现状：其实只有"一道真边界"

```
[API提交] --create_task--> [SQLite/Redis 队列] <--get_next_task-- [Worker进程]
                              ↑唯一真边界                            │
                                                                    │ 同进程同线程 _worker_loop
                                                                    ↓ in-process engine.parse()
                                              ┌─────────────────────┴──────────────────────┐
                                              │ MinerU pipeline (do_parse, 进程内驻显存)      │
                                              │ PaddleOCR-VL 非vLLM (进程内)                  │
                                              │ SenseVoice / Video / YOLO+LaMa (进程内)      │
                                              │ MinerU vlm/hybrid ──HTTP──> vLLM容器 ✅已解耦 │
                                              │ PaddleOCR-VL-VLLM ──HTTP──> vLLM容器 ✅已解耦 │
                                              └────────────────────────────────────────────┘
```

关键事实：

**1. LitServe 不是推理服务，是"进程复制器"**
- LitServer 实例化与启动：`litserve_worker.py:1347-1364`，`ls.LitServer(api, accelerator, devices, workers_per_device, timeout=False)` + `server.run(port)`。唯一作用是按 `devices × workers_per_device` 复制进程并注入 device。
- 内置 MCP 被打桩禁用（`:39-82` DummyMCPServer/DummyMCPConnector）。
- `setup(device)`（`:241-394`）全是调度/运行时初始化而非加载模型：分配 worker 序号（`243-247`）、分配 vLLM API（`250-260`）、设 `CUDA_VISIBLE_DEVICES`（`262-267`）、patch `torch.load`（`296-308`）、实例化 `TaskDB`（`326-334`）、生成 worker_id（`339-341`）、死 worker + stale 恢复（`366-385`）、**启动后台轮询线程 `_worker_loop` 与心跳线程（`387-394`）**。引擎对象全置 None（`345-350`），仅水印引擎在 `354-361` 提前加载。
- `decode_request`（`1279-1280`）仅取 action；`predict`（`1282-1296`）只处理 health/poll，且 poll 在自动循环开启时直接返回 "skipped"（`1285-1287`）→ 默认配置下 HTTP predict 完全不触发推理。
- HTTP 端点(8001) 仅供 `task_scheduler.py` 健康检查（`task_scheduler.py:84-106,222-232`）。

**2. 引擎全部 in-process import 后直接 `engine.parse()`**

| 引擎 | 加载点 | 调用点 |
|---|---|---|
| MinerUPipelineEngine | `litserve_worker.py:661-666` | `:674` |
| PaddleOCR-VL | `:709-712` | `:717` |
| PaddleOCR-VL-VLLM | `:733-738` | `:743` |
| SenseVoice | `:758-761` | `:766-772` |
| Video | `:777-780` | `:785-794` |
| Format engines | 注册 `:129-134`；取 `:575-577,961-964` | `:969` |
| Watermark | `:354-358`(setup) | `:1091` |
| MarkItDown | `:113-115,344` | `:936` |

MinerU 引擎是进程内单例（`mineru_pipeline/engine.py:43-53`），`do_parse` lazy load 在 `:108-129`，`parse()` 入口 `:218`。

**3. 任务认领与推理同进程同线程**
- 轮询循环 `litserve_worker.py:414-450`：claim `:422` `self.task_db.get_next_task(...)` → 调引擎 `:430` `self._process_task(task)`。
- `_process_task`（`:452-616`）：DB 与引擎调用交织——拆分写库 `:490`、引擎路由 `:504-581`、写结果回库 `:587-600`（`update_task_status(... data=json.dumps(...))`，结果作 JSON 字符串塞进 `data` 列）、合并回调 `:603-609`、失败写库 `:613-615`。
- `_worker_loop` 由 setup `:388-389` 起的 daemon 线程跑；`TaskDB` 在同 setup `:334` 创建，同进程。
- claim 原子性：`task_db.py:201-275`（SQLite）/ `:277-324`（Redis），`get_next_task` `:207` 先试 Redis 再回退 SQLite。

**4. GPU/进程模型**
- 进程 = LitServe worker 副本（`:1347-1352`）；`devices` 来自 CLI/env（`:1384-1388`），`workers_per_device` 来自 `MAX_CONCURRENT_TASKS`（默认 1，`:1394-1399`，文件头 `10-11` 注释说明默认强制为 1 防爆显存）。
- CUDA 隔离：`setup(device)` 内设 `CUDA_VISIBLE_DEVICES=gpu_id` + `MINERU_DEVICE_MODE="cuda:0"`（`:262-267`），每 worker 只看一张 GPU，引擎一律 `cuda:0`（`:287,712,737,358`）。
- 引擎单例每进程一份；`_global_worker_counter` 用 spawn Value 跨进程共享（`:235-236`），仅用于分配序号轮询 vLLM API 列表。

**5. 强同机/共享文件系统假设（最关键解耦障碍）**
- `OUTPUT_PATH`：worker 把输出写到 `Path(self.output_dir)/Path(file_path).stem`（`:668-669,714,740,763,782,815,879,933,971`），绝对路径写入 DB `result_path`（`:587-600`）。
- API 提交端把上传文件落盘到本地 `UPLOAD_DIR` 并把本地绝对路径存进 `file_path`（`api_server.py:278-281,339-341`），worker 直接用该路径读源文件（`litserve_worker.py:455`）→ 假设 API、worker 共享同一文件系统。
- API 读结果时按 `result_path` 本地打开目录（`api_server.py:440-492,556-566`）。
- 子任务拆分/合并跨进程靠共享 FS：拆分写 `OUTPUT_PATH/splits/{task_id}`（`:1136-1152`），合并读各子任务 `result_path`、跨目录搬图、删分片（`:1164-1268`）。
- 预处理用本地临时目录 + LibreOffice 子进程（`:992-1016,1039-1063`），假设本机装 LibreOffice/FFmpeg。
- VLLMController 通过本机 Docker socket 启停容器（`:148-153,466-472`），假设 worker 与 vLLM 容器同宿主机。
- 心跳/超时回收依赖 DB 时间戳；死 worker 回收靠 `os.kill(pid,0)` 本机探活（`task_db.py:610-617`）→ 假设 scheduler/worker 同主机。

## 3.3 已有的解耦机制（VLM 已是真·松耦合）

**VLLMController（`litserve_worker.py:142-202`）**：不是推理客户端，是 Docker 容器生命周期管理器。
- `_get_client()` 用 `docker.from_env()`（`:148-156`），要求挂载宿主机 docker socket（`docker-compose.yml:269`）。
- `ensure_service(target, conflict)`（`:158-202`）严格互斥：先 `conflict.stop()` + `sleep(2)`（`:168-176`），再 `target.start()` 轮询 30 次等 running（`:179-193`），只判容器 status 不做 HTTP 健康检查。
- 管理容器硬编码：`tianshu-vllm-paddleocr`、`tianshu-vllm-mineru`（`:462-463`）。
- 触发（`:465-472`）：`paddleocr-vl-vllm` → 启 paddle 关 mineru；`vlm-auto-engine`/`hybrid-auto-engine` → 启 mineru 关 paddle。
- server_url 传递：CLI `--paddleocr-vl-vllm-api-list`/`--mineru-vllm-api-list`（`:1378-1380`）→ `setup()` 按 `global_index % len` 分配（`:250-260`）→ compose 实际值 `http://vllm-paddleocr:30023/v1`、`http://vllm-mineru:30024/v1`（`docker-compose.yml:262-263`）。

**vLLM 推理路径（远程 HTTP）**：
- MinerU：`parse()` 把 backend 改写 `vlm-auto-engine→vlm-http-client`、`hybrid-auto-engine→hybrid-http-client`，设 `server_url = vlm_api_base.replace("/v1","")`（`mineru_pipeline/engine.py:251-259`）；就绪检查轮询 `{base}/v1/models`（`:131-156`）；调 `do_parse(..., backend="vlm-http-client", server_url=...)`（`:330-349`）。
- PaddleOCR-VL-VLLM：`self.vllm_api_base = vllm_api_base or os.getenv("VLLM_API_BASE","http://vllm-paddleocr:30023/v1")`（`engine.py:58`）；`PaddleOCRVL(vl_rec_backend="vllm-server", vl_rec_server_url=...)`（`:128-131`）；健康检查先 `{base}/health` 再 `{base}/v1/models`（`:90-104`）。注意：版面检测等基础模型仍在本进程内。

**模型加载边界对照表**

| 引擎 / backend | 加载位置 | 证据 |
|---|---|---|
| MinerU `pipeline`（do_parse） | 进程内 | `mineru_pipeline/engine.py:108-129`；显存管理 `litserve_worker.py:312-323` |
| MinerU `vlm-/hybrid-auto-engine` | 外部 vLLM（HTTP） | `engine.py:251-263,330-349`；容器 `tianshu-vllm-mineru` |
| PaddleOCR-VL（非 vLLM） | 进程内 | `paddleocr_vl/engine.py:150-160`，`paddle.set_device`(`:154`) |
| PaddleOCR-VL-VLLM | 混合：基础模型进程内 + OCR 走 vLLM | `paddleocr_vl_vllm/engine.py:128-131`；容器 `tianshu-vllm-paddleocr` |
| SenseVoice | 进程内 | `audio_engines/sensevoice_engine.py:160-219`（funasr.AutoModel） |
| Video | 进程内（FFmpeg + 关键帧 OCR 复用 OCR 引擎） | `litserve_worker.py:509-512` |
| YOLO11x + LaMa 去水印 | 进程内 | `litserve_worker.py:354-359`（setup 时初始化） |
| Format/MarkItDown/Office | 进程内（非模型/轻量） | `litserve_worker.py:127-136` |

**部署拓扑**：
- `docker-compose.yml`（生产，唯一定义 vLLM）：redis(可选)、rustfs、init-models、backend(8000)、scheduler、vllm-paddleocr(30023,`restart:unless-stopped` **默认常驻**)、vllm-mineru(30024,`restart:no`+`profiles:["manual"]` **默认不启，由 VLLMController 动态 start**)、worker(8001,挂 docker.sock)、mcp-server(8002)、frontend(80)。两 vLLM 各申请 1 GPU + `gpu-memory-utilization 0.6` → 强制互斥轮换。
- `docker-compose.offline.yml`：**无任何 vLLM 服务**，worker 命令不带 `--*-vllm-api-list`（`:222`）→ 完全跑进程内引擎。
- `docker-compose.dev.yml`：无 vLLM 容器（`:153-156`）。
- `start_all.py`（本地非 Docker）：worker 带 `--paddleocr-vl-vllm-api-list`（`:147-148`）但不自己拉容器，容器须外部 compose 创建。

**配置环境变量**：`VLLM_API_BASE`（默认 `http://vllm-paddleocr:30023/v1`，仅 paddleocr_vl_vllm 读取）；`--paddleocr-vl-vllm-engine-enabled`/`--paddleocr-vl-vllm-api-list`/`--mineru-vllm-api-list`；`VLLM_PADDLE_DEVICE`/`VLLM_MINERU_DEVICE`（默认 0）。vLLM 容器**未定义 compose healthcheck**，就绪靠引擎侧轮询。`API_BASE_URL`（mcp_server/start_all）与 vLLM 无关，是 MCP 反代 API 的地址。

## 3.4 解耦的三道障碍（按硬度）

| 障碍 | 性质 | 现状 | 解法基础是否已存在 |
|---|---|---|---|
| A. 共享文件系统契约 | 🔴 最硬 | `file_path`/`result_path` 是本地绝对路径 | ✅ 已有 RustFS + 交接文档，未全面落地 |
| B. MinerU pipeline 后端进程内驻留 | 🟡 中 | `do_parse` 多模型流水线在 worker 进程内 | ⚠️ MinerU 官方有 server 模式可借 |
| C. 同主机控制面（docker.sock / os.kill 探活 / VLLMController） | 🟡 中 | worker 反向用 docker.sock 接管 vLLM 生命周期 | 需换成编排器/服务发现 |

注：vLLM 当前解耦也不彻底——生命周期反被 worker 用 docker.sock 接管且 GPU 互斥强制单实例轮换（非水平扩展），服务发现写死容器名 URL（无注册中心）。

## 3.5 推荐的目标架构（worker 退化为无 GPU 纯编排器）

```
┌─────────────────────────────────────────────────────────────┐
│  控制面 (CPU-only, 可水平扩展)                                  │
│  API Server + Orchestrator-Worker(纯调度) + Scheduler         │
│  - 认领任务、预处理(office→pdf 可单独成服务)、分片、合并、写结果  │
│  - 不加载任何模型，不需要 GPU                                   │
└───────────────┬─────────────────────────┬────────────────────┘
                │ 对象存储交接(RustFS/S3)    │ 推理调用(HTTP/gRPC, 统一契约)
                ↓                          ↓
┌──────────────────────────┐  ┌─────────────────────────────────┐
│  数据面: RustFS/S3         │  │  推理面 (GPU, 各自独立伸缩)         │
│  源文件 + 中间产物 + 结果   │  │  ① MinerU-Service (pipeline+vlm) │
│  result_path = s3:// URI  │  │  ② PaddleOCR-Service             │
│                           │  │  ③ SenseVoice-Service            │
│                           │  │  ④ Watermark-Service(YOLO+LaMa)  │
└──────────────────────────┘  └─────────────────────────────────┘
```

核心改造点：
1. `file_path`/`result_path` 从本地绝对路径 → 对象存储 URI（解锁一切的钥匙，已有 `rustfs_client.py` + 交接文档作基础）。
2. 每个引擎包成独立推理服务，统一契约 `POST /infer {input_uri, options} → {output_uri / inline_result}`。
3. worker 去 GPU 化：保留调度循环（队列、分片合并、超时回收是强项），`_process_task` 里的 `engine.parse()` 换成 HTTP 调用。
4. VLLMController 退役，容器生命周期交编排器（compose profile / k8s HPA / Ray Serve），worker 不再持有 docker.sock。

## 3.6 各引擎解耦难度与路径

| 引擎 | 当前 | 难度 | 路径 |
|---|---|---|---|
| MinerU vlm/hybrid | ✅ 已 HTTP | 🟢 收尾 | 去 VLLMController，vLLM 交编排器常驻 + 服务发现 |
| PaddleOCR-VL-VLLM | ✅ 已 HTTP | 🟢 收尾 | 同上 |
| MinerU pipeline | 🔴 进程内 | 🟡 中 | 用 MinerU 官方 mineru-api/server 模式单独起服务（最有价值一步） |
| PaddleOCR-VL 非 vLLM | 🔴 进程内 | 🟢 易 | 统一收敛到 vLLM 模式，删进程内分支 |
| SenseVoice | 🔴 进程内 | 🟢 易 | FastAPI/Triton 薄封装（funasr 易封） |
| Video | 🔴 进程内 | 🟡 中 | FFmpeg 抽帧留控制面，关键帧 OCR 复用 OCR 服务 |
| YOLO+LaMa 去水印 | 🔴 进程内 | 🟢 易 | Triton/FastAPI 服务化（纯视觉模型最适合） |
| Office/FASTA/MarkItDown | 进程内(非模型) | — | 留控制面，无需 GPU |

## 3.7 分阶段路线（按 ROI）
- **阶段 0｜先补地基（必须最先）**：`result_path`/`file_path` 全面切对象存储 URI（落地已有 RustFS 交接设计）；给 `engine.parse()` 抽统一 `InferenceClient` 接口（in-process + HTTP 双实现可切换），零行为变化但铺路。
- **阶段 1｜先剥最容易的**：去水印(YOLO+LaMa)、SenseVoice 抽独立服务，跑通"控制面调推理面"端到端链路和契约。
- **阶段 2｜剥核心 MinerU pipeline**：用 MinerU 官方 server 模式起独立服务，worker 改 HTTP 调用（占显存最大、最该独立伸缩，收益最高）。
- **阶段 3｜退役同主机控制面**：删 VLLMController + docker.sock；`os.kill(pid,0)` 探活换成心跳表探活；服务发现用环境变量/注册中心。
- **阶段 4｜worker 彻底去 GPU**：控制面节点不需 GPU 可廉价扩展；GPU 只在推理面按引擎独立伸缩。

## 3.8 权衡与风险
- **收益**：GPU 利用率提升（推理服务多 worker 共享、按引擎独立伸缩）、控制面廉价横向扩展、引擎可独立升级（呼应主题二——MinerU 升级只需更新 MinerU 服务镜像，不动调度层）、故障隔离（一个引擎 OOM 不拖垮整个 worker）。
- **代价**：① 网络往返 + 大文件传输开销（对象存储交接对小文件可能比本地慢）；② 运维复杂度上升（1 个 worker 镜像 → N 个推理服务）；③ MinerU pipeline 服务化要吃透其 server 模式能力边界。
- **最大风险 = 零测试**：跨进程拆解极易引入"结果一致性漂移"，没有 golden-file 回归测试 = 盲拆。解耦前先为现有 in-process 输出建立黄金样本作对比基线。
- **务实建议**：若当前部署是单机几张卡，不必一步到位拆微服务——阶段 0+1（对象存储交接 + InferenceClient 抽象 + 剥 1-2 个简单引擎）就能拿到大部分架构收益且风险可控。全面微服务化只有在多机/多卡、需按引擎独立伸缩时才划算。

---

# 任务安排建议（整合三大主题）

按"先止血、再兜底、后重构"分波次。每项标注来源主题与证据锚点。

## 第一波：止血（数天，安全 + 数据正确性 P0）
| 任务 | 来源 | 锚点 |
|---|---|---|
| 注册接口强制 `role=USER` + 接 `allow_registration` 开关 | 主题一·安全 #1/#2 | `auth/routes.py:41-50`、`models.py:105-112` |
| 文件服务接口加鉴权 + 归属校验；MCP 加认证 | 主题一·安全 #4 | `api_server.py:1131-1182`、`mcp_server.py:705` |
| 前端引入 DOMPurify 净化 markdown | 主题一·前端 #1 | `MarkdownViewer.vue:12,50` |
| CORS 改白名单（用 `ALLOWED_ORIGINS`） | 主题一·安全 #3 | `api_server.py:83-89` |
| SQLite 启用 `PRAGMA journal_mode=WAL` + `busy_timeout`（一行高收益） | 主题一·调度 #2 | `task_db.py:65-77` |

## 第二波：可靠性兜底（1-2 周）
| 任务 | 来源 | 锚点 |
|---|---|---|
| Redis/SQLite 一致性对账（reconciler）或队列空回退扫 SQLite | 主题一·调度 #1/#5 | `task_db.py:166-186` |
| 父任务合并原子互斥（认领式状态翻转）+ 合并期心跳续租 | 主题一·调度 #3/#4 | `litserve_worker.py:602-609,1164-1268` |
| GPU 自动休眠监控加锁（修 use-after-free） | 主题一·引擎 S1 | `mineru_pipeline/engine.py:89-106` |
| VLLMController 跨进程文件锁 + 显存释放轮询 | 主题一·引擎 S2 | `litserve_worker.py:142-202` |
| 格式引擎注册表改存类/无状态 parse | 主题一·引擎 S3 | `format_engines/base.py:88-104` |
| **建测试基础设施**：先覆盖 `output_normalizer`/`format_engines`（纯函数）+ `task_db.get_next_task` 并发认领 | 主题一·测试 #1/#2 | — |
| CI 接入 pytest + 前端 `npm run build` + pip-audit/npm audit | 主题一·CI | `.github/workflows/pylint.yml` |
| 不可信输入加大小上限（FASTA/GenBank/zip/FFmpeg timeout） | 主题一·引擎 S4/M9 | `fasta_engine.py:254-283` 等 |
| 修"报告成功实则降级"逻辑（去水印空操作、vLLM 健康检查、合并丢页） | 主题一·引擎 M1/M4、调度 #7 | `pdf_watermark_handler.py:164-172` 等 |
| **MinerU 升级 3.3.1**：先建 golden-file 回归 → 钉版本 → 显式传 `effort` → 决策 2509/2605 模型 → 复核 fast-langdetect/vLLM 约束 | 主题二 全部 | `engine.py:330`、~10 处模型名引用 |

## 第三波：架构演进与技术债（中长期）
| 任务 | 来源 | 锚点 |
|---|---|---|
| **推理解耦阶段 0**：result_path/file_path 切对象存储 URI + 抽 `InferenceClient` 统一接口 | 主题三·3.7 | `api_server.py:278-281`、`litserve_worker.py:587-600` |
| **推理解耦阶段 1-2**：剥离去水印/SenseVoice → 剥离 MinerU pipeline（官方 server 模式） | 主题三·3.6/3.7 | — |
| **推理解耦阶段 3-4**：退役 VLLMController/docker.sock，心跳表探活，worker 去 GPU | 主题三·3.7 | `litserve_worker.py:142-202`、`task_db.py:609-623` |
| 资源泄漏统一 try/finally；去水印显存释放 | 主题一·引擎 M2/M3/M10 | `pdf_utils.py:46-202` 等 |
| 统一三份 Dockerfile 与文档 CUDA 版本；主 compose 补 healthcheck | 主题一·工程化 | `Dockerfile` vs `Dockerfile.offline` vs `CLAUDE.md` |
| 依赖锁定（lock 文件 + 7 个无约束包补下限） | 主题一·依赖 | `requirements.txt` |
| 巨型函数拆分（`litserve_worker.py`/`api_server.py`）；前端抽 composable 收敛 any/魔法字符串；重命名 `perse_uitls.py` | 主题一·多处 | — |
| 补可观测性（/metrics、结构化日志、清理 43 处 print） | 主题一·工程化 | — |

## 贯穿性主线
**"零测试"是横跨三个主题的最大杠杆点**：它放大了并发 bug（主题一）、使 MinerU 升级无法安全验证（主题二）、让推理解耦变成盲拆（主题三）。因此第二波的"建测试基础设施"不是可选项，而是后续所有改造的前置条件。

---

*报告完。三大主题的子分析 agent 完整原始报告均可按需展开任一维度的全部细节。*
