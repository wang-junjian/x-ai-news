#!/usr/bin/env python3
"""Publish one daily AI news HTML to the timeline, and keep a local copy.

Usage
  python3 push.py /abs/path/ai_daily_2026-11-09.html
  python3 push.py /abs/path/file.html --date 2026-11-09
  python3 push.py --pull                 # sync every online day back to local
  AINEWS_URL=http://127.0.0.1:3000 python3 push.py file.html   # local test

The local data/ directory is the authoritative copy: it is always updated
even when the remote publish fails, so a redeploy can restore all history.
"""

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from urllib.parse import quote, urlsplit, urlunsplit

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

import server  # noqa: E402  (reuses its atomic store + parser)
from daily_parser import parse_daily, valid_date  # noqa: E402

CONFIG = server.load_config()
DEFAULT_URL = os.environ.get("AINEWS_URL") or CONFIG.get("base_url") or "http://127.0.0.1:3000"
TOKEN = os.environ.get("AINEWS_PUBLISH_TOKEN") or CONFIG.get("publish_token", "")
SHARECODE = os.environ.get("AINEWS_SHARECODE") or CONFIG.get("sharecode", "")
DATE_IN_NAME = re.compile(r"(\d{4}-\d{2}-\d{2})")


def endpoint(base, path):
    """The share gateway requires ?sharecode= on every non-browser request."""
    parts = urlsplit(base)
    query = parts.query
    if SHARECODE and "sharecode=" not in query:
        suffix = "sharecode=" + quote(SHARECODE, safe="")
        query = suffix + ("&" + query if query else "")
    return urlunsplit((parts.scheme, parts.netloc, path, query, ""))


def date_from(path):
    match = DATE_IN_NAME.search(os.path.basename(path))
    return match.group(1) if match else ""


def write_local(date, source):
    document = parse_daily(source, date, updated_at=server.now_iso())
    if not document["total"]:
        raise ValueError("解析到 0 张卡片，拒绝写入")
    server.save_day(date, source, document)
    return document


def post_remote(base, date, source):
    body = json.dumps({"date": date, "html": source}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        endpoint(base, "/api/publish"),
        data=body,
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "X-Publish-Token": TOKEN,
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def pull(base):
    with urllib.request.urlopen(endpoint(base, "/api/export"), timeout=60) as response:
        data = json.loads(response.read().decode("utf-8"))
    days, raw = data.get("days", {}), data.get("raw", {})
    for date, document in days.items():
        if not valid_date(date):
            continue
        server.atomic_write(server.day_path(date),
                            json.dumps(document, ensure_ascii=False, indent=1))
        if date in raw:
            server.atomic_write(server.raw_path(date), raw[date])
    index = server.load_index()
    by_date = {}
    for entry in index["entries"]:
        by_date[entry["date"]] = entry
    for date, document in days.items():
        by_date[date] = {
            "date": date,
            "title": document.get("title", ""),
            "date_label": document.get("date_label", date),
            "weekday": document.get("weekday", ""),
            "total": document.get("total", 0),
            "cat_count": document.get("cat_count", 0),
            "updated_at": document.get("updated_at"),
        }
    entries = sorted(by_date.values(), key=lambda e: e["date"], reverse=True)
    server.atomic_write(server.INDEX_PATH, json.dumps(
        {"entries": entries, "updated_at": server.now_iso()}, ensure_ascii=False, indent=1))
    print("[pull] 已同步 %d 期到本地 data/" % len(days))
    return 0


def main():
    ap = argparse.ArgumentParser(description="推送每日 AI 新闻 HTML 到时间线")
    ap.add_argument("file", nargs="?", help="当日 HTML 文件绝对路径")
    ap.add_argument("--date", help="覆盖从文件名推断的日期，格式 YYYY-MM-DD")
    ap.add_argument("--url", default=DEFAULT_URL, help="站点地址")
    ap.add_argument("--pull", action="store_true", help="把线上全部历史同步回本地")
    ap.add_argument("--local-only", action="store_true", help="只写本地，不推送线上")
    args = ap.parse_args()

    if args.pull:
        return pull(args.url)
    if not args.file:
        ap.error("需要提供 HTML 文件路径")

    path = os.path.abspath(os.path.expanduser(args.file))
    if not os.path.isfile(path):
        print("[error] 文件不存在: %s" % path, file=sys.stderr)
        return 2
    date = args.date or date_from(path)
    if not valid_date(date):
        print("[error] 无法从文件名推断日期，请用 --date YYYY-MM-DD", file=sys.stderr)
        return 2
    with open(path, encoding="utf-8") as fh:
        source = fh.read()

    try:
        document = write_local(date, source)
    except Exception as exc:
        print("[error] 本地写入失败: %s" % exc, file=sys.stderr)
        return 3
    print("[local] %s 已归档 %d 条 / %d 类" % (date, document["total"], document["cat_count"]))

    if args.local_only:
        return 0
    try:
        result = post_remote(args.url, date, source)
    except urllib.error.HTTPError as exc:
        print("[error] 线上推送失败 HTTP %d: %s" % (exc.code, exc.read().decode("utf-8", "replace")),
              file=sys.stderr)
        return 4
    except Exception as exc:
        print("[error] 线上推送失败: %s" % exc, file=sys.stderr)
        return 4
    print("[online] %s %s 条 · 共 %s 期 · %s"
          % (date, result.get("total"), result.get("total_days"), args.url))
    return 0


if __name__ == "__main__":
    sys.exit(main())
