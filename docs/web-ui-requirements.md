# LinkChecker Gradio Web UI — 需求规格说明书

> 经过 3 轮架构校验和优化后的最终版本

## 1. 项目概述

### 1.1 目标
为 LinkChecker 添加一个本地 Gradio Web 界面，通过 `linkchecker --web` 启动，在浏览器中提供完整的链接检查体验。纯 Python 实现，零前端代码。

### 1.2 技术栈
- **UI 框架**: Gradio >= 4.0（Blocks API）
- **图表**: matplotlib（Gradio 内置支持，无额外依赖）
- **数据库**: SQLite（WAL 模式，历史记录存储）
- **安装方式**: `pip install linkchecker[web]`（可选依赖，不影响 CLI 用户）

### 1.3 设计原则
- **最小侵入性**: 不修改 LinkChecker 核心引擎代码，仅通过现有 Logger/Configuration 接口集成
- **复用优先**: 复用 `Configuration`、`Director`、`Aggregate`、`_Logger` 等现有组件
- **独立部署**: Web 模块完全自包含在 `linkcheck/web/` 下

---

## 2. 文件结构

```
linkcheck/web/
  __init__.py           # 包初始化
  gradio_app.py         # Gradio 界面定义 + 事件绑定（主文件）
  gradio_logger.py      # GradioLogger(_Logger) — 实时结果桥接
  check_runner.py       # CheckRunner — 封装 Director 调用
  history_store.py      # HistoryStore — SQLite 历史记录
  export_utils.py       # 导出工具（CSV/HTML）

tests/web/
  __init__.py
  test_check_runner.py
  test_gradio_logger.py
  test_history_store.py
  test_export_utils.py
```

**新建文件**: 6 个源文件 + 4 个测试文件
**修改文件**: 3 个（arg_parser.py, linkchecker.py, pyproject.toml）

---

## 3. 核心组件规格

### 3.1 GradioLogger（`linkcheck/web/gradio_logger.py`）

**继承**: `linkcheck.logger._Logger`（`linkcheck/logger/__init__.py`）

**关键设计决策**（经第 1/2 轮校验确认）：

| 决策 | 选择 | 原因 |
|------|------|------|
| LoggerName | `"gradio"`（非 None） | None 会导致 `_get_loggers()` 和 `Configuration.__init__` 中出错 |
| 注册方式 | 追加到 `config["fileoutput"]` | 不替换主 logger，保留控制台输出用于调试 |
| 数据获取 | 重写 `log_filter_url()` | 默认的 `log_filter_url()` 仅在 `do_print=True` 时调用 `log_url()`（仅无效/有警告的 URL），Web UI 需要看到所有 URL |

**必须实现的方法**:

```python
class GradioLogger(_Logger):
    LoggerName = "gradio"
    LoggerArgs = {}

    def __init__(self, results_list, **kwargs):
        """
        Args:
            results_list: 线程安全的共享列表，checker 线程 append，Gradio 主线程 read
        """

    def start_output(self):
        """必须调用 super().start_output() 以初始化 stats 和 starttime"""

    def log_filter_url(self, url_data, do_print):
        """重写：始终调用 log_url()，忽略 do_print 参数。
        这样 Web UI 能看到所有检查过的 URL，而不仅是无效的。
        仍需调用 self.stats.log_url(url_data, do_print) 以维护统计。"""

    def log_url(self, url_data):
        """url_data 是 CompactUrlData 对象（__slots__），属性:
        - url, parent_url, result, valid (bool), warnings (list of tuples),
        - info (list), checktime (float), size (int), content_type, level, domain,
        - line, column, modified (datetime|None), extern (int)
        序列化为 dict 并 append 到 results_list。"""

    def end_output(self, **kwargs):
        """kwargs 含: downloaded_bytes (int), num_urls (int), interrupt (bool)"""
```

**数据流**:
```
Checker Thread → director/logger.py:Logger.log_url() [@synchronized]
  → GradioLogger.log_filter_url() → GradioLogger.log_url()
    → results_list.append(dict)  [list.append 是 GIL 保护的原子操作]
      → Gradio 主线程轮询 results_list [每 0.5 秒]
```

### 3.2 CheckRunner（`linkcheck/web/check_runner.py`）

**封装 LinkChecker 核心引擎调用链**：

