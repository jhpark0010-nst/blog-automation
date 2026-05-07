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
from src.content_filter import is_similar

SLACK_WEBHOOK = os.environ.get("SLACK_WEBHOOK_URL", "")
DRAFTS_DIR = PROJECT_ROOT / "data" / "drafts"
PUBLISHED_DIR = DRAFTS_DIR / "published"
DRAFTS_DIR.mkdir(parents=True, exist_ok=True)

# 발행 직전 중복 체크 윈도우 (일). collect 단계 dedup 이 잡지 못한
# "candidates 머무는 사이 발행된 유사글" 케이스 방어.
WRITER_DEDUP_DAYS = 7

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
2. **원문의 모든 핵심 사실을 빠짐없이 담을 것 (필수)**. 다음 항목들은 원문에 등장하면 **반드시 본문에 반영**:
   - 모든 숫자/금액/비율/연령/기간 (예: "월 5천 원", "최대 4년", "만 65세")
   - 모든 날짜 (시행일, 신청 마감, 발표일 등)
   - 모든 대상 그룹 또는 차등 구간 (예: "비수도권 / 인구감소 우대지역 / 특별지역" 같은 카테고리는 반드시 풀어쓰기)
   - 신청처/문의처/홈페이지 URL/대표번호
   - 적용 조건과 예외 사항
   원문이 여러 그룹·구간·카테고리를 표·열거로 다루면 본문에 `<ol>` 또는 `<ul>` 로 그대로 옮겨야 합니다. 일부 누락은 정보 손실이며 글의 가치를 떨어뜨립니다.
3. **불확실한 숫자/날짜/인명/기관명은 발명 금지**. 원문에 없으면 "자세한 조건은 {{기관명}} 에서 확인하세요" 로 회피.
4. **일반 지식 범위의 보강은 허용**: 공공기관 대표번호 (예: 국세상담센터 126, 한국장학재단 1599-2000), 공식 홈페이지 도메인 (예: hometax.go.kr, bokjiro.go.kr) 같이 **안정적인 공개 정보** 는 기억나는 선에서 포함 가능. 확신 없으면 생략.
5. **WebSearch/외부 검색 불가** — 주어진 원문과 내재 지식만 사용.

## 출력 스키마

**raw JSON 1개만 반환**. 응답의 첫 글자는 반드시 `{{` 이고 마지막 글자는 `}}` 입니다. 마크다운 코드블록 금지, 자연어 설명 금지 (예: "I need to work with..."), 응답 앞뒤 어떤 prefix/suffix 도 없이 순수 JSON 객체만. 필드:

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

## body_html 구조 (필수, 총 {WRITER_TARGET_WORD_COUNT_MIN}~{WRITER_TARGET_WORD_COUNT_MAX} 어절 = 한국어 단어 ≈ 약 1300~2000자)

**최소 분량 {WRITER_TARGET_WORD_COUNT_MIN} 어절은 강제 사항입니다**. 미달이면 글 가치 부족으로 판정돼 수정 대상이 됩니다. 작성 후 어절 수가 모자라면 다음 중 하나로 보강하세요:

- 빠뜨린 원문 사실 (위 작성 원칙 2번) 을 추가 H2 섹션으로 풀어쓰기
- 원문이 추상적이면 "왜 이 제도가 필요한가" 같은 배경 섹션을 일반 지식 범위에서 짧게 추가
- FAQ 를 4개로 확장

1. **리드 문단 1개** — 메인 키워드 + 숫자/날짜. 2~3문장.
2. **핵심 요약 인포박스 1개** — 3~4줄 불릿. 원문의 차등 구간/대상 그룹이 있으면 여기서 **모두** 명시.
3. **본론 섹션 2~4개** — 각각 `<h2>` + 2~3문단. 제도 배경, 대상/조건, 신청 방법, 주의사항 중 주제에 맞게. **원문에 여러 그룹·차등 구간·카테고리가 있으면 별도 H2 섹션으로 분리해서 표·리스트로 빠짐없이 다루기**.
4. **FAQ 3개** — `<details>` 태그. 실제 독자가 궁금해할 질문 (대상 자격, 중복 가능 여부, 신청 절차 등).
5. **한 줄 정리** — 마지막에 행동 요약 1~2문장.

