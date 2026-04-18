"""data/drafts/*.html → WordPress REST API 발행
GitHub Actions에서 실행. 성공 시 data/drafts/published/ 로 이동.
"""
import base64
import os
import re
import shutil
import sys
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
        html = path.read_text(encoding="utf-8")
        title = extract_title(html, path.name)
        meta_desc = extract_meta_desc(html)
        slug = extract_slug(html, path.name)
        try:
            post_id, link = publish_to_wp(title, html, meta_desc, slug)
            dest = PUBLISHED_DIR / path.name
            shutil.move(str(path), str(dest))
            success.append({"title": title, "link": link, "file": path.name})
            slack_notify(
                f"📝 *새 글 공개 발행*\n제목: {title}\nWordPress: {link}"
            )
            print(f"OK: {path.name} → {link}")
        except requests.HTTPError as e:
            err = f"HTTP {e.response.status_code}: {e.response.text[:300]}"
            failures.append({"file": path.name, "error": err})
            slack_notify(f"❌ *WordPress 발행 실패*\n파일: {path.name}\n에러: {err}")
            print(f"FAIL: {path.name} - {err}", file=sys.stderr)
        except Exception as e:
            failures.append({"file": path.name, "error": str(e)})
            slack_notify(f"❌ *WordPress 발행 실패*\n파일: {path.name}\n에러: {e}")
            print(f"FAIL: {path.name} - {e}", file=sys.stderr)

    slack_notify(
        f"📊 *발행 요약*\n성공: {len(success)}건\n실패: {len(failures)}건"
    )
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