```python
class CheckRunner:
    def __init__(self):
        self.aggregate = None       # 当前 Aggregate（用于取消）
        self.is_running = False     # 防止并发检查

    def run_check(self, urls, config_overrides, results_list):
        """在当前线程中执行检查（由 Gradio 在后台线程调用）。

        调用链（复用现有函数）：
        1. config = Configuration()                          # linkcheck/configuration/__init__.py
        2. config.set_status_logger(console.StatusLogger())  # 必需，linkcheck/director/console.py
        3. 应用 config_overrides（threads, timeout, recursionlevel, checkextern）
        4. gradio_logger = GradioLogger(results_list)
        5. config["fileoutput"].append(gradio_logger)        # 追加到 fileoutput 列表
        6. config.sanitize()                                 # 创建默认主 logger，不影响 fileoutput
        7. self.aggregate = get_aggregate(config)            # linkcheck/director/__init__.py
        8. aggregate_url(self.aggregate, url)                # linkcheck/cmdline.py:aggregate_url()
        9. check_urls(self.aggregate)                        # 阻塞直到完成或异常
        10. 返回 gradio_logger.stats（统计信息）

        异常处理:
        - check_urls() 内部已处理 KeyboardInterrupt → interrupt() → abort()
        - RuntimeError（线程启动失败）→ 捕获并返回错误
        - 通用 Exception → 捕获并返回错误
        """

    def cancel_check(self):
        """取消运行中的检查。
        调用 self.aggregate.cancel()（清空队列+设置 shutdown 标志）
        然后 self.aggregate.finish()（停止+join 所有线程）
        check_urls() 中的 urlqueue.join() 会因队列清空而返回。"""

    def pause_check(self):
        """暂停检查（保留持久化 DB）。
        调用 self.aggregate.cancel() + self.aggregate.finish()
        然后 _cleanup_persistence(self.aggregate, interrupted=True)
        interrupted=True 时 DB 文件不会被删除，可用于续传。
        返回 cache_db_path 供后续 resume_check() 使用。"""

    def resume_check(self, cache_db_path, urls, config_overrides, results_list):
        """从之前暂停的检查继续。
        调用链与 run_check() 基本相同，额外设置:
        1. config["persist"] = True
        2. config["resume"] = True
        3. config["cache_db"] = cache_db_path
        get_aggregate() 中的 resume 逻辑会:
        - 验证配置一致性（url 列表、recursionlevel 等）
        - 重置 in-progress 状态的 URL 为 pending
        - 从 DB 恢复已完成的结果到 results_list
        """
```

**复用的现有函数**（文件路径）:

| 函数 | 来源 |
|------|------|
| `Configuration()` | `linkcheck/configuration/__init__.py` |
| `config.set_status_logger()` | `linkcheck/configuration/__init__.py:198` |
| `config.sanitize()` | `linkcheck/configuration/__init__.py:266` |
| `get_aggregate(config)` | `linkcheck/director/__init__.py:160` |
| `check_urls(aggregate)` | `linkcheck/director/__init__.py:27` |
| `aggregate_url(aggregate, url)` | `linkcheck/cmdline.py:69` |
| `StatusLogger()` | `linkcheck/director/console.py:29` |
| `_cleanup_persistence()` | `linkcheck/director/__init__.py:185` |

**断点续传机制**（复用现有持久化层）：

`run_check()` 中默认设置 `config["persist"] = True`，启用 SQLite 持久化后端。这样即使是普通的完整检查，也会将进度持久化到 SQLite DB。

| 现有组件 | 来源 | 作用 |
|----------|------|------|
| `SqliteStore` | `linkcheck/cache/sqlite_store.py` | SQLite WAL 模式后端 |
| `PersistentUrlQueue` | `linkcheck/cache/persistent_url_queue.py` | 持久化 URL 队列 |
| `PersistentResultCache` | `linkcheck/cache/persistent_result_cache.py` | 持久化结果缓存 |
| `_cleanup_persistence()` | `linkcheck/director/__init__.py` | interrupted=True 时保留 DB |
| `get_aggregate()` resume 逻辑 | `linkcheck/director/__init__.py` | 验证配置一致性 + 重置进度 |

### 3.3 Gradio 界面（`linkcheck/web/gradio_app.py`）

**三个 Tab 页**:

