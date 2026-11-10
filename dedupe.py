#!/usr/bin/env python3
"""Cross-issue deduplication for the AI news timeline (stdlib only).

A card counts as "already published" when either rule matches an earlier issue:

  1. its normalised headline equals a known headline;
  2. its normalised URL equals a known URL *and* its headline is a rewrite of
     one of that URL's known headlines (similarity >= THRESHOLD).

URL equality alone is deliberately not enough: the source pages sometimes
reuse one column/home URL for several unrelated stories, and a bare URL check
would silently drop valid news. Aliases of filtered cards are also registered,
so a story that changes both its wording and its source link is still caught.

Usage
  python3 dedupe.py --preview           # report only, no writes
  python3 dedupe.py --rebuild           # re-parse every raw HTML, dedupe in
                                       # chronological order, rewrite days + index
"""

import argparse
import difflib
import json
import os
import re
import sys
import unicodedata
from datetime import datetime
from urllib.parse import urlsplit

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from daily_parser import parse_daily, valid_date  # noqa: E402

THRESHOLD = float(os.environ.get("AINEWS_DEDUPE_THRESHOLD", "0.60"))
DATE_IN_NAME = re.compile(r"^(\d{4}-\d{2}-\d{2})\.html$")
COUNT_RE = re.compile(r"\d+")


def norm_title(value):
    """NFKC + lowercase + drop whitespace/punctuation, keeping CJK and digits."""
    text = unicodedata.normalize("NFKC", value or "").lower()
    return "".join(ch for ch in text if ch.isalnum())


def norm_url(value):
    """Lowercase host, drop query/fragment and trailing slashes."""
    url = (value or "").strip()
    if not url:
        return ""
    parts = urlsplit(url)
    path = re.sub(r"/+$", "", parts.path)
    return (parts.netloc.lower() + path).lower()


def similarity(a, b):
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


class Index:
    """Fingerprints of everything published in earlier issues."""

    def __init__(self):
        self.titles = {}   # normalised headline -> first published date
        self.urls = {}     # normalised URL -> set of known headlines

    def add(self, headline, url, date):
        title = norm_title(headline)
        link = norm_url(url)
        if title and title not in self.titles:
            self.titles[title] = date
        if link:
            self.urls.setdefault(link, set()).add(title)

    def match(self, headline, url):
        """Return (reason, first_date, score) or None."""
        title = norm_title(headline)
        link = norm_url(url)
        if title and title in self.titles:
            return "title", self.titles[title], 1.0
        if link and link in self.urls:
            best_title, best_score = "", 0.0
            for known in self.urls[link]:
                score = similarity(title, known)
                if score > best_score:
                    best_title, best_score = known, score
            if best_score >= THRESHOLD:
                return "url_rewrite", self.titles.get(best_title, ""), best_score
        return None


def build_index(history_docs):
    """Index retained cards plus aliases of cards filtered in earlier issues."""
    index = Index()
    for doc in history_docs:
        date = doc.get("date", "")
        for cat in doc.get("cats", []):
            for card in cat.get("cards", []):
                index.add(card.get("headline", ""), card.get("url", ""), date)
        for item in doc.get("dedupe", {}).get("items", []):
            index.add(item.get("headline", ""), item.get("url", ""),
                      item.get("first_date") or date)
    return index