HTML 스타일은 **프로젝트 스타일 가이드를 정확히 따를 것** (아래 별도 섹션 참조).

## ⚠️ 이미지 규칙 (엄격)

- **`body_html` 안에 `<figure>`, `<img>` 태그를 넣지 말 것**. 썸네일 이미지는 시스템이 자동으로 맨 위에 삽입합니다. 중복 이미지가 발행됨.
- 원문에 이미지 URL 이 여러 개여도 마찬가지. body_html 은 순수 텍스트/HTML 구조(`<p>`, `<h2>`, `<div>`, `<ul>`, `<ol>`, `<details>`, `<strong>`)로만.

## ⚠️ JSON 문자열 이스케이프 (엄격)

응답 전체가 JSON 문자열이므로 **모든 문자열 필드 내부에 이중따옴표(`"`) 를 직접 넣지 말 것**. 원문이 따옴표 인용을 쓰더라도 반드시 아래 중 하나로 바꿔 쓰세요:

- 한국어 인용부호 `"…"` (U+201C/U+201D) 또는 홑따옴표 `'…'`
- HTML 속성값 안의 따옴표는 홑따옴표 `'` 사용 (예: `<img alt='사진 설명'>`)
- 큰따옴표가 꼭 필요하면 JSON 이스케이프 `\"` 로

이중따옴표 하나가 이스케이프 안 되면 전체 JSON 파싱이 터집니다. **strong/p/h2 본문 안에 원문 큰따옴표가 있으면 무조건 한국어 인용부호로 치환**.

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


def _recent_published_titles(days: int = WRITER_DEDUP_DAYS) -> list[str]:
    """data/drafts/published/*.html 중 최근 N일 내 발행글 제목 리스트.

    HTML 주석의 PublishedAt(우선) 또는 Created 기준. mtime 은 actions/checkout
    이 덮어써서 GitHub Actions 환경에서 신뢰 못 함.
    """
    if not PUBLISHED_DIR.exists():
        return []
    now = datetime.now()
    titles: list[str] = []
    for html_path in PUBLISHED_DIR.glob("*.html"):
        text = html_path.read_text(encoding="utf-8")[:3000]
        # 주석 메타에서 Title + Created/PublishedAt 추출
        title = None
        created_str = None
        for m in re.finditer(r"<!--(.*?)-->", text, re.DOTALL):
            for line in m.group(1).splitlines():
                if ":" not in line:
                    continue
                k, _, v = line.partition(":")
                k = k.strip().lower()
                v = v.strip()
                if k == "title" and not title:
                    title = v
                elif k in ("publishedat", "created") and not created_str:
                    created_str = v
        if not title or not created_str:
            continue
        try:
            dt = datetime.fromisoformat(created_str)
            dt_naive = dt.replace(tzinfo=None) if dt.tzinfo else dt
        except ValueError:
            continue
        if (now - dt_naive).total_seconds() > days * 86400:
            continue
        titles.append(title)
    return titles


def claude_semantic_dedup_check(
    candidate: dict, recent_titles: list[str]
) -> tuple[bool, str]:
    """Claude 의미 기반 중복 체크. bigram 으론 못 잡는 같은 사건 다른 헤드라인 케이스 대응.

    예: 동일 정책 발표를 정책브리핑 vs 연합뉴스가 표현 완전 다르게 보도하는 케이스.
    Haiku 사용으로 호출당 ~$0.001. 실패 시 안전 모드 = 통과.
    Returns: (is_duplicate, matched_title).
    """
    if not recent_titles:
        return False, ""

    title = candidate.get("title", "")
    summary = (candidate.get("summary", "") or "")[:300]

    sample = recent_titles[:30]
    system = (
        "당신은 한국 정부정책·생활정보 블로그의 편집자다. "
        "새 후보 기사가 최근 발행된 글들과 같은 제도/사건/혜택을 다루는지 판단한다. "
        "같은 제도/사건이지만 헤드라인 표현만 다르면 중복(duplicate). "
        "같은 기관·대상이라도 다른 제도·발표·시행일을 다루면 독립 기사(NOT duplicate)."
    )
    user = (
        "새 후보:\n"
        f"제목: {title}\n"
        f"요약: {summary}\n\n"
        "최근 발행된 글 제목 목록:\n"
        + "\n".join(f"- {t}" for t in sample)
        + "\n\n새 후보가 위 목록 중 하나와 같은 사건/제도/혜택을 다루는가? "
        + 'raw JSON 만 반환: {"is_duplicate": true|false, "matched": "정확히 일치하는 제목 또는 빈 문자열"}'
    )

    try:
        model = (
            os.environ.get("CLAUDE_MODEL_REVIEW", "").strip()
            or "claude-haiku-4-5-20251001"
        )
        result, _ = call_json(
            system=system,
            user=user,
            max_tokens=200,
            temperature=0.1,
            model=model,
        )
        return bool(result.get("is_duplicate", False)), str(result.get("matched", ""))
    except Exception as e:
        logger.warning(f"Claude dedup 체크 실패 (통과 처리): {e}")
        return False, ""