#### Tab 1: 链接检查
- **输入区域**: URL 文本框（支持多行批量输入）
- **配置区域**: 线程数滑块(1-20, 默认10)、超时滑块(5-120s, 默认60)、递归深度滑块(-1~10, -1=无限)、检查外部链接复选框
- **控制按钮**: 开始检查（primary）、暂停（stop，保留进度）、续传（secondary，恢复上次暂停的检查）
- **状态显示**: 文本框显示进度（已检查 N 个链接…），暂停后显示"已暂停，可续传"及已完成/待检查数量
- **结果表格**: `gr.Dataframe`，列: URL、父页面、状态(✓/✗/⚠)、结果描述、耗时(s)、大小
- **文件保存区域**（可折叠 `gr.Accordion`）:
  - 保存到文件复选框（默认关闭）
  - 文件路径文本框（默认 `~/linkchecker-results`）
  - 格式选择下拉框: CSV / HTML / Text（对应 `csvlog.py` / `html.py` / `text.py` 内置 Logger）
  - 选中后将对应的文件 Logger 加入 `config["fileoutput"]`
- **导出按钮**: 导出 CSV、导出 HTML → `gr.File` 下载组件（内存中的即时导出，区别于上面的文件保存）

**实时更新机制**（经第 2/3 轮校验确认可行）：

```python
def run_check_with_updates(url, threads, timeout, recursion, extern):
    """Gradio 生成器函数，绑定到 start_btn.click()"""
    results_list = []
    runner = CheckRunner()

    # 后台线程执行检查
    thread = threading.Thread(target=runner.run_check, args=(...))
    thread.start()

    # 轮询 + yield 更新到界面
    last_count = 0
    while thread.is_alive():
        if len(results_list) > last_count:
            last_count = len(results_list)
            yield build_dataframe(results_list), f"已检查 {last_count} 个链接..."
        time.sleep(0.5)

    # 最终结果
    yield build_dataframe(results_list), f"完成！共 {len(results_list)} 个链接"
```

**暂停/续传机制**：
- 暂停按钮调用 `runner.pause_check()`:
  1. `aggregate.cancel()` → 清空队列 + shutdown 标志
  2. `aggregate.finish()` → 停止所有线程
  3. `_cleanup_persistence(aggregate, interrupted=True)` → 保留 cache DB
  4. 返回 cache_db_path，存储在 `gr.State` 中
- 暂停后界面显示: "已暂停 — 已完成 X 个，待检查 Y 个"，续传按钮变为可用
- 续传按钮调用 `runner.resume_check(cache_db_path, ...)`:
  1. 设置 `config["persist"] = True`, `config["resume"] = True`, `config["cache_db"] = path`
  2. `get_aggregate()` 自动从 DB 恢复状态 + 重置 in-progress URL
  3. 已完成的结果从 DB 加载到 results_list（显示在表格中）
  4. 继续检查剩余 URL
- 关闭浏览器后 cache DB 仍保留在磁盘（默认 `~/.linkchecker/` 下），下次启动 `--web` 时可手动选择 DB 续传

**文件保存机制**:
- 勾选「保存到文件」后，`run_check()` 额外实例化对应格式的内置 Logger:
  - CSV → `linkcheck.logger.csvlog.CSVLogger`
  - HTML → `linkcheck.logger.html.HtmlLogger`
  - Text → `linkcheck.logger.text.TextLogger`
- Logger 的 `fileoutput` 参数指向用户指定的文件路径
- 追加到 `config["fileoutput"]` 列表，与 GradioLogger 并行工作
- 检查完成后文件即完整写入（包括 header/footer）

**并发防护**（第 3 轮补充）：
- `CheckRunner.is_running` 标志防止重复点击
- 检查运行中时 start_btn 置灰（`interactive=False`），暂停按钮可用

#### Tab 2: 配置管理
- **配置文件路径**: 文本框（默认使用 Configuration 类的配置发现逻辑）
- **配置编辑器**: `gr.Code(language="ini")` 编辑 INI 格式配置
- **操作按钮**: 加载配置、保存配置
- 读写使用 Python 标准 open/write，配置文件路径参考 `linkcheck/configuration/__init__.py` 中的发现逻辑

#### Tab 3: 历史记录
- **历史列表**: `gr.Dataframe` 显示历史检查记录
- **刷新按钮**: 重新加载历史数据
- **趋势图**: `gr.Plot`（matplotlib）显示错误数量随时间变化趋势
- **URL 筛选**: 按 URL 过滤历史记录

