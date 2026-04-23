#!/usr/bin/env python3
"""Reviewer (GitHub Actions 진입점).

최근 24시간 발행글 → Anthropic API 사후 감사 (중복/팩트/가독성/SEO) →
FIX 액션을 review-actions.json 에 append → review-apply workflow 가 WP 반영.
"""
from __future__ import annotations

import glob
import json
import logging
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.anthropic_helper import call_json, estimate_cost_usd

SLACK_WEBHOOK = os.environ.get("SLACK_WEBHOOK_URL", "")
REVIEW_ACTIONS_PATH = PROJECT_ROOT / "data" / "review-actions.json"
PUBLISHED_DIR = PROJECT_ROOT / "data" / "drafts" / "published"

REVIEW_WINDOW_HOURS = 24
DEDUP_LOOKBACK_DAYS = 7

BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
COMMENT_META_RE = re.compile(r"<!--(.*?)-->", re.DOTALL)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


STYLE_GUIDE_PATH = PROJECT_ROOT / "config" / "style_guide.md"


def load_style_guide() -> str:
    if STYLE_GUIDE_PATH.exists():
        return STYLE_GUIDE_PATH.read_text(encoding="utf-8")
    return ""


SYSTEM_PROMPT_BASE = """당신은 한국 정부정책·생활정보 블로그 publickorea.org 의 사후 감수자입니다. 방금 발행된 한 편의 글을 4가지 기준으로 검토하고 적절한 액션을 결정합니다.

## 검토 기준

1. **중복**: 최근 7일 내 발행글과 주제/대상/결론이 거의 동일한가?
2. **팩트 정확성**: 원문 요약과 비교해 숫자·날짜·기관명·제도명에 오류 또는 날조가 있는가?
3. **가독성**: 한국어 맞춤법, 띄어쓰기, 어색한 문장, 반복/누락.
4. **SEO 및 스타일 가이드 준수**: 제목 28~34자, 메타 110~140자, 슬러그 형식, 핵심요약 박스/H2/FAQ 구조 여부, 존댓말 유지.

## 출력

**raw JSON 1개만** 반환 (마크다운 코드블록 금지). 스키마:

```
{
  "action": "fix" | "notify" | "pass",
  "reason": "한 줄 요약 (한국어)",
  "issues": ["상세 이슈 1 (한국어)", "상세 이슈 2 (한국어)"],
  "new_content": "(action=fix 일 때만) 수정된 전체 HTML 본문. 주석 헤더 없이 body 만. 한국어로.",
  "new_meta_desc": "(선택, 메타가 바뀔 때만) 한국어, 110~140자",
  "new_title": "(선택, 제목이 바뀔 때만) 한국어, 28~34자"
}
```

## 액션 규칙

- `fix`: 맞춤법/가독성/SEO 규칙 위반 같은 **경미한 수정**. `new_content` 에 수정된 HTML 제공. 팩트 바꾸지 말 것.
- `notify`: 팩트 오류 의심, 중복 확실. 사람 판단 필요. `new_content` 안 보냄. `issues` 에 구체적 근거.
- `pass`: 문제 없음.

**보수적으로 판단**: 애매하면 `pass`. `new_content` 안에 원문에 없는 사실을 새로 만들지 말 것.
"""


def build_system_prompt() -> str:
    guide = load_style_guide()
    if guide:
        return (
            SYSTEM_PROMPT_BASE
            + "\n\n---\n\n## PROJECT STYLE GUIDE (준수 여부 체크)\n\n"
            + guide
        )
    return SYSTEM_PROMPT_BASE


def load_json(path: Path, default=None):
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default if default is not None else []


def save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def slack_notify(text: str) -> None:
    if not SLACK_WEBHOOK:
        return
    try:
        requests.post(SLACK_WEBHOOK, json={"text": text}, timeout=10)
    except Exception as e:
        logger.warning(f"Slack 알림 실패: {e}")


