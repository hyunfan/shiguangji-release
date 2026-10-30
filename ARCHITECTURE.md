# 维护说明

本地服务仍使用 Python 标准库，浏览器端使用原生 JavaScript。基本功能没有新增运行依赖；分段视频合并使用可选 FFmpeg/FFprobe 组件。

## 代码布局

根目录只保留需要按文件路径启动的入口脚本和维护工具；其余应用模块按职责放在 `shiguangji/` 包内。入口脚本、`.cmd` 启动脚本、`static/` 与数据目录位置不变。

| 位置 | 内容 |
|---|---|
| 根目录 | 入口：`app.py`（HTTP 服务）、`launcher.py`、`supervisor.py`、`windows_integration.py`、`desktop_launcher.pyw`、`setup_ffmpeg.py`；维护工具：`build_release.py`、`build_portable.py`、`check_publication.py`、`audit_publication.py`、`deploy_release.py`、`verify_portable.py`、`run_tests.py`、`audit_collection.py` |
| `shiguangji/core/` | 无采集器依赖的公共基础：版本号、原子写入、错误类型、存档网址还原（`archive_urls.original_url`）、公共函数 |
| `shiguangji/platforms/` | 平台与账号：输入校验、平台注册、账号身份、X / Instagram 内容解析 |
| `shiguangji/collect/` | 采集编排：X / Instagram 采集、旧名发现（`identity_discovery`）、本地旧名合集复用（`local_aliases`）、正文补取、运行预算 |
| `shiguangji/sources/` | 数据来源：Wayback / 公开来源请求、重试冷却、各来源查询与 Common Crawl 查询计划 |
| `shiguangji/media/` | 媒体：下载、文件校验、媒体汇总、多源恢复、HLS 合并 |
| `shiguangji/storage/` | 存储：合集数据库、读取、导出（含缺失原因报告）、锁、删除、压缩、空间策略、备份、网盘副本与试读 |
| `shiguangji/tasks/` | 任务：任务服务、队列与派发、状态与兼容、任务历史、自动续批 |
| `shiguangji/web/` | 本地 HTTP 接口：请求处理与错误映射（handler）、GET/POST 路由表（routes_get / routes_post）、静态与 Range 文件（files）、采集参数（options）、Host/Origin/令牌校验（security） |
| `shiguangji/system/` | 系统与可靠性：防休眠、守护设置与诊断、Windows Shell Link 接口 |

包内模块统一使用绝对导入（`from shiguangji.<层> import 模块` 或 `from shiguangji.<层>.模块 import 名称`）。需要应用根目录的模块使用 `shiguangji.APP_ROOT`，不要用 `Path(__file__).parent` 推算。`cloud_backup.py`、`cloud_media.py` 以自身文件路径启动子进程，`backup_library.py`、`compact_library.py` 可按文件路径运行；这几个模块在以脚本方式运行时会把应用根目录加入 `sys.path`。新增模块时同步更新 `release_files.json`，`tests/python/test_package_layout.py` 会检查根目录与白名单。

### 依赖方向

包之间只允许向下依赖：`core` → `platforms`（纯解析）→ `sources` / `media` / `storage`（同层可互相引用）→ `collect` → `tasks` → `web` → 根目录入口（`app.py` 等）。`system` 只依赖 `core`，可被任意层使用；包内模块不导入根目录脚本。`tests/python/test_package_layout.py` 用 `ast` 扫描全部 import（包括函数内的延迟导入）守护这一规则；暂时无法安全修复的已知违规列在该测试的 `KNOWN_VIOLATIONS` 中并注明原因，该名单只能减少，新增违规或已修复但未移除的条目都会让测试失败。

## 模块边界

下表路径均相对仓库根目录。