### 3.4 HistoryStore（`linkcheck/web/history_store.py`）

**SQLite 存储**，参考 `linkcheck/cache/sqlite_store.py` 的 WAL 模式。

**存储位置**: `~/.linkchecker/web_history.db`（需创建目录）

**Schema**:
```sql
CREATE TABLE sessions (
    id          TEXT PRIMARY KEY,       -- UUID
    urls        TEXT NOT NULL,          -- JSON 数组
    created_at  REAL NOT NULL,          -- time.time()
    duration    REAL DEFAULT 0,
    total       INTEGER DEFAULT 0,
    valid       INTEGER DEFAULT 0,
    errors      INTEGER DEFAULT 0,
    warnings    INTEGER DEFAULT 0
);

CREATE TABLE results (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL,
    url         TEXT NOT NULL,
    parent_url  TEXT DEFAULT '',
    result      TEXT DEFAULT '',
    valid       INTEGER DEFAULT 1,
    warnings    TEXT DEFAULT '[]',       -- JSON
    checktime   REAL DEFAULT 0,
    size        INTEGER DEFAULT -1,
    content_type TEXT DEFAULT '',
    level       INTEGER DEFAULT 0,
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);

CREATE INDEX idx_results_session ON results(session_id);
CREATE INDEX idx_sessions_created ON sessions(created_at);
```

**方法**:
- `save_session(urls, results, stats, duration)` — 检查完成后保存
- `get_sessions(limit=50)` — 历史列表
- `get_session_results(session_id)` — 单次详情
- `get_trend_data(url_pattern, days=30)` — 趋势数据（返回日期+错误数）
- `delete_session(session_id)` — 删除记录

### 3.5 ExportUtils（`linkcheck/web/export_utils.py`）

- `results_to_csv(results: list[dict]) -> str` — CSV 字符串（字段参考 `linkcheck/logger/csvlog.py:Columns`）
- `results_to_html(results: list[dict]) -> str` — 简单 HTML 表格
- `save_to_tempfile(content, suffix) -> str` — 写入临时文件，返回路径供 Gradio 下载

---

## 4. 现有文件修改规格

### 4.1 `linkcheck/command/arg_parser.py`

在 General options 组（约 275 行）添加：
```python
group.add_argument("--web", action="store_true", default=False,
    help="Start the web interface (requires: pip install linkchecker[web])")
group.add_argument("--web-port", type=int, default=7860, dest="web_port",
    help="Port for the web interface (default: 7860)")
```

### 4.2 `linkcheck/command/linkchecker.py`

在 `options = argparser.parse_args()` 之后（约 118 行）、配置文件读取之前插入：

```python
# Web UI mode - early exit before normal CLI flow
if getattr(options, 'web', False):
    try:
        from ..web.gradio_app import create_app
    except ImportError:
        print(
            "Web UI requires gradio. Install with:\n"
            "  pip install linkchecker[web]",
            file=sys.stderr
        )
        sys.exit(1)
    app = create_app()
    app.launch(server_port=options.web_port, share=False, inbrowser=True)
    sys.exit(0)
```

**位置选择理由**（第 1 轮校验确认）：放在 argparse 之后（能正确解析 --web-port 等参数），但在 config.read() 之前（Web 模式不需要 CLI 配置流程）。

### 4.3 `pyproject.toml`

```toml
[project.optional-dependencies]
web = ["gradio >= 4.0"]
```

---

## 5. 三轮校验发现的关键问题与修正

### 第 1 轮：架构校验

| # | 问题 | 影响 | 修正 |
|---|------|------|------|
| 1 | `LoggerName` 不能为 `None` | `_get_loggers()` 和 `Configuration.__init__` 中 `self.loggers[None]` 会覆盖 | 设置为 `"gradio"` |
| 2 | `config["logger"]` 会被 `sanitize()` 覆盖 | 不能在 sanitize 前设置主 logger | 使用 `config["fileoutput"].append()` 方式 |
| 3 | `StatusLogger` 是必需的 | `config.set_status_logger()` 在 linkchecker.py 中调用 | CheckRunner 中必须调用此方法 |
| 4 | `aggregate_url()` 签名确认 | 需要正确参数 | `aggregate_url(aggregate, url, err_exit_code=2)` |

