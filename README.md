# arXiv cs.AI 每日论文自动采集

使用 GitHub Actions 每日自动采集 arXiv `cs.AI` 分类的最新论文，增量保存到仓库中，永久保留历史数据、不重复采集。

## 功能特性

- **每日自动运行**：每天北京时间 7:00 自动触发
- **数据来源**：arXiv 官方 API（`export.arxiv.org`）
- **采集范围**：`cs.AI` 分类
- **增量去重**：每次查询最近 3 天论文，对照 `seen_papers.json` 去重，只追加新论文，不覆盖历史数据
- **可靠采集**：正确处理 API 分页、遵守限速、失败自动重试、原子写入
- **手动触发**：支持在 GitHub Actions 页面手动运行，方便补采

## 文件结构

```
.
├── fetch_papers.py                  # 核心采集脚本
├── daily_cs.json                    # 当日新抓取的论文（覆盖写，不累积历史）
├── seen_papers.json                 # 已采集论文的 arXiv ID 列表
├── .github/
│   └── workflows/
│       └── daily-fetch.yml          # GitHub Actions 工作流配置
└── README.md
```

## 数据文件说明

### 1. `daily_cs.json`

保存**当日（最近一次运行）新抓取到的论文**，按首次发布时间倒序排列。每次运行会**覆盖写入**，只保留当日新论文，不累积历史数据。历史已采集的论文靠 `seen_papers.json` 去重，无需在此文件保留。

```json
[
  {
    "arxiv_id": "2411.12345",
    "title": "Paper Title",
    "abstract": "This is the abstract...",
    "authors": ["Author A", "Author B"],
    "categories": ["cs.AI"],
    "first_published": "2024-11-20T18:00:00Z",
    "updated": "2024-11-21T09:30:00Z",
    "abstract_url": "http://arxiv.org/abs/2411.12345",
    "pdf_url": "http://arxiv.org/pdf/2411.12345"
  }
]
```

字段说明：

| 字段 | 含义 |
| --- | --- |
| `arxiv_id` | arXiv 论文 ID（唯一标识） |
| `title` | 标题 |
| `abstract` | 摘要 |
| `authors` | 作者列表 |
| `categories` | 所属分类列表 |
| `first_published` | 首次发布时间（UTC，ISO 8601） |
| `updated` | 更新时间（UTC，ISO 8601） |
| `abstract_url` | 论文摘要页链接 |
| `pdf_url` | PDF 链接 |

### 2. `seen_papers.json`

保存所有已采集过的论文 arXiv ID，用于去重，防止重复采集和重复分析。

```json
[
  "2411.12345",
  "2411.12346"
]
```

## 采集逻辑

每日执行以下流程：

1. 查询最近 3 天的 `cs.AI` 论文（3 天窗口是为避免任务延迟或临时失败导致漏采，不代表只保留 3 天数据）
2. 获取论文元数据和摘要
3. 对照 `seen_papers.json` 中的 ID
4. 已存在的论文直接跳过
5. 未采集过的新论文**覆盖写入** `daily_cs.json`（当日新论文，不累积历史）
6. 将新增论文 ID 写入 `seen_papers.json`
7. 提交并推送更新到 GitHub

## 运行方式

### 自动运行

工作流配置了 `cron: "0 23 * * *"`（UTC 时间 23:00，即北京时间次日 7:00），每天自动运行，无需人工干预。

### 手动触发

1. 打开 GitHub 仓库的 **Actions** 页面
2. 选择左侧 **Daily arXiv cs.AI Collector** 工作流
3. 点击右侧 **Run workflow** 按钮
4. 选择分支后点击 **Run workflow** 确认

### 本地运行

```bash
# 安装依赖（仅标准库，无需额外安装）
python fetch_papers.py --days 3
```

## 可靠性设计

- **分页**：循环请求直到取回全部结果（`max_results=100`/页），不遗漏后续页
- **限速**：每次请求间隔 3 秒，遵守 arXiv API 频率限制
- **重试**：请求失败采用指数退避重试（最多 5 次），失败不会标记为成功
- **一致性**：只有成功保存论文后，才更新 `seen_papers.json`，避免漏采
- **原子写入**：先写临时文件再替换，避免中途失败损坏数据文件
- **并发保护**：`concurrency` 配置防止同一工作流并发执行导致冲突

## 注意事项

- arXiv API 返回的 `published` 字段为论文首次提交时间；`updated` 为最近一次版本更新时间
- 首次运行会采集最近 3 天的全部论文作为基线数据
- 仓库需保持 `contents: write` 权限（已在工作流中声明），以便 Actions 推送提交
