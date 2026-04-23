#!/usr/bin/env python3
"""Writer (GitHub Actions 진입점).

정책/생활정보 심층 재구성 블로그. candidates score 상위 3 중 최신 1건 선택 →
Claude API 1회 호출 → HTML 저장 → inline WP 발행 → candidates 제거 → git push → Slack.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import requests

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import (
    CANDIDATES_PATH,
    WRITER_ARTICLES_PER_RUN,
    WRITER_TARGET_WORD_COUNT_MAX,
    WRITER_TARGET_WORD_COUNT_MIN,
)
from scripts.publish_drafts import publish_single_draft
from src.anthropic_helper import call_json, estimate_cost_usd

SLACK_WEBHOOK = os.environ.get("SLACK_WEBHOOK_URL", "")
DRAFTS_DIR = PROJECT_ROOT / "data" / "drafts"
DRAFTS_DIR.mkdir(parents=True, exist_ok=True)

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


SYSTEM_PROMPT_BASE = f"""당신은 publickorea.org (한국 정부정책·생활정보 블로그) 의 전문 에디터입니다.

## 목표

원문 기사를 읽기 쉬운 **심층 재구성 블로그 포스트** 로 재가공합니다. 번역이 아니라 **재구성** — 독자(일반 국민) 가 실제로 혜택을 활용할 수 있게 정보를 재배열·보강합니다.

## 작성 원칙 (중요)

1. **원문을 그대로 베껴쓰지 말 것**. 독자 관점으로 리드를 전환, 실용 정보 중심으로 섹션 재배열.
2. **불확실한 숫자/날짜/인명/기관명은 발명 금지**. 원문에 없으면 "자세한 조건은 {{기관명}} 에서 확인하세요" 로 회피.
3. **일반 지식 범위의 보강은 허용**: 공공기관 대표번호 (예: 국세상담센터 126, 한국장학재단 1599-2000), 공식 홈페이지 도메인 (예: hometax.go.kr, bokjiro.go.kr) 같이 **안정적인 공개 정보** 는 기억나는 선에서 포함 가능. 확신 없으면 생략.
4. **WebSearch/외부 검색 불가** — 주어진 원문과 내재 지식만 사용.

## 출력 스키마

**raw JSON 1개만 반환** (마크다운 코드블록 금지). 필드:

```
{{
  "title": "28~34자 제목. 메인 키워드 앞 15자 안에 배치",
  "slug": "english-slug; 3~6 단어; 소문자 하이픈",
  "meta_desc": "110~140자; 메인 키워드 + 구체 숫자/날짜",
  "tags": ["3~5개: 제도명/혜택유형/대상/기관"],
  "featured_alt": "이미지 alt; 키워드 포함 짧게",
  "body_html": "완성된 HTML 문자열. 아래 구조와 스타일 그대로 사용"
}}
```

## body_html 구조 (필수, 총 {WRITER_TARGET_WORD_COUNT_MIN}~{WRITER_TARGET_WORD_COUNT_MAX}단어)

1. **리드 문단 1개** — 메인 키워드 + 숫자/날짜. 2~3문장.
2. **핵심 요약 인포박스 1개** — 3~4줄 불릿.
3. **본론 섹션 2~4개** — 각각 `<h2>` + 2~3문단. 제도 배경, 대상/조건, 신청 방법, 주의사항 중 주제에 맞게.
4. **FAQ 3개** — `<details>` 태그.
5. **한 줄 정리** — 마지막에 행동 요약 1~2문장.

HTML 스타일은 **프로젝트 스타일 가이드를 정확히 따를 것** (아래 별도 섹션 참조).

## 톤