### 第 2 轮：技术可行性

| # | 问题 | 影响 | 修正 |
|---|------|------|------|
| 5 | `log_filter_url()` 仅在 `do_print=True` 时调用 `log_url()` | Web UI 无法看到所有有效 URL | GradioLogger 必须重写 `log_filter_url()` 以始终调用 `log_url()` |
| 6 | `Logger.__init__` 从 `config['logger']` + `config['fileoutput']` 构建列表 | 确认 fileoutput 方式可行 | 无需修改 |
| 7 | `check_urls()` 是阻塞调用 | 需要后台线程 | `threading.Thread` 中运行，Gradio 生成器轮询 |
| 8 | `CompactUrlData` 使用 `__slots__` | 不能动态添加属性 | 读取现有属性序列化为 dict |

### 第 3 轮：边界情况

| # | 问题 | 影响 | 修正 |
|---|------|------|------|
| 9 | 没有取消机制 | 用户无法中止长时间检查 | 通过共享 flag + `aggregate.cancel()` + `aggregate.finish()` |
| 10 | 并发检查防护缺失 | 多次点击开始会创建多个检查 | `CheckRunner.is_running` 标志 + 按钮状态控制 |
| 11 | `_release.py` 未生成 | 导入 linkcheck 模块会失败 | 文档中注明开发前需运行 `hatchling build -t sdist --hooks-only` |
| 12 | 历史 db 目录可能不存在 | 首次使用报错 | `os.makedirs(~/.linkchecker, exist_ok=True)` |
| 13 | 导出临时文件清理 | 磁盘泄漏 | 使用 `tempfile.NamedTemporaryFile(delete=False)` + Gradio 自动清理 |
| 14 | matplotlib 无额外依赖 | 原方案用 echarts 需要额外包 | 改用 matplotlib，Gradio 原生支持 `gr.Plot` |

---

## 6. 实施顺序

```
Step 1: gradio_logger.py + check_runner.py
        → 核心桥接，能通过代码启动检查并收集结果
        → 编写 test_gradio_logger.py + test_check_runner.py

Step 2: gradio_app.py Tab 1（检查页面）
        → 基本 UI：输入 URL → 点击按钮 → 实时看到结果
        → 包含暂停/续传、文件保存和并发防护

Step 3: arg_parser.py + linkchecker.py + pyproject.toml
        → CLI 集成：linkchecker --web 可用

Step 4: history_store.py + gradio_app.py Tab 3（历史页面）
        → 历史记录存储和展示 + matplotlib 趋势图
        → 编写 test_history_store.py

Step 5: export_utils.py
        → CSV/HTML 导出和下载
        → 编写 test_export_utils.py

Step 6: gradio_app.py Tab 2（配置页面）
        → INI 配置文件的加载和编辑
```

---

## 7. 验证清单

```bash
# 前置条件
hatchling build -t sdist --hooks-only  # 生成 _release.py
pip install -e ".[web]"                 # 安装含 gradio 依赖

# 功能验证
linkchecker --web                       # 启动 → 浏览器打开 127.0.0.1:7860
# Tab 1: 输入 URL → 开始检查 → 实时结果 → 暂停 → 续传 → 导出 CSV/HTML
# 断点续传: 开始检查 → 暂停 → 关闭浏览器 → 重新 --web → 续传 → 完成
# 文件保存: 勾选保存到文件 → 选择格式 → 检查完成后文件即写入
# Tab 2: 加载配置 → 编辑 → 保存
# Tab 3: 查看历史 → 趋势图 → 按 URL 筛选

# 兼容性验证
pip install -e .                        # 不含 [web]
linkchecker https://example.com         # CLI 正常工作
linkchecker --web                       # 提示安装 gradio

# 测试
pytest tests/web/ -v

# Lint
flake8 linkcheck/web/
```

---

## 8. 依赖影响

| 依赖 | 版本 | 用途 | 影响范围 |
|------|------|------|----------|
| gradio | >= 4.0 | Web UI 框架 | 仅 `[web]` 可选依赖 |
| matplotlib | — | 趋势图（gradio 依赖已包含） | 无额外安装 |
| sqlite3 | — | 历史记录（Python 标准库） | 无额外安装 |

**CLI 用户零影响**: 不安装 `[web]` 时，`linkcheck/web/` 模块永远不会被导入。