| 模块 | 职责 |
|---|---|
| `app.py` | 入口与组装：命令行、单实例与后台启动；持有 `TASKS`、`CLOUD`、`STORAGE` 等单例。路由在请求时通过本模块读取这些对象，测试仍可直接替换 `app.*` |
| `shiguangji/web/` | HTTP 安全边界（Host、Origin、`X-Local-Token`）、请求体限制、路由表分派与错误码映射；不导入根目录脚本 |
| `shiguangji/tasks/task_service.py` | 门面：每个资料库独立的任务状态、取消信号、进度、后台线程、保存与打包；以下三个混入类的方法都挂在 `TaskService` 上，对外接口不变 |
| `shiguangji/tasks/queue_coordinator.py` | `QueueCoordinator`：队列派发、自动重试计时、崩溃后续跑、任务历史检索与单条重试 |
| `shiguangji/tasks/batch_runner.py` | `BatchRunner`：单次或自动多批采集循环与续批判断 |
| `shiguangji/tasks/result_finalizer.py` | `ResultFinalizer`：结束原因解读、离线导出重建、失败记录写入 manifest |
| `shiguangji/tasks/job_dispatch.py` / `shiguangji/tasks/task_queue.py` | 队列派发与原子持久化；准备失败和运行结束分别处理 |
| `shiguangji/tasks/task_history.py` / `static/history.js` | 独立 SQLite 任务历史、旧数据补录、筛选分页与单条重试界面 |
| `shiguangji/tasks/task_status.py` / `shiguangji/tasks/task_compat.py` | 当前结果展示；旧状态记录兼容推断 |
| `shiguangji/platforms/platform_accounts.py` / `shiguangji/platforms/platform_registry.py` | 输入校验、平台行为、账号信息；保留现有合集键 |
| `windows_integration.py` / `shiguangji/system/windows_shortcuts.py` | 桌面入口与归属核对；通过 Windows Unicode Shell Link 接口创建、加载和读回快捷方式，不执行其目标 |
| `shiguangji/collect/archive.py` / `shiguangji/collect/instagram_archive.py` | X / Instagram 采集编排；平台内容归属规则保持独立 |
| `shiguangji/sources/archive_http.py` / `shiguangji/sources/source_http.py` | Wayback / 公开来源的请求、跳转校验、超时、预算与取消 |
| `shiguangji/core/archive_support.py` / `shiguangji/core/core_errors.py` | 无采集器依赖的公共函数、错误类型和错误分类 |
| `shiguangji/media/media_download.py` / `shiguangji/media/media_files.py` | 流式下载、结构检查、摘要检查、原子发布、缓存验证 |
| `shiguangji/media/media_inventory.py` | 只读按帖汇总明确视频 ID、封面、文件摘要重复记录；不更改原记录、下载状态或恢复调度 |
| `shiguangji/media/media_recovery.py` / 各来源模块 | 多源查询与逐项证据；共用保存规则 |
| `shiguangji/sources/search_plan.py` | 保存 Common Crawl 查询顺序、优先/扩大范围阶段及完成原因 |
| `shiguangji/storage/collection_store.py` | 原有 SQLite 格式的读写与兼容初始化 |
| `shiguangji/storage/collection_reader.py` | 摘要、内容版本缓存和在线帖子分页 |
| `shiguangji/storage/collection_export.py` | 离线页面、CSV/JSONL、导出校验清单及 ZIP |
| `shiguangji/sources/retry_policy.py` | 保存有界的 URL 摘要和来源冷却时间；完整读取响应后才清除失败记录 |
| `supervisor.py` / `shiguangji/system/reliability.py` | 按需守护、有限崩溃恢复、明确恢复开关及只读进展诊断 |
| `shiguangji/storage/storage_guard.py` / `shiguangji/storage/backup_library.py` | 空间预留、日志轮换、SQLite 在线快照和摘要校验的增量备份、新目录恢复 |
| `shiguangji/storage/cloud_backup.py` | 以本地备份索引为持久待办队列，独立子进程向同步目录、挂载盘或外部目录追加副本；本地交付凭据、目标标识核对、超时隔离和退避重试，不将目录读回当作网盘上传完成 |
| `shiguangji/storage/storage_policy.py` / `shiguangji/storage/storage_service.py` | 本地空间配置、后台预览和确认清理；只清理可重建的下载 ZIP，清理前重新核对清单和原文件内容 |
| `shiguangji/storage/cloud_media.py` | 网盘试读恢复包、完整摘要验证、容量受限缓存和使用中保护；所有挂载盘操作在有超时的子进程中完成 |

`shiguangji/collect/archive.py` 保留旧公共名称的导出，方便已有调用者逐步迁移。核心网络和存储模块不再反向导入采集器。`shiguangji/platforms/extractor.py` 继续导出 `original_url`（实现位于 `shiguangji/core/archive_urls.py`），已有调用者无需修改。

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

`check_publication.py` 将 Git 已跟踪的当前文件复制到临时源码目录，拒绝私有数据路径、链接和越界路径，再扫描源码并构建白名单发布包。验证同时比较包内清单、实际字节和导出源码；便携包额外与固定官方运行时核对。配置文件（含 YAML、TOML、INI 和 `.env.example`）也参加隐私扫描；测试凭据仅豁免指定文件中两条完整虚构网址的摘要。检查规则不能证明没有其他隐私，远程历史及附件仍须单独审查。

`.github/workflows/checks.yml` 使用只读仓库权限和固定提交摘要的 GitHub Actions。Windows 上分别运行 Python 3.10 / 3.14 及前端测试，通过后运行 `check_publication.py --portable --smoke`。工作流只使用合成数据、临时目录和固定官方 Python 下载，不读取真实合集，也不自动发布或升级安装目录。该检查入口、工作流和问题模板属于源码维护工具，不进入程序发布白名单。

`shiguangji/core/app_version.py` 是应用版本的唯一来源。`release_files.json` 明确列出发布文件；构建包含 `release.json` 的逐文件摘要，排除合集、运行状态、日志、测试与诊断数据。

