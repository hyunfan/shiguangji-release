# 维护说明

本地服务仍使用 Python 标准库，浏览器端使用原生 JavaScript。基本功能没有新增运行依赖；分段视频合并使用可选 FFmpeg/FFprobe 组件。

## 模块边界

| 模块 | 职责 |
|---|---|
| `app.py` | HTTP 参数、安全边界、响应与后台启动 |
| `task_service.py` | 每个资料库独立的任务状态、取消信号、自动续批、保存、打包与队列衔接 |
| `job_dispatch.py` / `task_queue.py` | 队列派发与原子持久化；准备失败和运行结束分别处理 |
| `task_history.py` / `static/history.js` | 独立 SQLite 任务历史、旧数据补录、筛选分页与单条重试界面 |
| `task_status.py` / `task_compat.py` | 当前结果展示；旧状态记录兼容推断 |
| `platform_accounts.py` / `platform_registry.py` | 输入校验、平台行为、账号信息；保留现有合集键 |
| `archive.py` / `instagram_archive.py` | X / Instagram 采集编排；平台内容归属规则保持独立 |
| `archive_http.py` / `source_http.py` | Wayback / 公开来源的请求、跳转校验、超时、预算与取消 |
| `archive_support.py` / `core_errors.py` | 无采集器依赖的公共函数、错误类型和错误分类 |
| `media_download.py` / `media_files.py` | 流式下载、结构检查、摘要检查、原子发布、缓存验证 |
| `media_inventory.py` | 只读按帖汇总明确视频 ID、封面、文件摘要重复记录；不更改原记录、下载状态或恢复调度 |
| `media_recovery.py` / 各来源模块 | 多源查询与逐项证据；共用保存规则 |
| `search_plan.py` | 保存 Common Crawl 查询顺序、优先/扩大范围阶段及完成原因 |
| `collection_store.py` | 原有 SQLite 格式的读写与兼容初始化 |
| `collection_reader.py` | 摘要、内容版本缓存和在线帖子分页 |
| `collection_export.py` | 离线页面、CSV/JSONL、导出校验清单及 ZIP |
| `retry_policy.py` | 保存有界的 URL 摘要和来源冷却时间；完整读取响应后才清除失败记录 |
| `supervisor.py` / `reliability.py` | 按需守护、有限崩溃恢复、明确恢复开关及只读进展诊断 |
| `storage_guard.py` / `backup_library.py` | 空间预留、日志轮换、SQLite 在线快照和摘要校验的增量备份、新目录恢复 |
| `cloud_backup.py` | 以本地备份索引为持久待办队列，独立子进程向同步目录、挂载盘或外部目录追加副本；本地交付凭据、目标标识核对、超时隔离和退避重试，不将目录读回当作网盘上传完成 |
| `storage_policy.py` / `storage_service.py` | 本地空间配置、后台预览和确认清理；只清理可重建的下载 ZIP，清理前重新核对清单和原文件内容 |
| `cloud_media.py` | 网盘试读恢复包、完整摘要验证、容量受限缓存和使用中保护；所有挂载盘操作在有超时的子进程中完成 |

`archive.py` 保留旧公共名称的导出，方便已有调用者逐步迁移。核心网络和存储模块不再反向导入采集器。

## 状态与数据约束

媒体汇总 `media_inventory.schema=1` 与原始 `counts` 并存，后者仍用于续批和队列进度。`collection_reader` 摘要缓存升级为 schema 2，旧缓存只读重算，不改写用户合集；导出时将同一汇总写入 manifest 和离线数据。视频按帖子内明确 ID 分组，缺少身份或身份冲突时各自保留；封面与 HLS 不能单独使视频组计为已保存。摘要及大小仅用于显示去重，不证明文件当前可播放。新提取的历史 JSON 和公开元数据共用 `media_belongs_to_post`，但不对已合并的旧记录实施猜测式删除。

