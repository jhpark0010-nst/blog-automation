"""data/drafts/*.html → WordPress REST API 발행
GitHub Actions에서 실행. 성공 시 data/drafts/published/ 로 이동.

두 경로로 호출됨:
1. `python scripts/publish_drafts.py` → main() 이 drafts/*.html 전부 순차 발행 (fallback/legacy workflow).
2. `from scripts.publish_drafts import publish_single_draft` → Writer inline 발행.
"""
import base64
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

WP_URL = os.environ["WP_URL"].rstrip("/")
WP_USERNAME = os.environ["WP_USERNAME"]
WP_APP_PASSWORD = os.environ["WP_APP_PASSWORD"]
SLACK_WEBHOOK = os.environ.get("SLACK_WEBHOOK_URL", "")

DRAFTS_DIR = Path("data/drafts")
PUBLISHED_DIR = DRAFTS_DIR / "published"
PUBLISHED_DIR.mkdir(parents=True, exist_ok=True)

DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}-")
COMMENT_META_RE = re.compile(r"<!--(.*?)-->", re.DOTALL)


def _parse_comment_meta(html: str) -> dict[str, str]:
    """HTML 상단 주석에서 'Title: ...', 'Meta: ...' 등 메타데이터 추출"""
    m = COMMENT_META_RE.search(html)
    if not m:
        return {}
    meta = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            k = k.strip()
            v = v.strip()
            if k and v:
                meta[k.lower()] = v
    return meta


def extract_title(html: str, filename: str) -> str:
    meta = _parse_comment_meta(html)
    if t := meta.get("title"):
        return t
    soup = BeautifulSoup(html, "html.parser")
    if (t := soup.find("title")) and t.get_text(strip=True):
        return t.get_text(strip=True)
    if (h1 := soup.find("h1")) and h1.get_text(strip=True):
        return h1.get_text(strip=True)
    return filename.removesuffix(".html").replace("-", " ")


def extract_meta_desc(html: str) -> str:
    meta = _parse_comment_meta(html)
    if m := meta.get("meta"):
        return m
    soup = BeautifulSoup(html, "html.parser")
    m = soup.find("meta", attrs={"name": "description"})
    if m and m.get("content"):
        return m["content"]
    p = soup.find("p")
    return p.get_text(strip=True)[:150] if p else ""


def extract_slug(html: str, filename: str) -> str:
    """주석의 Slug 필드 우선, 없으면 파일명에서 날짜 prefix(YYYY-MM-DD-) 제거"""
    meta = _parse_comment_meta(html)
    if s := meta.get("slug"):
        return s.lower().strip()
    stem = filename.removesuffix(".html")
    return DATE_PATTERN.sub("", stem).lower()


def publish_to_wp(title: str, content: str, meta_desc: str, slug: str):
    auth = base64.b64encode(f"{WP_USERNAME}:{WP_APP_PASSWORD}".encode()).decode()
    payload = {"title": title, "content": content, "status": "publish", "slug": slug}
    if meta_desc:
        payload["meta"] = {"yoast_wpseo_metadesc": meta_desc}
    resp = requests.post(
        f"{WP_URL}/wp-json/wp/v2/posts",
        headers={"Authorization": f"Basic {auth}", "Content-Type": "application/json"},
        json=payload,
        timeout=30,
    )
    resp.raise_for_status()
    d = resp.json()
    return d["id"], d["link"]


def slack_notify(text: str) -> None:
    if not SLACK_WEBHOOK:
        return
    try:
        requests.post(SLACK_WEBHOOK, json={"text": text}, timeout=10)
    except Exception as e:
        print(f"Slack 알림 실패: {e}", file=sys.stderr)


def append_publish_comment(html: str, post_id: int, link: str) -> str:
    """발행 성공 후 HTML 끝에 WP 정보 주석 추가. Reviewer 가 post_id 로 WP 수정/삭제."""
    comment = (
        "\n\n<!-- WP 발행 정보\n"
        f"WpPostId: {post_id}\n"
        f"WpLink: {link}\n"
        f"PublishedAt: {datetime.now().isoformat()}\n"
        "-->\n"
    )
    return html + comment


def publish_single_draft(path: Path) -> dict:
    """단일 draft 파일을 WP 발행 + published/ 이동까지 처리.

    Slack 알림은 호출자가 결정 (inline Writer 에서는 Writer 가 묶어서 알림).

    Returns dict with:
      - status: "success" | "failed"
      - title, file (always)
      - link, post_id (success only)
      - error (failed only)
    """
    html = path.read_text(encoding="utf-8")
    title = extract_title(html, path.name)
    meta_desc = extract_meta_desc(html)
    slug = extract_slug(html, path.name)

    try:
        post_id, link = publish_to_wp(title, html, meta_desc, slug)
        final_html = append_publish_comment(html, post_id, link)
        dest = PUBLISHED_DIR / path.name
        dest.write_text(final_html, encoding="utf-8")
        path.unlink()
        return {
            "status": "success",
            "title": title,
            "file": path.name,
            "link": link,
            "post_id": post_id,
        }
    except requests.HTTPError as e:
        return {
            "status": "failed",
            "title": title,
            "file": path.name,
            "error": f"HTTP {e.response.status_code}: {e.response.text[:300]}",
        }
    except Exception as e:
        return {
            "status": "failed",
            "title": title,
            "file": path.name,
            "error": str(e),
        }


def main() -> int:
    files = sorted(
        f for f in DRAFTS_DIR.glob("*.html") if DATE_PATTERN.match(f.name)
    )
    if not files:
        print("발행할 초안 없음")
        return 0

    print(f"발행 대상 {len(files)}건")
    success, failures = [], []

    for path in files:
        result = publish_single_draft(path)
        if result["status"] == "success":
            success.append(result)
            slack_notify(
                f"📝 *새 글 공개 발행*\n제목: {result['title']}\nWordPress: {result['link']}"
            )
            print(f"OK: {result['file']} → {result['link']}")
        else:
            failures.append(result)
            slack_notify(
                f"❌ *WordPress 발행 실패*\n파일: {result['file']}\n에러: {result['error']}"
            )
            print(f"FAIL: {result['file']} - {result['error']}", file=sys.stderr)

    slack_notify(
        f"📊 *발행 요약*\n성공: {len(success)}건\n실패: {len(failures)}건"
    )
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
