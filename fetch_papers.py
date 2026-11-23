#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
arXiv cs.AI 每日论文采集脚本

功能：
  - 通过 arXiv 官方 API 查询 cs.AI 分类下最近 N 天的论文
  - 抓取论文核心信息（ID、标题、摘要、作者、分类、首次发布时间、更新时间、摘要页链接、PDF 链接）
  - 增量采集：对照 seen_papers.json 去重，只追加新论文，永久保留历史数据
  - 正确处理 API 分页，遵守请求频率限制，失败自动重试

用法：
  python fetch_papers.py [--days N] [--data-dir DIR]
"""

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
ARXIV_API_URL = "http://export.arxiv.org/api/query"
CATEGORY = "cs.AI"
QUERY_DAYS = 3            # 每次查询最近 3 天，避免任务延迟/临时失败导致漏采
RESULTS_PER_PAGE = 100    # arXiv API 每页最大返回数
MAX_RESULTS_PER_QUERY = 2000  # arXiv API 单次查询结果上限，超出需按时间窗口拆分
REQUEST_DELAY = 3.0       # 两次请求间隔（秒），遵守 arXiv 限速（建议 >= 3s）
RETRY_TIMES = 5           # 请求失败重试次数
RETRY_BACKOFF = 5.0       # 重试退避基数（秒），指数退避

DEFAULT_DATA_DIR = os.path.dirname(os.path.abspath(__file__))

PAPERS_FILE = "最新的论文信息.json"
SEEN_FILE = "seen_papers.json"


# ---------------------------------------------------------------------------
# arXiv API 请求
# ---------------------------------------------------------------------------
def build_query(start_date: datetime, end_date: datetime, start: int) -> str:
    """构造 arXiv API 查询参数。

    arXiv 查询语法：
      cat:cs.AI                分类过滤
      submittedDate:[... TO ...]  按"首次提交时间"窗口过滤
    """
    start_str = start_date.strftime("%Y%m%d%H%M")
    end_str = end_date.strftime("%Y%m%d%H%M")
    query = (
        f"cat:{CATEGORY} "
        f"AND submittedDate:[{start_str} TO {end_str}]"
    )
    params = {
        "search_query": query,
        "start": start,
        "max_results": RESULTS_PER_PAGE,
        "sortBy": "submittedDate",
        "sortOrder": "descending",
    }
    return urllib.parse.urlencode(params)


def request_api(url: str) -> str:
    """发起 arXiv API 请求，带指数退避重试。

    只有成功拿到 200 响应才返回内容；失败会重试，最终抛异常（不会静默标记成功）。
    """
    last_error = None
    for attempt in range(1, RETRY_TIMES + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "arxiv-csai-collector/1.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"HTTP {resp.status}")
                return resp.read().decode("utf-8")
        except Exception as e:  # noqa: BLE001
            last_error = e
            if attempt < RETRY_TIMES:
                sleep_s = RETRY_BACKOFF * (2 ** (attempt - 1))
                print(f"  [warn] 请求失败（第 {attempt} 次）：{e}，{sleep_s:.0f}s 后重试", file=sys.stderr)
                time.sleep(sleep_s)
    raise RuntimeError(f"arXiv API 请求失败，已重试 {RETRY_TIMES} 次：{last_error}")


def parse_feed(xml_text: str) -> list:
    """从 arXiv Atom XML 中解析论文条目列表。

    字段映射（arXiv Atom 命名空间）：
      id            -> arXiv ID（形如 http://arxiv.org/abs/XXXX.XXXXX）
      title         -> 标题
      summary       -> 摘要
      author        -> 作者列表
      category      -> 分类
      published     -> 首次发布时间
      updated       -> 更新时间
      link          -> 摘要页链接 / PDF 链接
    """
    import xml.etree.ElementTree as ET

    ns = {
        "atom": "http://www.w3.org/2005/Atom",
        "arxiv": "http://arxiv.org/schemas/atom",
    }
    root = ET.fromstring(xml_text)
    papers = []
    for entry in root.findall("atom:entry", ns):
        def _text(tag):
            node = entry.find(f"atom:{tag}", ns)
            return node.text.strip() if node is not None and node.text else ""

        arxiv_id_url = _text("id")
        arxiv_id = arxiv_id_url.rsplit("/abs/", 1)[-1]
        # 剥离版本号（v1/v2/...），以基础 ID 作为唯一标识，避免同一论文不同版本重复采集
        arxiv_id = arxiv_id.split("v", 1)[0] if arxiv_id.endswith(("v1", "v2", "v3", "v4", "v5")) else arxiv_id

        authors = [
            a.find("atom:name", ns).text.strip()
            for a in entry.findall("atom:author", ns)
            if a.find("atom:name", ns) is not None and a.find("atom:name", ns).text
        ]

        categories = [
            c.get("term")
            for c in entry.findall("atom:category", ns)
            if c.get("term")
        ]

        # 链接：摘要页 + PDF
        abstract_url = arxiv_id_url
        pdf_url = ""
        for link in entry.findall("atom:link", ns):
            if link.get("rel") == "alternate" and link.get("title") == "pdf":
                pdf_url = link.get("href", "")

        papers.append({
            "arxiv_id": arxiv_id,
            "title": _text("title"),
            "abstract": _text("summary"),
            "authors": authors,
            "categories": categories,
            "first_published": _text("published"),
            "updated": _text("updated"),
            "abstract_url": abstract_url,
            "pdf_url": pdf_url,
        })
    return papers


# ---------------------------------------------------------------------------
# 文件读写
# ---------------------------------------------------------------------------
def load_json(path: str, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"  [warn] 读取 {path} 失败（{e}），按空数据处理", file=sys.stderr)
        return default


def save_json(path: str, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)  # 原子写入，避免中途失败损坏文件


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def fetch_all_papers(days: int) -> list:
    """查询最近 days 天的 cs.AI 论文，处理分页，返回论文列表（按发布时间倒序）。"""
    now = datetime.now(timezone.utc)
    end_date = now
    start_date = now - timedelta(days=days)

    all_papers = []
    start = 0
    while True:
        url = ARXIV_API_URL + "?" + build_query(start_date, end_date, start)
        print(f"  请求第 {start // RESULTS_PER_PAGE + 1} 页（start={start}）...")
        xml_text = request_api(url)

        import xml.etree.ElementTree as ET
        ns = {"opensearch": "http://a9.com/-/spec/opensearch/1.1/",
              "atom": "http://www.w3.org/2005/Atom"}
        root = ET.fromstring(xml_text)
        total_node = root.find("opensearch:totalResults", ns)
        total = int(total_node.text) if total_node is not None and total_node.text else 0

        papers = parse_feed(xml_text)
        if not papers:
            break
        all_papers.extend(papers)

        start += len(papers)
        if start >= total or total == 0:
            break

        # 超过单次查询上限时，按时间窗口递归拆分（防御性处理）
        if start >= MAX_RESULTS_PER_QUERY:
            print("  [warn] 结果超过单次查询上限，按 1 天窗口拆分重试", file=sys.stderr)
            all_papers = []
            for d in range(days):
                day_end = now - timedelta(days=d)
                day_start = day_end - timedelta(days=1)
                all_papers.extend(fetch_window(day_start, day_end))
            break

        time.sleep(REQUEST_DELAY)

    # 去重（同一 ID 只保留一条），按首次发布时间倒序
    seen = {}
    for p in all_papers:
        seen[p["arxiv_id"]] = p
    papers = list(seen.values())
    papers.sort(key=lambda x: x["first_published"], reverse=True)
    return papers


def fetch_window(start_date: datetime, end_date: datetime) -> list:
    """按给定时间窗口抓取（用于拆分大结果集）。"""
    out = []
    start = 0
    while True:
        url = ARXIV_API_URL + "?" + build_query(start_date, end_date, start)
        xml_text = request_api(url)
        papers = parse_feed(xml_text)
        if not papers:
            break
        out.extend(papers)
        start += len(papers)
        if start >= MAX_RESULTS_PER_QUERY:
            break
        time.sleep(REQUEST_DELAY)
    return out


def main():
    parser = argparse.ArgumentParser(description="arXiv cs.AI 每日论文采集")
    parser.add_argument("--days", type=int, default=QUERY_DAYS, help="查询最近 N 天（默认 3）")
    parser.add_argument("--data-dir", default=DEFAULT_DATA_DIR, help="数据文件目录")
    args = parser.parse_args()

    papers_path = os.path.join(args.data_dir, PAPERS_FILE)
    seen_path = os.path.join(args.data_dir, SEEN_FILE)

    print(f"开始采集 arXiv {CATEGORY} 最近 {args.days} 天论文 ...")
    new_papers = fetch_all_papers(args.days)
    print(f"查询到 {len(new_papers)} 条论文（去重后）")

    # 读取历史已采集 ID
    seen_ids = set(load_json(seen_path, []))
    existing_papers = load_json(papers_path, [])

    # 增量：只保留未采集过的新论文
    to_add = [p for p in new_papers if p["arxiv_id"] not in seen_ids]
    print(f"其中新论文 {len(to_add)} 条，已采集 {len(new_papers) - len(to_add)} 条")

    if not to_add:
        print("无新论文，无需更新。")
        return

    # 追加新论文，并按发布时间倒序重排
    merged = {p["arxiv_id"]: p for p in existing_papers}
    for p in to_add:
        merged[p["arxiv_id"]] = p
    all_sorted = sorted(merged.values(), key=lambda x: x["first_published"], reverse=True)

    # 先保存论文信息，成功后再更新 seen（保证一致性：只有成功保存的才标记为已采集）
    save_json(papers_path, all_sorted)
    new_ids = [p["arxiv_id"] for p in to_add]
    seen_ids.update(new_ids)
    save_json(seen_path, sorted(seen_ids))

    print(f"成功保存 {len(to_add)} 条新论文到 {PAPERS_FILE}")
    print(f"seen_papers.json 已更新（共 {len(seen_ids)} 条 ID）")
    print(f"{PAPERS_FILE} 当前共 {len(all_sorted)} 条论文")


if __name__ == "__main__":
    main()