- 队列准备失败用 `fail_start`；已启动的任务凭 `run_id` 结束。旧回调不能结束其他运行，也不能覆盖已经结束的项目。
- 调度遇到存储错误立即返回。一次派发不能重复处理未推进状态的同一项目。
- “任务结束”“当前范围查完”“已发现内容恢复程度”分别保存和显示。兼容旧记录不能仅凭同名账号认定同一次运行。
- 来源错误用异常中的状态码和明确原因分类，不从文字中的 404/403 猜测结果。
- 成功媒体经过临时文件、格式/容器检查、大小检查、摘要与原子发布。结构校验不能替代逐帧解码，也不保证浏览器支持视频编码。
- 复用本地媒体时重新读取内容，结构与摘要检查使用同一文件句柄，结束时核对文件身份。大小和时间相同不能代替内容校验；已保存成功结果不再跨调用缓存。SHA-256 按 1 MiB 分块读取并检查暂停信号。页面轮询仍只读清单/摘要，不读取媒体内容。
- `TaskService` 实例拥有资料库路径、任务字典、取消/退出信号、锁和队列。采集器工厂与参数校验由入口传入，服务不导入 HTTP 模块。回调绑定运行编号，已结束或其他运行的进度不能覆盖当前状态。
- 工作线程按“保存结果 → 按设置打包 → 备份 → 释放防休眠 → 写入队列结果 → 启动下一项”的顺序结束。默认只在显式请求下载包时打包；队列保存出错时停止自动推进。Instagram 在取得合集锁之后才初始化数据库，最终保存和关闭数据库也在锁内。
- Common Crawl 保留旧媒体早于帖子发布的可能性；计划有优先范围和扩大范围两个阶段。原有批次/请求/无进展停止限制继续生效，扩大范围不表示必然有结果。

## 读取与导出

`collections/.task-history.sqlite` 独立于合集数据库和队列保留上限。网页任务在启动线程前记录开始，释放防休眠后记录结果；代理、令牌和内部运行字段不写入历史选项。旧记录只做一次有证据的补录。`GET /api/history` 支持 `q/state/platform/offset/limit`，`POST /api/history/retry` 沿用本地令牌和同源保护；重试前校验空闲状态、合集锁及目录，继续单项不恢复整个暂停队列。直接命令行采集不经过此服务，不记录新历史。

`manifest.json` 继续是可携带的完整清单。`summary.json` 是可重建的本地摘要，带对应清单的文件版本和内容摘要。

自动续批由任务服务内部设置 `_defer_offline_export`，采集器的批末检查点仅发布数据库、完整清单和摘要。任务服务在暂停或本轮结束时统一生成完整离线文件；ZIP 由 `zip_on_demand` 控制，显式导出始终生成。单批或直接调用采集器保持完整离线导出。此内部标记不改变恢复进度或媒体校验。`page_progress` 表示 X 合集累计已成功解析的快照；`stage_seconds` 使用单调时钟累计本次运行的阶段耗时，新任务会重置，重启不会自动恢复执行。

旧合集首次读取会解析完整清单，此后使用内存摘要缓存；新导出合集可直接读取磁盘摘要。内容更改、同名文件替换或摘要不匹配都会失效。兼容读取不修改旧文件。

- `/api/collection/<key>?view=summary`：统计、月份、状态、版本，不含帖子正文。
- `/api/posts/<key>`：`offset`、`limit`（最多 200）、`search`、`type`、`month`、`sort`、`revision`。版本不匹配返回 `stale`，前端重载，避免两代页面拼接。
- 不带 `view` 的详情接口保留，兼容旧客户端。
- 在线默认每页 40 条；筛选针对整份合集。图片灯箱只浏览当前已载入页中的图片；离线版仍可一次浏览全部筛选图片。
- 运行中的检查点只更新清单与摘要；暂停、完成、失败收尾和打包时同步完整离线版。工作期间打开离线链接看到的是上次完整保存结果。
- `generation.json` 第 2 版记录固定导出文件和每条帖子 TXT 的 SHA-256；ZIP 在同一合集锁内同步并打包。`posts/` 为程序生成文本区，只收录本次清单列出的路径，旧日期路径保留在磁盘但不进入新 ZIP；不删除媒体、原始快照或用户文件。ZIP 的侧车记录同时绑定清单摘要与 ZIP 文件状态，旧包不会冒充新包。

## 开发、发布与回退

Python 3.10+；完整测试还需要 Node.js。应用运行本身不需要 Node.js。

```powershell
python -X utf8 run_tests.py
python build_release.py
```

Python 测试和独立样例在 `tests/python/`；前端 DOM 行为测试在 `tests/frontend/`。旧 `test_*.py` 的测试名称保持不变。诊断脚本保留在 `diagnostics/`，不进入发布包。