def parse_comment_meta(html: str) -> dict[str, str]:
    meta = {}
    for m in COMMENT_META_RE.finditer(html):
        for line in m.group(1).splitlines():
            if ":" in line:
                k, _, v = line.partition(":")
                k = k.strip()
                v = v.strip()
                if k and v:
                    meta[k.lower()] = v
    return meta


def fetch_source_summary(url: str, max_chars: int = 2000) -> str | None:
    if not url or not url.startswith("http"):
        return None
    try:
        from bs4 import BeautifulSoup
        resp = requests.get(url, timeout=15, headers={"User-Agent": BROWSER_UA})
        resp.raise_for_status()
        resp.encoding = resp.apparent_encoding
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup.select("script, style, nav, footer, aside, .ad"):
            tag.decompose()
        text = soup.get_text(separator="\n", strip=True)
        lines = [ln for ln in text.splitlines() if ln.strip()]
        return "\n".join(lines)[:max_chars]
    except Exception as e:
        logger.warning(f"원문 fetch 실패 ({url}): {e}")
        return None


def collect_recent_titles(exclude_path: Path) -> list[str]:
    """최근 7일 내 published 글 제목 (자신 제외)."""
    cutoff = time.time() - DEDUP_LOOKBACK_DAYS * 86400
    titles = []
    for html_path in PUBLISHED_DIR.glob("*.html"):
        if html_path == exclude_path:
            continue
        if html_path.stat().st_mtime < cutoff:
            continue
        text = html_path.read_text(encoding="utf-8")[:3000]
        meta = parse_comment_meta(text)
        if t := meta.get("title"):
            titles.append(t)
    return titles


def strip_comment_headers(html: str) -> str:
    """주석 블록 전부 제거 (Reviewer 에게는 body 만 보여준다)."""
    return COMMENT_META_RE.sub("", html).strip()


def build_review_input(
    article_body: str,
    article_meta: dict,
    source_summary: str | None,
    recent_titles: list[str],
) -> str:
    return (
        "## 검토 대상 글\n\n"
        f"**제목**: {article_meta.get('title', '')}\n"
        f"**메타**: {article_meta.get('meta', '')}\n"
        f"**슬러그**: {article_meta.get('slug', '')}\n"
        f"**원문 제목**: {article_meta.get('originaltitle', '')}\n"
        f"**원문 URL**: {article_meta.get('originalurl', '')}\n\n"
        "### 본문 HTML\n\n"
        f"{article_body[:8000]}\n\n"
        "### 원문 본문 발췌 (팩트 대조용)\n\n"
        f"{source_summary or '(원문 fetch 실패 — 팩트체크 스킵)'}\n\n"
        "### 최근 7일 발행 제목 (중복 체크용)\n\n"
        + "\n".join(f"- {t}" for t in recent_titles[:30])
        + "\n\n위 스키마에 맞게 JSON 만 반환하세요."
    )


def review_one_file(filepath: Path, recent_titles: list[str]) -> dict:
    html = filepath.read_text(encoding="utf-8")
    meta = parse_comment_meta(html)
    body_only = strip_comment_headers(html)

    source_url = meta.get("originalurl", "")
    source_summary = fetch_source_summary(source_url) if source_url else None

    try:
        result, api_meta = call_json(
            system=build_system_prompt(),
            user=build_review_input(body_only, meta, source_summary, recent_titles),
            max_tokens=4500,
            temperature=0.2,
        )
    except Exception as e:
        logger.error(f"API 호출 실패 ({filepath.name}): {e}")
        return {
            "_file": filepath.name,
            "action": "error",
            "reason": str(e)[:200],
            "_api_meta": {"input_tokens": 0, "output_tokens": 0, "model": "?"},
        }

    result["_file"] = filepath.name
    result["_meta"] = meta
    result["_api_meta"] = api_meta
    return result