- **존댓말 ~요/~세요 종결**. 공문체 금지, 과장/감탄 금지.
- 광고성 문구 금지 ("놓치지 마세요!", "꿀팁!", 느낌표 남발).
- 독자에게 정중하되 친근."""


def build_system_prompt() -> str:
    guide = load_style_guide()
    if guide:
        return (
            SYSTEM_PROMPT_BASE
            + "\n\n---\n\n## PROJECT STYLE GUIDE (MANDATORY — 이대로 따를 것)\n\n"
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


def pick_one_candidate(items: list[dict]) -> dict | None:
    """score 상위 3건 중 collected_at 최신 1건 선택 (사용자 Routine 원본 로직)."""
    if not items:
        return None
    top3 = sorted(items, key=lambda x: x.get("score", 0), reverse=True)[:3]
    return sorted(top3, key=lambda x: x.get("collected_at", ""), reverse=True)[0]


def build_user_message(item: dict) -> str:
    return (
        "## 원문 기사 (한국어)\n\n"
        f"**출처 URL**: {item.get('link', '')}\n"
        f"**원문 제목**: {item.get('title', '')}\n"
        f"**발행일**: {item.get('published', '')}\n"
        f"**이미지 URL**: {item.get('thumbnail_url') or '(없음)'}\n"
        f"**평가 점수**: {item.get('score', 0)}\n\n"
        f"**요약**:\n{item.get('summary', '')}\n\n"
        f"**본문**:\n{item.get('content', '')}\n\n"
        "---\n\n"
        "위 스키마에 맞게 JSON 1개만 반환하세요. 마크다운 코드블록 금지."
    )


def assemble_final_html(article: dict, source_item: dict) -> str:
    """Claude JSON 결과 + 원본 메타 → 최종 HTML (주석 헤더 + 썸네일 + body + 출처)."""
    created_at = datetime.now().isoformat()
    source_url = source_item.get("link", "")
    thumbnail = source_item.get("thumbnail_url") or ""
    tags = ", ".join(article.get("tags", []))

    header = (
        "<!--\n"
        f"Title: {article['title']}\n"
        f"Slug: {article['slug']}\n"
        f"Meta: {article['meta_desc']}\n"
        f"Tags: {tags}\n"
        f"Score: {source_item.get('score', 0)}\n"
        f"OriginalTitle: {source_item.get('title', '')}\n"
        f"OriginalURL: {source_url}\n"
        f"ImageSourceURL: {thumbnail}\n"
        f"FeaturedAlt: {article.get('featured_alt', '')}\n"
        f"Created: {created_at}\n"
        "-->\n\n"
    )

    parts = [header]

    # 썸네일 (있으면 맨 위)
    if thumbnail:
        alt = article.get("featured_alt") or article.get("title", "")
        parts.append(
            f'<figure style="margin:0 0 24px 0;">'
            f'<img src="{thumbnail}" alt="{alt}" style="width:100%;border-radius:8px;"/>'
            f"</figure>\n\n"
        )

    # Claude 가 작성한 body_html 삽입
    parts.append(article.get("body_html", "").strip() + "\n\n")

    # 출처 링크
    if source_url:
        original_title = source_item.get("title", source_url)
        parts.append(
            f'<p style="margin-top:32px;font-size:0.9em;color:#64748B;">'
            f'출처: <a href="{source_url}" target="_blank" rel="noopener">'
            f"{original_title}</a></p>\n"
        )

    return "".join(parts)


def git_commit_push(message: str) -> bool:
    try:
        subprocess.run(["git", "add", "data/"], check=True, cwd=PROJECT_ROOT)
        result = subprocess.run(
            ["git", "diff", "--staged", "--quiet"],
            cwd=PROJECT_ROOT,
        )
        if result.returncode == 0:
            logger.info("변경사항 없음 (commit 생략)")
            return True
        subprocess.run(
            ["git", "commit", "-m", message],
            check=True,
            cwd=PROJECT_ROOT,
        )
        subprocess.run(
            ["git", "pull", "--rebase", "origin", "main"],
            check=True,
            cwd=PROJECT_ROOT,
        )
        subprocess.run(["git", "push", "origin", "main"], check=True, cwd=PROJECT_ROOT)
        return True
    except subprocess.CalledProcessError as e:
        logger.error(f"git 실패: {e}")
        return False


def process_one_candidate(candidate: dict, idx: int, total: int) -> dict:
    title_original = candidate.get("title", "(no title)")[:60]
    logger.info(
        f"[{idx}/{total}] 시작: [{candidate.get('score', 0)}점] {title_original}"
    )

    try:
        article, api_meta = call_json(
            system=build_system_prompt(),
            user=build_user_message(candidate),
            max_tokens=4500,
            temperature=0.4,
        )
    except Exception as e:
        logger.error(f"API 실패: {e}")
        return {
            "status": "api_failed",
            "title": title_original,
            "error": str(e)[:300],
            "api_meta": None,
        }

    required = ["title", "slug", "meta_desc", "body_html"]
    missing = [k for k in required if not article.get(k)]
    if missing:
        logger.error(f"응답 필드 누락: {missing}")
        return {
            "status": "schema_failed",
            "title": title_original,
            "error": f"응답 필드 누락: {missing}",
            "api_meta": api_meta,
        }

    slug = re.sub(r"[^a-z0-9-]", "", article["slug"].lower())
    if not slug:
        return {
            "status": "schema_failed",
            "title": title_original,
            "error": f"slug 정규화 실패 ({article['slug']})",
            "api_meta": api_meta,
        }
    article["slug"] = slug

    today = datetime.now().strftime("%Y-%m-%d")
    filename = f"{today}-{slug}.html"
    draft_path = DRAFTS_DIR / filename
    if draft_path.exists():
        i = 2
        while (DRAFTS_DIR / f"{today}-{slug}-{i}.html").exists():
            i += 1
        filename = f"{today}-{slug}-{i}.html"
        draft_path = DRAFTS_DIR / filename

    html = assemble_final_html(article, candidate)
    draft_path.write_text(html, encoding="utf-8")
    logger.info(f"draft 저장: {filename} ({len(html)} chars)")

    pub_result = publish_single_draft(draft_path)
    pub_status = pub_result.get("status")
    logger.info(f"publish 결과: {pub_status}")

    candidates_data = load_json(CANDIDATES_PATH, {"last_updated": "", "items": []})
    items = candidates_data.get("items", [])
    remaining = [i for i in items if i.get("guid") != candidate.get("guid")]
    save_json(CANDIDATES_PATH, {
        "last_updated": datetime.now().isoformat(),
        "items": remaining,
    })

    commit_msg = {
        "success": f"publish: {article['title']}",
        "failed": f"draft (publish fail): {article['title']}",
    }.get(pub_status, f"draft: {article['title']}")
    git_commit_push(commit_msg)

    return {
        "status": pub_status,
        "title": article["title"],
        "original_title": title_original,
        "slug": slug,
        "link": pub_result.get("link"),
        "post_id": pub_result.get("post_id"),
        "error": pub_result.get("error"),
        "api_meta": api_meta,
        "candidates_remaining": len(remaining),
    }


def main() -> int:
    logger.info("=" * 50)
    logger.info(
        f"Writer 시작: {datetime.now().isoformat()} (per-run 목표: {WRITER_ARTICLES_PER_RUN}편)"
    )

    try:
        subprocess.run(
            ["git", "config", "user.email", "writer@blog-automation.local"],
            check=True, cwd=PROJECT_ROOT,
        )
        subprocess.run(
            ["git", "config", "user.name", "blog-writer"],
            check=True, cwd=PROJECT_ROOT,
        )
    except subprocess.CalledProcessError:
        pass

    candidates_data = load_json(CANDIDATES_PATH, {"last_updated": "", "items": []})
    items = candidates_data.get("items", [])
    logger.info(f"candidates: {len(items)}건")

    if not items:
        slack_notify("⏸️ *Writer*: 작성 후보 없음")
        return 0

    # 1건 선택 (상위 3 중 최신)
    selected = pick_one_candidate(items)
    if not selected:
        slack_notify("⏸️ *Writer*: 작성 후보 없음")
        return 0

    result = process_one_candidate(selected, 1, 1)

    api_meta = result.get("api_meta") or {}
    total_in = api_meta.get("input_tokens", 0)
    total_out = api_meta.get("output_tokens", 0)
    model_used = api_meta.get("model", "?")
    cost = estimate_cost_usd(total_in, total_out, model_used)

    status = result["status"]
    if status == "success":
        slack_notify(
            f"✍️📝 *작성+발행 완료*\n"
            f"제목: {result['title']}\n"
            f"WordPress: {result.get('link', '-')}\n"
            f"candidates 남음: {result.get('candidates_remaining', '?')}건\n"
            f"모델: {model_used} | 토큰 {total_in}in/{total_out}out | 약 ${cost:.4f}"
        )
        return 0
    elif status in ("failed", "api_failed", "schema_failed"):
        slack_notify(
            f"❌ *Writer 실패*\n"
            f"제목: {result.get('title', result.get('original_title', '-'))}\n"
            f"에러: {str(result.get('error', ''))[:300]}"
        )
        return 1
    else:
        slack_notify(f"⚠️ *Writer 종료 (status={status})*: {result.get('title', '-')}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