`app_version.py` 是应用版本的唯一来源。`release_files.json` 明确列出发布文件；构建包含 `release.json` 的逐文件摘要，排除合集、运行状态、日志、测试与诊断数据。

Windows x64 便携包使用 `python build_portable.py` 构建；首次下载 `python-runtime.json` 固定的官方嵌入式 ZIP，核对 SHA256 后缓存到 `diagnostics/runtime-cache`。离线构建可传 `--runtime-zip 路径`。构建保留所有官方组件及许可，仅将 `python314._pth` 改为标准库、运行环境目录和应用根目录，不加载系统 PYTHONPATH、用户包或 site。运行文件放在 `python/`，与可写日志 `runtime/` 分开。根目录 `portable.json` 是来源记录和完整解压标志；缺失运行环境时启动脚本明确失败。便携包另命名为 `shiguangji-版本-windows-x64-portable.zip`，不会替换源码发布包。

在 Windows 上执行 `python verify_portable.py diagnostics/releases/shiguangji-1.28.1-windows-x64-portable.zip` 进行实际启动验证。脚本先核对发布文件，再解压到临时中文带空格目录，移除 PATH 中的系统 Python，并设置无效 PYTHONHOME/PYTHONPATH。检查模块、SQLite 持久化、Windows 证书库、临时快捷方式读回、真实 HTTP 页面、包内 pythonw 后台、重复启动复用与正常停止/重启。测试只用临时数据和临时快捷方式，不注册用户桌面或协议，也不采集真实账号。此验证不等于在另一台全新电脑或 Windows 10 上实测。

```powershell
python deploy_release.py diagnostics/releases/shiguangji-1.28.1.zip --target "C:\实际安装目录" --backup-root diagnostics/deploy-backups --port 8766
```

升级前检查服务归属和任务空闲；保存程序文件备份；温和停止旧后台，校验更新文件，按升级前状态恢复后台；失败时回退程序文件。不建立开机自启动，不覆盖 `collections/`。

1.27 起，`compressed_files.py` 提供逻辑文件读取，原始网页可存为 `.txt.gz`，备份对象可存为 `<sha256>.gz`。索引始终记录解压后的内容摘要和大小；原有时间戳保留。`compact_library.py` 逐文件压缩、解压校验、最终路径读回，再释放对应的普通副本；进度保存在资料库内，失败项保留原件且可重试。新采集及新增备份对象自动压缩。媒体原件不进入压缩流程。网盘包和下载 ZIP 输出兼容旧版本的普通文件。

数据压缩完成后，**回退到 1.26 或更早版本前必须先解压数据**：停止归档任务，在 1.27 环境运行 `python compact_library.py --base "实际合集目录" --expand`，确认结果无失败后再回退程序。解压会重新占用压缩所节省的空间。迁移和反向迁移都不删除合集内容或修改备份索引。

备份 ZIP 中 `rollback-files.json` 列出本次新增程序文件；手动回退应先停止后台，恢复备份代码，移除这些明确列出的新增文件，然后正常启动。不要还原或删除合集目录。

1.19 没有改变原 SQLite 表结构、X 目录名或 `instagram--` 目录名，因此不要求全库迁移。旧名检索、存储方法和前端状态仍按后续需要渐进整理，不在本轮重写。

## 1.25 恢复调度

`post_recovery.py` 对已有 X 帖子执行有界正文补取，复用公开来源身份检查并保存 provenance；`recovery_report.py` 只按快照实际状态分类缺失原因。`hls_recovery.py` 负责同视频 HTTPS 分片约束、摘要缓存、全量分片下载、本地合并/解码与原子发布，禁止 FFmpeg 联网，不处理加密内容。中断时不得把部分视频计为恢复成功。

`AutoBatches` 将无请求的来源冷却与无进展区分。`TaskQueue` 持久化 next_retry_at / retry_rounds / retry_started，最多 3 轮、48 小时，原有模式保留；等待项让出队列。`TaskService` 在后台空闲时通过可取消定时器检查到期任务，调用原派发路径。暂停/关闭/重启不擅自恢复；定时器启动错误使队列暂停并保留原因。会话增量与累计请求写入 automatic，而原 counts 继续表示合集存量。