Windows x64 便携包使用 `python build_portable.py` 构建；首次下载 `python-runtime.json` 固定的官方嵌入式 ZIP，核对 SHA256 后缓存到 `diagnostics/runtime-cache`。离线构建可传 `--runtime-zip 路径`。构建保留所有官方组件及许可，仅将 `python314._pth` 改为标准库、运行环境目录和应用根目录，不加载系统 PYTHONPATH、用户包或 site。运行文件放在 `python/`，与可写日志 `runtime/` 分开。根目录 `portable.json` 是来源记录和完整解压标志；缺失运行环境时启动脚本明确失败。便携包另命名为 `shiguangji-版本-windows-x64-portable.zip`，不会替换源码发布包。

在 Windows 上执行 `python verify_portable.py diagnostics/releases/shiguangji-1.28.2-windows-x64-portable.zip` 进行实际启动验证。脚本先核对发布文件，再解压到临时中文带空格目录，移除 PATH 中的系统 Python，并设置无效 PYTHONHOME/PYTHONPATH。检查模块、SQLite 持久化、Windows 证书库、临时快捷方式读回、真实 HTTP 页面、包内 pythonw 后台、重复启动复用与正常停止/重启。测试只用临时数据和临时快捷方式，不注册用户桌面或协议，也不采集真实账号。此验证不等于在另一台全新电脑或 Windows 10 上实测。

```powershell
python deploy_release.py diagnostics/releases/shiguangji-1.28.2.zip --target "C:\实际安装目录" --backup-root diagnostics/deploy-backups --port 8766
```

升级时按安装目录中上一版的 `release.json` 找出新版不再包含的程序文件（仅限根目录 `.py`/`.pyw` 以及 `shiguangji/`、`static/` 下的代码与页面文件），先写入备份，待新文件全部校验通过后再删除；`collections/`、`runtime/`、`diagnostics/`、`tools/`、`python/`、FFmpeg、配置、文档和 `.cmd` 永不在清理范围内。没有 `release.json` 的老安装只按 `deploy_release.LEGACY_ROOT_MODULES`（1.28.2 及更早版本平铺在根目录的 52 个模块名）识别旧模块，且仅当新版已包含 `shiguangji/` 包时才清理。新增文件与删除的旧文件分别记入备份中 `rollback-files.json` 的 `added` 与 `removed`，升级失败时自动恢复被删除的文件。升级前检查服务归属和任务空闲；保存程序文件备份；温和停止旧后台，校验更新文件，按升级前状态恢复后台；失败时回退程序文件。不建立开机自启动，不覆盖 `collections/`。

1.27 起，`shiguangji/storage/compressed_files.py` 提供逻辑文件读取，原始网页可存为 `.txt.gz`，备份对象可存为 `<sha256>.gz`。索引始终记录解压后的内容摘要和大小；原有时间戳保留。`shiguangji/storage/compact_library.py` 逐文件压缩、解压校验、最终路径读回，再释放对应的普通副本；进度保存在资料库内，失败项保留原件且可重试。新采集及新增备份对象自动压缩。媒体原件不进入压缩流程。网盘包和下载 ZIP 输出兼容旧版本的普通文件。

数据压缩完成后，**回退到 1.26 或更早版本前必须先解压数据**：停止归档任务，在当前版本运行 `python shiguangji/storage/compact_library.py --base "实际合集目录" --expand`（1.27～1.28.2 中该脚本位于根目录 `compact_library.py`），确认结果无失败后再回退程序。解压会重新占用压缩所节省的空间。迁移和反向迁移都不删除合集内容或修改备份索引。

备份 ZIP 中 `rollback-files.json` 的 `added` 列出本次新增程序文件，`removed` 列出本次删除的旧程序文件（其内容已在备份中）；手动回退应先停止后台，恢复备份中的全部代码（包括 `removed` 中的文件），移除 `added` 中明确列出的新增文件，然后正常启动。不要还原或删除合集目录。

1.19 没有改变原 SQLite 表结构、X 目录名或 `instagram--` 目录名，因此不要求全库迁移。旧名检索、存储方法和前端状态仍按后续需要渐进整理，不在本轮重写。

## 1.25 恢复调度

`shiguangji/collect/post_recovery.py` 对已有 X 帖子执行有界正文补取，复用公开来源身份检查并保存 provenance；`shiguangji/storage/recovery_report.py`（导出时调用的纯函数）只按快照实际状态分类缺失原因。`shiguangji/media/hls_recovery.py` 负责同视频 HTTPS 分片约束、摘要缓存、全量分片下载、本地合并/解码与原子发布，禁止 FFmpeg 联网，不处理加密内容。中断时不得把部分视频计为恢复成功。

`AutoBatches` 将无请求的来源冷却与无进展区分。`TaskQueue` 持久化 next_retry_at / retry_rounds / retry_started，最多 3 轮、48 小时，原有模式保留；等待项让出队列。`TaskService` 在后台空闲时通过可取消定时器检查到期任务，调用原派发路径。暂停/关闭/重启不擅自恢复；定时器启动错误使队列暂停并保留原因。会话增量与累计请求写入 automatic，而原 counts 继续表示合集存量。