def filter_document(document, history_docs):
    """Return a new day document with cards already published removed."""
    if document.get("dedupe", {}).get("enabled"):
        return document  # idempotent: never filter the same document twice

    date = document.get("date", "")
    index = build_index(history_docs)
    removed_items = []
    new_cats = []

    for cat in document.get("cats", []):
        kept_cards = []
        for card in cat.get("cards", []):
            headline = card.get("headline", "")
            url = card.get("url", "")
            match = index.match(headline, url)
            if match:
                reason, first_date, score = match
                removed_items.append({
                    "headline": headline,
                    "url": url,
                    "cat": cat.get("name", ""),
                    "reason": reason,
                    "first_date": first_date,
                    "score": round(score, 2),
                })
                index.add(headline, url, first_date or date)
            else:
                kept_cards.append(card)
                index.add(headline, url, date)
        if not kept_cards:
            continue
        new_cat = dict(cat)
        new_cat["cards"] = kept_cards
        old_count = str(new_cat.get("count", ""))
        new_cat["count"] = (COUNT_RE.sub(str(len(kept_cards)), old_count, count=1)
                            if COUNT_RE.search(old_count)
                            else "%d 条" % len(kept_cards))
        new_cats.append(new_cat)

    result = dict(document)
    result["cats"] = new_cats
    result["total"] = sum(len(cat["cards"]) for cat in new_cats)
    result["cat_count"] = len(new_cats)
    result["dedupe"] = {
        "enabled": True,
        "removed": len(removed_items),
        "threshold": THRESHOLD,
        "history_days": [d.get("date", "") for d in history_docs],
        "items": removed_items,
        "at": now_iso(),
    }
    return result


def rebuild(preview=False, verbose=False):
    """Re-parse every raw daily HTML and rewrite all structured days in order."""
    import server  # reuse atomic_write / load_index helpers

    data_dir = os.path.join(ROOT, "data")
    raw_dir = os.path.join(data_dir, "raw")
    days_dir = os.path.join(data_dir, "days")
    dates = sorted(
        match.group(1)
        for name in os.listdir(raw_dir)
        for match in [DATE_IN_NAME.match(name)]
        if match
    )
    if not dates:
        print("[dedupe] data/raw/ 里没有可重建的日期文件", file=sys.stderr)
        return 2

    history = []
    entries = []
    for date in dates:
        with open(os.path.join(raw_dir, date + ".html"), encoding="utf-8") as fh:
            source = fh.read()
        old = server.read_json(os.path.join(days_dir, date + ".json"), {})
        document = parse_daily(source, date,
                               updated_at=old.get("updated_at") or now_iso())
        filtered = filter_document(document, history)
        if filtered["total"] == 0:
            print("%s  全部 %d 条均为往期重复，跳过（不创建空期）"
                  % (date, filtered["dedupe"]["removed"]))
            continue
        history.append(filtered)
        print("%s  %2d → %2d 条  (过滤 %2d, %d 类)"
              % (date, document["total"], filtered["total"],
                 filtered["dedupe"]["removed"], filtered["cat_count"]))
        if verbose:
            for item in filtered["dedupe"]["items"]:
                print("    - [%s] %s → 首见 %s"
                      % (item["reason"], item["headline"], item["first_date"]))
        entries.append({
            "date": date,
            "title": filtered["title"],
            "date_label": filtered["date_label"],
            "weekday": filtered["weekday"],
            "total": filtered["total"],
            "cat_count": filtered["cat_count"],
            "updated_at": filtered["updated_at"],
        })
        if not preview:
            server.atomic_write(os.path.join(days_dir, date + ".json"),
                                json.dumps(filtered, ensure_ascii=False, indent=1))

    if not preview:
        entries.sort(key=lambda e: e["date"], reverse=True)
        server.atomic_write(os.path.join(data_dir, "entries.json"), json.dumps(
            {"entries": entries, "updated_at": now_iso()},
            ensure_ascii=False, indent=1))
    print("[dedupe] %s：共 %d 期，%d → %d 条，过滤 %d 条"
          % ("预览（未写盘）" if preview else "已重建", len(history),
             sum(d["total"] for d in history) + sum(d["dedupe"]["removed"] for d in history),
             sum(d["total"] for d in history),
             sum(d["dedupe"]["removed"] for d in history)))
    return 0


def main():
    ap = argparse.ArgumentParser(description="跨期新闻去重与历史回填")
    ap.add_argument("--preview", action="store_true", help="只打印报告，不写盘")
    ap.add_argument("--rebuild", action="store_true", help="按日期正序重建全部期")
    ap.add_argument("--verbose", action="store_true", help="列出每条被过滤的标题")
    args = ap.parse_args()
    if args.preview == args.rebuild:
        ap.error("请在 --preview 与 --rebuild 中二选一")
    return rebuild(preview=args.preview, verbose=args.verbose)


if __name__ == "__main__":
    sys.exit(main())