def pick_one_candidate(items: list[dict]) -> tuple[dict | None, list[str]]:
    """(score 내림차순, collected_at 내림차순) 정렬 후 최근 발행과 중복 안 되는 첫 후보.

    2층 dedup:
    1) 텍스트 bigram (is_similar, 임계 8)
    2) Claude 의미 기반 (Haiku 호출)

    중복으로 스킵된 후보 guid 리스트도 함께 반환 → main 에서 candidates 정리 시 사용.
    """
    skipped_guids: list[str] = []
    if not items:
        return None, skipped_guids

    sorted_items = sorted(
        items,
        key=lambda x: (x.get("score", 0), x.get("collected_at", "")),
        reverse=True,
    )

    recent_titles = _recent_published_titles()
    if recent_titles:
        logger.info(f"최근 {WRITER_DEDUP_DAYS}일 발행글 제목 비교 대상: {len(recent_titles)}건")

    for cand in sorted_items:
        title = cand.get("title", "")
        # 1) 텍스트 bigram 8 공통 이상 = 유사 (collect 단계와 동일 임계)
        if recent_titles and is_similar(title, recent_titles):
            logger.warning(f"  [스킵-bigram] 최근 발행과 유사: {title[:60]}")
            skipped_guids.append(cand.get("guid", ""))
            continue
        # 2) Claude 의미 기반 — bigram 못 잡는 동일 사건 다른 표현 케이스
        is_dup, matched = claude_semantic_dedup_check(cand, recent_titles)
        if is_dup:
            logger.warning(
                f"  [스킵-semantic] {title[:55]} ↔ {matched[:40]}"
            )
            skipped_guids.append(cand.get("guid", ""))
            continue
        return cand, skipped_guids

    return None, skipped_guids


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


LEADING_IMG_RE = re.compile(
    r"^\s*(?:<figure[^>]*>.*?</figure>|<img[^>]*/?>)\s*",
    re.DOTALL | re.IGNORECASE,
)


def strip_leading_image(body_html: str) -> str:
    """body_html 맨 앞에 Claude 가 덧붙인 figure/img 제거 (프롬프트 어긴 경우 안전장치)."""
    prev = None
    current = body_html
    # 여러 겹 감싼 경우 대비해 반복 제거
    while current != prev:
        prev = current
        current = LEADING_IMG_RE.sub("", current)
    return current


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

    # Claude 가 body_html 맨 앞에 이미지 넣었으면 제거 (중복 방지)
    body = strip_leading_image(article.get("body_html", "").strip())
    parts.append(body + "\n\n")

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
            max_tokens=6000,  # 700어절 = ~3500토큰 + 여유 (잘림 방지)
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

    # 1건 선택 (점수+최신 우선, 최근 7일 발행과 중복인 후보 자동 스킵)
    selected, skipped_guids = pick_one_candidate(items)

    # 발행 중복으로 스킵된 후보들을 candidates 에서 제거 (다음 cron 에서 또 픽되지 않게)
    if skipped_guids:
        skip_set = set(skipped_guids)
        new_items = [i for i in items if i.get("guid", "") not in skip_set]
        save_json(CANDIDATES_PATH, {
            "last_updated": datetime.now().isoformat(),
            "items": new_items,
        })
        logger.info(f"중복 스킵된 후보 {len(skipped_guids)}건 candidates 에서 제거")

    if not selected:
        slack_notify(
            f"⏸️ *Writer*: 작성 후보 없음"
            + (f" (최근 발행 중복으로 {len(skipped_guids)}건 스킵)" if skipped_guids else "")
        )
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
