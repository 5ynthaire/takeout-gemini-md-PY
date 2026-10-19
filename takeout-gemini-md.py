#!/usr/bin/env python3
"""Export English or Japanese Takeout Gemini activity to Markdown.

Usage:
    python takeout-gemini-md.py --takeoutzip takeout.zip [--out gemini_log]
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import re
import shutil
import stat
import sys
import zipfile
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Iterable
from urllib.parse import unquote

try:
    from bs4 import BeautifulSoup, NavigableString, Tag
except ImportError:  # pragma: no cover - reported clearly by main()
    BeautifulSoup = NavigableString = Tag = None  # type: ignore[assignment,misc]

CSV_COLUMNS = ("md_file", "thread_title", "post_count", "first_post_datetime", "last_post_datetime", "original_url")
APP_ID_RE = re.compile(r"https://gemini\.google\.com/app/([0-9a-f]{16})(?:[/?#]|$)", re.I)
ORIGINAL_NAME_RE = re.compile(r"^-\s+(.*)$", re.S)
GENERATED_IMAGE_RE = re.compile(r"^\d+\s+generated image", re.I)
PROMPT_PREFIXES = ("Prompted ", "送信したメッセージ:", "送信したメッセージ：")
TITLE_MAX = 120

@dataclass(frozen=True)
class Attachment:
    source_ref: str
    original_name: str
    role: str

@dataclass
class Post:
    role: str
    body: str
    timestamp: str
    attachments: list[Attachment]

def text(value: Any) -> str:
    return value if isinstance(value, str) else "" if value is None else str(value)

def app_id(item: dict[str, Any]) -> str:
    for detail in item.get("details") or []:
        if isinstance(detail, dict):
            match = APP_ID_RE.search(text(detail.get("url") or detail.get("name")).strip())
            if match:
                return match.group(1).lower()
    return ""

def is_prompted(item: dict[str, Any]) -> bool:
    title = text(item.get("title"))
    return any(title.startswith(p) for p in PROMPT_PREFIXES) and bool(app_id(item))

def prompt_text(item: dict[str, Any]) -> str:
    title = text(item.get("title"))
    for prefix in PROMPT_PREFIXES:
        if title.startswith(prefix):
            return title[len(prefix):].strip()
    return ""

def iso_datetime(value: Any) -> datetime | None:
    raw = text(value).strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone()  # naive -> local zone; aware -> converted to local zone

def timestamp_text(value: Any) -> str:
    parsed = iso_datetime(value)
    return parsed.isoformat(sep=" ", timespec="seconds") if parsed else ""

def first_line_title(prompt: str) -> str:
    line = (prompt or "").replace("\r", "").split("\n", 1)[0].strip()
    return line[: TITLE_MAX - 1].rstrip() + "…" if len(line) > TITLE_MAX else line

def _escape_url(value: str) -> str:
    return value.replace("\\", "%5C").replace("(", "%28").replace(")", "%29").replace(" ", "%20")

def _render_html_node(node: Any, *, list_depth: int = 0) -> str:
    """HTML -> Markdown for the subset Gemini responses actually use.
    Tables and blockquotes are not special-cased; their text still comes
    through, just without pipe-table / '> ' formatting.
    """
    if NavigableString is not None and isinstance(node, NavigableString):
        return str(node)
    if Tag is None or not isinstance(node, Tag):
        return ""

    name = node.name.lower()
    if name in {"script", "style", "head", "noscript"}:
        return ""
    if name == "br":
        return "\n"
    if name == "img":
        alt = text(node.get("alt")).strip()
        return f"[{alt}]" if alt else ""

    if name in {"ul", "ol"}:
        items: list[str] = []
        for i, child in enumerate(c for c in node.children if isinstance(c, Tag) and c.name.lower() == "li"):
            prefix = f"{i + 1}. " if name == "ol" else "- "
            lines = _render_html_node(child, list_depth=list_depth + 1).strip().splitlines() or [""]
            indent = "  " * list_depth
            items.append(indent + prefix + lines[0])
            items.extend(indent + "  " + line for line in lines[1:])
        return "\n" + "\n".join(items) + "\n"
    if name == "li":
        return "".join(_render_html_node(c, list_depth=list_depth) for c in node.children).strip()

    if name == "pre":
        code = node.get_text("", strip=False).strip("\n")
        fence = "```"
        while fence in code:
            fence += "`"
        return f"\n\n{fence}\n{code}\n{fence}\n\n"

    children = "".join(_render_html_node(c, list_depth=list_depth) for c in node.children)
    if name in {"b", "strong"}:
        return f"**{children.strip()}**" if children.strip() else ""
    if name in {"i", "em"}:
        return f"*{children.strip()}*" if children.strip() else ""
    if name in {"del", "s", "strike"}:
        return f"~~{children.strip()}~~" if children.strip() else ""
    if name == "code":
        content = children.strip()
        if not content:
            return ""
        ticks = "`" * (max((len(m.group(0)) for m in re.finditer(r"`+", content)), default=0) + 1)
        pad = " " if content.startswith("`") or content.endswith("`") else ""
        return f"{ticks}{pad}{content}{pad}{ticks}"
    if name == "a":
        href, label = text(node.get("href")).strip(), children.strip()
        return f"[{label}]({_escape_url(href)})" if href and label else label or href
    if name.startswith("h") and len(name) == 2 and name[1].isdigit():
        level = max(1, min(6, int(name[1])))
        return f"\n\n{'#' * level} {children.strip()}\n\n" if children.strip() else ""
    if name in {"p", "div", "section", "article", "header", "footer", "main"}:
        return f"\n\n{children.strip()}\n\n" if children.strip() else ""
    if name == "hr":
        return "\n\n---\n\n"
    return children

def html_to_markdown(value: str) -> str:
    if not value:
        return ""
    if BeautifulSoup is None:
        raise RuntimeError("Beautiful Soup is required. Install it with: python -m pip install beautifulsoup4")
    soup = BeautifulSoup(value, "html.parser")
    for node in soup.find_all(["script", "style", "head", "noscript"]):
        node.decompose()
    rendered = "".join(_render_html_node(c) for c in soup.contents)
    rendered = html.unescape(rendered).replace("\xa0", " ").replace("\r", "")
    rendered = re.sub(r"[ \t]+\n", "\n", rendered)
    rendered = re.sub(r"\n[ \t]+", "\n", rendered)
    rendered = re.sub(r"\n{3,}", "\n\n", rendered)
    return rendered.strip()

def assistant_html(item: dict[str, Any]) -> str:
    parts = item.get("safeHtmlItem") or []
    if isinstance(parts, str):
        return parts
    if not isinstance(parts, list):
        return ""
    return "\n".join(
        text(p.get("html")) if isinstance(p, dict) else p for p in parts if isinstance(p, (dict, str))
    )

def _skip_file_ref(value: str) -> bool:
    lower = value.strip().lower()
    return not lower or lower.startswith((
        "http://", "https://", "data:",
        "https://gemini.google.com/gems/view",
        "http://googleusercontent.com/youtube_content/",
        "https://googleusercontent.com/youtube_content/",
    ))

def attachments_on_item(item: dict[str, Any]) -> list[Attachment]:
    refs: list[str] = []
    attached = item.get("attachedFiles") or []
    attached = [attached] if isinstance(attached, str) else attached
    for value in attached if isinstance(attached, list) else []:
        candidate = text(value).strip()
        if candidate and candidate not in refs and not _skip_file_ref(candidate):
            refs.append(candidate)

    image_file = text(item.get("imageFile")).strip()
    if image_file and image_file not in refs and not _skip_file_ref(image_file):
        refs.append(image_file)

    original_names: dict[str, str] = {}
    generated_image = False
    for subtitle in item.get("subtitles") or []:
        if not isinstance(subtitle, dict):
            continue
        name = text(subtitle.get("name")).strip()
        url = text(subtitle.get("url")).strip()
        if GENERATED_IMAGE_RE.match(name) or "generated image" in name.lower():
            generated_image = True
        original_match = ORIGINAL_NAME_RE.match(name)
        if original_match and url and not _skip_file_ref(url):
            original = original_match.group(1).strip()
            if original:
                original_names[url] = original
                if url not in refs:
                    refs.append(url)

    return [
        Attachment(
            source_ref=ref,
            original_name=original_names.get(ref) or PurePosixPath(ref.replace("\\", "/")).name or ref,
            role="assistant" if generated_image and ref not in original_names else "user",
        )
        for ref in refs
    ]

def load_activity(archive: zipfile.ZipFile) -> tuple[str, list[dict[str, Any]]]:
    candidates = []
    for info in archive.infolist():
        if info.is_dir():
            continue
        member = PurePosixPath(info.filename.replace("\\", "/"))
        is_activity_file = (
            member.name.casefold() == "myactivity.json"
            or re.sub(r"[\s_-]+", "", member.stem).casefold() == "マイアクティビティ"
        )
        has_gemini_apps_folder = any(
            "gemini" in part.casefold() and ("app" in part.casefold() or "アプリ" in part)
            for part in member.parent.parts
        )
        if is_activity_file and has_gemini_apps_folder:
            candidates.append(info.filename)
    if not candidates:
        raise ValueError("No MyActivity.json found in a Gemini Apps folder in the Takeout ZIP.")
    if len(candidates) > 1:
        raise ValueError("More than one Gemini Apps MyActivity.json was found; pass the ZIP containing only the intended export.")
    member = candidates[0]
    try:
        data = json.loads(archive.read(member).decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read {member} as UTF-8 JSON: {exc}") from exc
    if isinstance(data, dict):
        items = [data]
    elif isinstance(data, list):
        items = [item for item in data if isinstance(item, dict)]
    else:
        raise ValueError("MyActivity.json must contain a JSON object or array.")
    return member, items

def thread_groups(items: Iterable[dict[str, Any]]) -> OrderedDict[str, list[dict[str, Any]]]:
    groups: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
    for item in items:
        if is_prompted(item):
            groups.setdefault(app_id(item), []).append(item)
    # Takeout lists newest activity first; reverse each thread to oldest-first.
    for thread_id in list(groups):
        groups[thread_id] = list(reversed(groups[thread_id]))
    return groups

def build_posts(events: list[dict[str, Any]]) -> list[Post]:
    posts: list[Post] = []
    for item in events:
        stamp = timestamp_text(item.get("time"))
        attachments = attachments_on_item(item)
        user_files = [a for a in attachments if a.role == "user"]
        ai_files = [a for a in attachments if a.role == "assistant"]
        posts.append(Post("User", prompt_text(item), stamp, user_files))
        response = html_to_markdown(assistant_html(item))
        if response or ai_files:
            posts.append(Post("AI", response, stamp, ai_files))
    return posts

def archive_file_index(archive: zipfile.ZipFile, activity_member: str) -> dict[str, zipfile.ZipInfo]:
    """Exact (casefolded) file name -> zip entry, for files beside MyActivity.json."""
    base = PurePosixPath(activity_member.replace("\\", "/")).parent
    by_name: dict[str, zipfile.ZipInfo] = {}
    for info in archive.infolist():
        member = PurePosixPath(info.filename.replace("\\", "/"))
        if info.is_dir() or member.parent != base or member.name.casefold() == "myactivity.json":
            continue
        if stat.S_ISLNK(info.external_attr >> 16):
            continue
        by_name.setdefault(member.name.casefold(), info)
    return by_name

def find_attachment(ref: str, by_name: dict[str, zipfile.ZipInfo]) -> zipfile.ZipInfo | None:
    value = unquote((ref or "").replace("\\", "/").strip())
    if _skip_file_ref(value):
        return None
    return by_name.get(PurePosixPath(value).name.casefold())

def safe_extension(original_name: str, member_name: str) -> str:
    for name in (original_name, member_name):
        suffix = PurePosixPath(name.replace("\\", "/")).suffix.lower()
        if re.fullmatch(r"\.[a-z0-9]{1,10}", suffix):
            return suffix
    return ".bin"

def markdown_link_label(value: str) -> str:
    return (value or "attachment").replace("\\", "\\\\").replace("]", "\\]").replace("\n", " ").replace("\r", " ")

def write_attachment(
    archive: zipfile.ZipFile, info: zipfile.ZipInfo, original_name: str,
    thread_id: str, attachment_dir: Path, next_index: int,
) -> tuple[str, int]:
    output_name = f"{thread_id}_{next_index:04d}{safe_extension(original_name, info.filename)}"
    with archive.open(info, "r") as source, (attachment_dir / output_name).open("wb") as target:
        shutil.copyfileobj(source, target, length=1024 * 1024)
    return output_name, next_index + 1

def post_markdown(
    post: Post, thread_id: str, archive: zipfile.ZipFile, by_name: dict[str, zipfile.ZipInfo],
    attachment_dir: Path, copy_cache: dict[str, str], next_index: list[int],
) -> tuple[str, int]:
    parts = [f"## {post.role} at {post.timestamp or 'unknown time'}", ""]
    if post.body:
        parts.extend((post.body, ""))
    missing = 0
    if post.attachments:
        parts.extend(("Attachments:", ""))
        for attachment in post.attachments:
            info = find_attachment(attachment.source_ref, by_name)
            label = markdown_link_label(attachment.original_name)
            if info is None:
                parts.append(f"- {label} (file missing from Takeout ZIP)")
                missing += 1
                continue
            if info.filename not in copy_cache:
                name, next_index[0] = write_attachment(
                    archive, info, attachment.original_name, thread_id, attachment_dir, next_index[0]
                )
                copy_cache[info.filename] = name
            parts.append(f"- [{label}](../attachments/{copy_cache[info.filename]})")
    return "\n".join(parts).rstrip(), missing

def metadata_value(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)  # JSON quoted scalar is valid YAML 1.2

def export_zip(takeout_zip: Path, output_dir: Path) -> dict[str, int]:
    if not takeout_zip.is_file():
        raise ValueError(f"Takeout ZIP does not exist: {takeout_zip}")
    if takeout_zip.suffix.lower() != ".zip" or not zipfile.is_zipfile(takeout_zip):
        raise ValueError("--takeoutzip must point to a valid .zip file.")

    with zipfile.ZipFile(takeout_zip, "r") as archive:
        activity_member, items = load_activity(archive)
        groups = thread_groups(items)
        if not groups:
            raise ValueError("MyActivity.json contains no Prompted Gemini chat items with app URLs.")

        log_dir, attachment_dir = output_dir / "log", output_dir / "attachments"
        log_dir.mkdir(parents=True, exist_ok=True)
        attachment_dir.mkdir(parents=True, exist_ok=True)
        by_name = archive_file_index(archive, activity_member)

        catalog_rows: list[dict[str, str | int]] = []
        copied_attachments = missing_attachments = 0
        for thread_id, events in groups.items():
            posts = build_posts(events)
            dated = [when for when, _ in ((iso_datetime(e.get("time")), e) for e in events) if when]
            first_dt, last_dt = (min(dated), max(dated)) if dated else (None, None)
            first_text = first_dt.isoformat(sep=" ", timespec="seconds") if first_dt else ""
            last_text = last_dt.isoformat(sep=" ", timespec="seconds") if last_dt else ""
            title = first_line_title(prompt_text(events[0]) if events else "")
            original_url = f"https://gemini.google.com/app/{thread_id}"
            stamp = first_dt.strftime("%Y%m%d_%H%M%S") if first_dt else "undated"
            md_name = f"{stamp}_{thread_id}.md"

            body_parts = [
                "---",
                f"thread_title: {metadata_value(title)}",
                f"first_post_datetime: {metadata_value(first_text)}",
                f"last_post_datetime: {metadata_value(last_text)}",
                f"original_url: {metadata_value(original_url)}",
                "---", "",
            ]
            copy_cache: dict[str, str] = {}
            next_index = [1]
            post_count = 0
            for post in posts:
                rendered, missing = post_markdown(post, thread_id, archive, by_name, attachment_dir, copy_cache, next_index)
                if rendered:
                    body_parts.extend((rendered, ""))
                    post_count += 1
                missing_attachments += missing
            copied_attachments += len(copy_cache)
            (log_dir / md_name).write_text("\n".join(body_parts).rstrip() + "\n", encoding="utf-8", newline="\n")
            catalog_rows.append({
                "md_file": f"log/{md_name}",
                "thread_title": title,
                "post_count": post_count,
                "first_post_datetime": first_text,
                "last_post_datetime": last_text,
                "original_url": original_url,
            })

        csv_path = output_dir / f"{takeout_zip.stem}.csv"
        with csv_path.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=CSV_COLUMNS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(catalog_rows)

    return {
        "threads": len(catalog_rows),
        "posts": sum(int(row["post_count"]) for row in catalog_rows),
        "attachments_copied": copied_attachments,
        "attachment_refs_missing": missing_attachments,
    }

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Convert a Google Takeout ZIP's Gemini Apps history to Markdown.")
    parser.add_argument("--takeoutzip", required=True, type=Path, help="Google Takeout ZIP containing Gemini Apps/MyActivity.json")
    parser.add_argument("--out", type=Path, default=Path.cwd() / "gemini_log", help="Output folder (default: ./gemini_log)")
    args = parser.parse_args(argv)
    if BeautifulSoup is None:
        print("ERROR: Beautiful Soup is required. Install it with: python -m pip install beautifulsoup4", file=sys.stderr)
        return 2
    try:
        result = export_zip(args.takeoutzip.expanduser().resolve(), args.out.expanduser().resolve())
    except (OSError, zipfile.BadZipFile, ValueError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(
        f"Export complete: {result['threads']} threads, {result['posts']} posts, "
        f"{result['attachments_copied']} attachments copied, {result['attachment_refs_missing']} attachment references missing."
    )
    print(f"Output: {args.out.expanduser().resolve()}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
