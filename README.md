# AI 新闻时间线 / AI News Timeline

把每日 AI 要闻 HTML 解析成结构化数据，按时间线连续呈现，支持搜索与分类筛选。
**零第三方依赖**：服务端与解析器只用 Python 标准库，前端是单文件 HTML。

[中文](#中文) · [English](#english) · [MIT License](LICENSE)

---

## 中文

### 它做什么

一个自动化流程每天生成一份「AI 每日要闻」HTML（按类别排布的卡片）。本项目负责：

1. **解析** — `daily_parser.py` 用 `html.parser` 建一个轻量 DOM，按 class 读取卡片，产出结构化 JSON；
2. **存储** — 纯 JSON 落盘，`data/` 是本地权威副本，写入走原子替换；
3. **服务** — `server.py` 用 `ThreadingHTTPServer` 提供页面与只读 API，发布接口由 Token 保护；
4. **展示** — `public/index.html` 零依赖单页，按日期倒序、增量加载、搜索、分类筛选，并可跳转当日原始 HTML；
5. **推送** — `push.py` 把新一期同时写到本地与线上，也能把线上全部历史拉回本地。

仓库内 `data/` 含 4 期示例数据，clone 下来即可看到完整时间线。示例内容是对公开报道的速读整理，仅作演示。

### 快速开始

```bash
python3 server.py                 # 默认 http://127.0.0.1:3000
PORT=8080 python3 server.py       # 换端口
curl http://127.0.0.1:3000/health
```

要求 Python 3.8+，无需 `pip install`。

### 配置

配置有两种方式，**环境变量优先**：

```bash
cp config.example.json config.json    # 本地配置，已被 .gitignore 排除
```

| 配置项 | 环境变量 | 说明 |
| --- | --- | --- |
| `publish_token` | `AINEWS_PUBLISH_TOKEN` | `POST /api/publish` 的鉴权 Token；不设置则发布接口恒返回 401 |
| `base_url` | `AINEWS_URL` | `push.py` 默认推送目标 |
| `sharecode` | `AINEWS_SHARECODE` | 可选；部署在需要分享码的网关后面时，自动给非浏览器请求带上 `?sharecode=` |
| — | `AINEWS_DATA_DIR` | 指定可写数据目录；只读沙箱下会自动从 `data/` 播种 |
| — | `PORT` | 监听端口，默认 `3000` |

> **安全提示**：`config.json` 含明文凭据，永远不要提交。生产环境建议只通过环境变量注入 Token，并用随机长字符串。`GET` 接口均为只读且开放 CORS；`/api/publish` 是唯一写接口。

### 推送与同步

```bash
python3 push.py /abs/path/ai_daily_2026-11-10.html              # 本地 + 线上
python3 push.py /abs/path/file.html --date 2026-11-10           # 显式指定日期
python3 push.py /abs/path/file.html --local-only                 # 只写本地
python3 push.py --pull                                           # 线上历史同步回本地
AINEWS_URL=http://127.0.0.1:3000 python3 push.py file.html      # 本地联调
```

`push.py` 先写本地再推线上：即使线上失败，本地历史仍完整，重新部署即可恢复。解析到 0 张卡片时会拒绝写入，避免覆盖已有数据。

### 跨期去重

每日 HTML 常会带上往期已经发布过的新闻。发布链路会自动过滤，**只保留首次发布的那一条**。

判定规则（`dedupe.py`，纯标准库）：

1. 标题归一化（NFKC、去标点与空白、小写）后与历史标题相同 → 重复；
2. 链接归一化（去 query、尾斜杠、host 小写）后相同，且标题相似度 ≥ `0.60` → 视为同一条的措辞改写 → 重复。

不单独按 URL 判重：源页面有时会把同一个栏目/首页链接挂在多条**不同**新闻上。被过滤条目的标题与链接也会登记为历史指纹，避免「改标题 + 换来源」再次漏网。过滤发生在 `/api/publish` 落库前和 `push.py` 本地归档时，只比对早于当期的历史，重发同一天结果不变。过滤后重算 `total` 与分类计数，并在当日 JSON 写入 `dedupe` 审计字段（过滤条数、标题、原因、首次发布日期）。

`data/raw/*.html` 是未经改动的原始归档，「查看原始版式」仍展示完整卡片；主页时间线、搜索与分类筛选使用过滤后的结构化数据。

历史回填与复核（从 raw HTML 重建，可反复执行，不会二次过滤）：

```bash
python3 dedupe.py --preview --verbose    # 只打印报告，不写盘
python3 dedupe.py --rebuild              # 按日期正序重建 data/days/*.json 与 entries.json
```

若整期卡片全是往期重复，`/api/publish` 返回 422 并拒绝创建无内容的空期，`--rebuild` 也会跳过该期。

阈值可用环境变量 `AINEWS_DEDUPE_THRESHOLD` 调整（默认 `0.60`）。

### HTTP API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/` | 时间线页面 |
| GET | `/health` | 健康检查（数据目录、期数、时间） |
| GET | `/api/entries` | 轻量索引，按日期倒序 |
| GET | `/api/day?date=YYYY-MM-DD` | 单期结构化数据 |
| GET | `/api/feed?limit=30` | 最近 N 期完整数据，最大 365 |
| GET | `/api/export` | 全量备份（结构化数据 + 原始 HTML） |
| GET | `/d/<date>.html` | 当日未经改动的原始 HTML |
| POST | `/api/publish` | 发布一期，需 `X-Publish-Token` 头，Body 为 `{"date","html"}`；自动过滤往期重复，返回 `total` 与 `removed` |

### 目录结构

```
.
├── server.py           # HTTP 服务、原子存储、发布接口
├── daily_parser.py     # 每日 HTML → 结构化 JSON
├── dedupe.py           # 跨期去重与历史回填（--preview / --rebuild）
├── push.py             # 推送 / 拉取 / 本地归档
├── public/index.html   # 零依赖单页前端
├── data/               # 示例数据：entries.json + days/ + raw/
├── config.example.json # 配置模板
└── LICENSE             # MIT
```

### 许可

[MIT](LICENSE) © 2026 Junjian Wang

---

## English

### What it does

An automation produces one "AI Daily" HTML page each day (news cards grouped by category). This project turns that into a browsable timeline:

1. **Parse** — `daily_parser.py` builds a tiny DOM with `html.parser` and reads cards by class name, emitting structured JSON.
2. **Store** — plain JSON on disk, with atomic writes; `data/` is the local authoritative copy.
3. **Serve** — `server.py` (stdlib `ThreadingHTTPServer`) exposes the page and read-only APIs; publishing is token-protected.
4. **Display** — `public/index.html` is a dependency-free single page: newest first, incremental loading, search, category filters, and a link to the original daily HTML.
5. **Publish** — `push.py` writes a new issue locally and remotely, and can pull the full online history back.

`data/` ships with four sample issues so a fresh clone shows a working timeline. The sample content is a digest of public reporting, for demonstration only.

### Quick start

```bash
python3 server.py                 # http://127.0.0.1:3000 by default
PORT=8080 python3 server.py
curl http://127.0.0.1:3000/health
```

Python 3.8+; no `pip install` required.

### Configuration

Environment variables take precedence over the optional local `config.json` (git-ignored):

```bash
cp config.example.json config.json
```

| Config key | Environment variable | Purpose |
| --- | --- | --- |
| `publish_token` | `AINEWS_PUBLISH_TOKEN` | Auth token for `POST /api/publish`; without it, publishing always returns 401 |
| `base_url` | `AINEWS_URL` | Default remote target for `push.py` |
| `sharecode` | `AINEWS_SHARECODE` | Optional; appends `?sharecode=` to non-browser requests behind a share-code gateway |
| — | `AINEWS_DATA_DIR` | Writable data directory; a read-only sandbox is seeded from `data/` |
| — | `PORT` | Listen port, default `3000` |

> **Security**: never commit `config.json` — it holds a plaintext credential. Prefer injecting the token via environment variables in production, using a long random value. All `GET` routes are read-only and CORS-open; `/api/publish` is the only write route.

### Publish & sync

```bash
python3 push.py /abs/path/ai_daily_2026-11-10.html
python3 push.py /abs/path/file.html --date 2026-11-10
python3 push.py /abs/path/file.html --local-only
python3 push.py --pull
```

`push.py` writes locally first, so a remote failure never loses history. A source page that parses to zero cards is rejected to avoid overwriting an existing issue.

### Cross-issue deduplication

A daily HTML page often repeats stories already carried by an earlier issue. The publish path filters them automatically, **keeping the first appearance only**.

Rules (`dedupe.py`, stdlib only):

1. A normalised headline (NFKC, punctuation/whitespace removed, lowercased) equal to a known headline → duplicate;
2. A normalised URL (query, trailing slash and host case removed) equal to a known URL, with headline similarity ≥ `0.60` → a rewording of the same story → duplicate.

URL equality alone is not used: source pages sometimes attach one column/home URL to several **different** stories. Aliases of filtered cards are registered as history fingerprints too, so changing both the wording and the source link cannot slip through. Filtering runs before `/api/publish` writes to disk and while `push.py` archives locally; only earlier issues are compared, so republishing the same date is stable. `total` and category counts are recomputed, and a `dedupe` audit field (removed count, headlines, reasons, first-published dates) is written into the day JSON.

`data/raw/*.html` keeps the untouched archive, so "view original layout" still shows every card; the timeline, search and filters use the filtered structured data.

Rebuild or audit history (re-parses from raw HTML, safe to rerun):

```bash
python3 dedupe.py --preview --verbose    # report only, no writes
python3 dedupe.py --rebuild              # rewrite data/days/*.json and entries.json in date order
```

If every card in an issue is a duplicate, `/api/publish` returns 422 and refuses to create an empty day; `--rebuild` skips such an issue too.

Override the threshold with `AINEWS_DEDUPE_THRESHOLD` (default `0.60`).

### HTTP API

| Method | Path | Description |
| --- | --- | --- |
| GET | `/` | Timeline page |
| GET | `/health` | Health probe (data dir, issue count, time) |
| GET | `/api/entries` | Lightweight index, newest first |
| GET | `/api/day?date=YYYY-MM-DD` | One structured issue |
| GET | `/api/feed?limit=30` | Recent N issues, max 365 |
| GET | `/api/export` | Full backup (structured data + raw HTML) |
| GET | `/d/<date>.html` | Original daily HTML, unmodified |
| POST | `/api/publish` | Publish one issue; requires `X-Publish-Token`, body `{"date","html"}`; drops earlier-issue duplicates and returns `total` / `removed` |

### Layout

```
.
├── server.py           # HTTP server, atomic store, publish endpoint
├── daily_parser.py     # Daily HTML -> structured JSON
├── dedupe.py           # Cross-issue dedupe + history rebuild (--preview / --rebuild)
├── push.py             # Publish / pull / local archive
├── public/index.html   # Dependency-free single-page frontend
├── data/               # Sample data: entries.json + days/ + raw/
├── config.example.json # Config template
└── LICENSE             # MIT
```

### License

[MIT](LICENSE) © 2026 Junjian Wang
