#!/usr/bin/env python3
"""Parse the daily AI news HTML (category cards) into structured JSON.

Uses only the Python standard library. The source HTML is produced by a
WorkBuddy automation, so its shape is stable; this parser builds a tiny DOM
and reads by class name instead of relying on brittle line-based regex.
"""

import html as html_lib
import re
from datetime import datetime
from html.parser import HTMLParser

VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
}

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
COLOR_RE = re.compile(r"--c\s*:\s*(#[0-9a-fA-F]{3,8})")
FOOTER_COUNT_RE = re.compile(r"(\d+)\s*类\s*[·・]\s*(\d+)\s*条")


def valid_date(value):
    return bool(value and DATE_RE.match(value))


def clean(value):
    return re.sub(r"\s+", " ", (value or "")).strip()


class Node:
    __slots__ = ("tag", "attrs", "children")

    def __init__(self, tag, attrs=None):
        self.tag = tag
        self.attrs = dict(attrs or {})
        self.children = []

    @property
    def classes(self):
        return self.attrs.get("class", "").split()

    def has(self, name):
        return name in self.classes


class _DOMBuilder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("#root")
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = Node(tag, attrs)
        self.stack[-1].children.append(node)
        if tag not in VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.stack[-1].children.append(Node(tag, attrs))

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def walk(node):
    for child in node.children:
        if isinstance(child, Node):
            yield child
            yield from walk(child)


def node_text(node):
    if node is None:
        return ""
    parts = []
    for child in node.children:
        parts.append(child if isinstance(child, str) else node_text(child))
    return clean("".join(parts))


def find_class(node, name):
    if node is None:
        return None
    for item in walk(node):
        if item.has(name):
            return item
    return None


def find_tag(node, tag):
    for item in walk(node):
        if item.tag == tag:
            return item
    return None


def find_all_class(node, name):
    return [item for item in walk(node) if item.has(name)]


def _text_of_class(node, name):
    return node_text(find_class(node, name))


def parse_card(card):
    headline = find_class(card, "headline")
    ems = [node_text(e) for e in find_all_class(headline, "em")] if headline else []
    return {
        "url": card.attrs.get("href", ""),
        "tag": _text_of_class(card, "tag"),
        "src": _text_of_class(card, "src"),
        "headline": node_text(headline),
        "ems": [e for e in ems if e],
        "one": html_lib.unescape(_text_of_class(card, "one")),
        "hnum": _text_of_class(card, "hnum"),
        "hlab": _text_of_class(card, "hlab"),
    }


def parse_daily(source, date, updated_at=None):
    """Return the structured day document for one daily HTML file."""
    if not valid_date(date):
        raise ValueError("date must be YYYY-MM-DD")

    builder = _DOMBuilder()
    builder.feed(source)
    builder.close()
    root = builder.root

    cats = []
    for section in find_all_class(root, "cat"):
        style = section.attrs.get("style", "")
        match = COLOR_RE.search(style)
        cards = [parse_card(c) for c in find_all_class(section, "card")]
        cats.append({
            "name": _text_of_class(section, "cat-title"),
            "color": match.group(1) if match else "#5b5bd6",
            "sub": _text_of_class(section, "cat-sub"),
            "count": _text_of_class(section, "cat-count"),
            "cards": cards,
        })

    date_line = _text_of_class(root, "date")
    weekday = ""
    for piece in date_line.split("·"):
        piece = piece.strip()
        if "周" in piece or "星期" in piece:
            weekday = piece
            break

    footer = node_text(find_tag(root, "footer"))
    footer_match = FOOTER_COUNT_RE.search(footer)
    # Trust the real parsed cards; the footer number is only a sanity signal.
    total = sum(len(cat["cards"]) for cat in cats)

    return {
        "date": date,
        "title": node_text(find_tag(root, "h1")) or "AI 每日要闻 · " + date,
        "date_label": clean(date_line.split("·")[0]) if date_line else date,
        "weekday": weekday,
        "subtitle": date_line,
        "philosophy": html_lib.unescape(_text_of_class(root, "phil")),
        "kicker": _text_of_class(root, "kicker"),
        "total": total,
        "cat_count": len(cats),
        "cats": cats,
        "footer_declared": int(footer_match.group(2)) if footer_match else None,
        "updated_at": updated_at or datetime.now().astimezone().isoformat(timespec="seconds"),
    }