def main() -> int:
    logger.info("=" * 50)
    logger.info(f"Reviewer 시작: {datetime.now().isoformat()}")

    if not PUBLISHED_DIR.exists():
        logger.info("published 디렉토리 없음. 종료.")
        slack_notify("🔍 *Reviewer*: published 디렉토리 없음")
        return 0

    cutoff = time.time() - REVIEW_WINDOW_HOURS * 3600
    files = sorted(
        (p for p in PUBLISHED_DIR.glob("*.html") if p.stat().st_mtime >= cutoff),
        key=lambda p: p.stat().st_mtime,
    )
    logger.info(f"최근 {REVIEW_WINDOW_HOURS}h 발행글: {len(files)}건")

    if not files:
        slack_notify(f"🔍 *Reviewer*: 최근 {REVIEW_WINDOW_HOURS}h 발행글 없음")
        return 0

    actions_data = load_json(REVIEW_ACTIONS_PATH, {"last_updated": "", "actions": []})
    existing_actions = actions_data.get("actions", [])

    pass_count = 0
    fix_actions = []
    notify_messages = []
    error_count = 0
    total_in = total_out = 0
    model_used = "?"

    for filepath in files:
        logger.info(f"검토 중: {filepath.name}")
        recent_titles = collect_recent_titles(filepath)
        result = review_one_file(filepath, recent_titles)

        api_meta = result.get("_api_meta", {})
        total_in += api_meta.get("input_tokens", 0)
        total_out += api_meta.get("output_tokens", 0)
        model_used = api_meta.get("model", model_used)

        action = result.get("action", "pass")
        reason = result.get("reason", "")
        meta = result.get("_meta", {})

        if action == "error":
            error_count += 1
        elif action == "pass":
            pass_count += 1
            logger.info(f"  PASS: {reason}")
        elif action == "fix":
            fix_entry = {
                "slug": meta.get("slug"),
                "action": "fix",
                "reason": reason,
                "source_file": filepath.name,
            }
            if post_id := meta.get("wppostid"):
                try:
                    fix_entry["post_id"] = int(post_id)
                except ValueError:
                    pass
            if nc := result.get("new_content"):
                fix_entry["new_content"] = nc
            if nmd := result.get("new_meta_desc"):
                fix_entry["new_meta_desc"] = nmd
            if nt := result.get("new_title"):
                fix_entry["new_title"] = nt
            fix_actions.append(fix_entry)
            logger.info(f"  FIX: {reason}")
        elif action == "notify":
            issues = result.get("issues", [])
            notify_messages.append({
                "title": meta.get("title", filepath.name),
                "reason": reason,
                "issues": issues,
            })
            logger.warning(f"  NOTIFY: {reason}")

    if fix_actions:
        existing_actions.extend(fix_actions)
        save_json(REVIEW_ACTIONS_PATH, {
            "last_updated": datetime.now().isoformat(),
            "actions": existing_actions,
        })

    cost = estimate_cost_usd(total_in, total_out, model_used)

    lines = [
        f"🔍 *Reviewer 완료 (최근 {REVIEW_WINDOW_HOURS}h {len(files)}건)*",
        f"PASS: {pass_count} | FIX: {len(fix_actions)} | NOTIFY: {len(notify_messages)} | ERROR: {error_count}",
    ]
    if fix_actions:
        lines.append("\n*FIX (자동 수정 지시 → 잠시 후 WP 반영)*")
        for f in fix_actions[:5]:
            lines.append(f"• {f['slug']}: {f['reason']}")
    if notify_messages:
        lines.append("\n⚠️ *NOTIFY (사람 판단 필요)*")
        for n in notify_messages[:5]:
            lines.append(f"• {n['title'][:50]}: {n['reason']}")
            for issue in n.get("issues", [])[:2]:
                lines.append(f"  - {issue}")
    lines.append(f"\n모델: {model_used} | 토큰 {total_in}in/{total_out}out | 약 ${cost:.4f}")
    slack_notify("\n".join(lines))

    logger.info(
        f"Reviewer 완료: PASS {pass_count}, FIX {len(fix_actions)}, "
        f"NOTIFY {len(notify_messages)}, ERROR {error_count}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
