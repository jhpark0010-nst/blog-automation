#!/usr/bin/env python3
"""Reviewer (GitHub Actions 진입점).

최근 24시간 발행글 → Anthropic API 사후 감사 (중복/팩트/가독성/SEO) →
FIX 액션을 review-actions.json 에 append → review-apply workflow 가 WP 반영.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

import requests

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.anthropic_helper import call_json, estimate_cost_usd
from src.content_filter import best_bigram_overlap

SLACK_WEBHOOK = os.environ.get("SLACK_WEBHOOK_URL", "")
REVIEW_ACTIONS_PATH = PROJECT_ROOT / "data" / "review-actions.json"
PUBLISHED_DIR = PROJECT_ROOT / "data" / "drafts" / "published"

REVIEW_WINDOW_HOURS = 24
DEDUP_LOOKBACK_DAYS = 7

# Reviewer 모델. 하루 1회 × ~8편이라 Sonnet 여유. env 로 Haiku 로 오버라이드 가능.
REVIEW_MODEL = (
    os.environ.get("CLAUDE_MODEL_REVIEW", "").strip()
    or "claude-sonnet-4-6"
)

# 입출력 파라미터 (A: 모델·토큰 원복)
BODY_HTML_MAX_CHARS = 7000
SOURCE_SUMMARY_MAX_CHARS = 2500
RECENT_TITLES_MAX = 30
RESPONSE_MAX_TOKENS = 4000

# bigram 힌트용 (Claude 에게 참고 수치로 제공). 자동 notify 는 없음 —
# 같은 기관/아티스트라도 다른 제도/사건/곡이면 독립 기사이므로 판단은 Claude 에게 맡김.

# 원문 fetch (D: 안정성 보강)
SOURCE_FETCH_TIMEOUT = 20
BROWSER_UAS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
]
BROWSER_UA = BROWSER_UAS[0]  # 기존 변수 유지 (다른 곳 참조 대비)

COMMENT_META_RE = re.compile(r"<!--(.*?)-->", re.DOTALL)
TAG_STRIP_RE = re.compile(r"<[^>]+>")

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

1. **중복**: 최근 7일 내 발행글과 **동일 제도·사건·혜택**을 다루는가? (시스템이 계산한 **Bigram 유사도** 힌트가 함께 제공됩니다 — 수치가 높으면 제목 겹침이 많다는 신호지만 자동 판정 금지.)
   - 중복 판단 원칙: **같은 기관/대상이어도 서로 다른 제도·발표·시행일·수치**를 다루면 **독립 기사**. 예: "기초연금 인상안 발표" 와 "기초연금 신청 방법 가이드" 는 별개.
   - 진짜 중복 = 같은 이벤트/발표/제도를 다시 쓴 것 (같은 날짜·같은 수치·같은 액션).
2. **팩트 정확성**: 원문 요약과 비교해 숫자·날짜·기관명·제도명에 오류 또는 날조가 있는가? (원문 fetch 실패 시 팩트 단정 불가 — 그 경우 팩트 검증은 스킵하고 그 사실만 issues 에 남길 것.)
3. **가독성**: 한국어 맞춤법, 띄어쓰기, 어색한 문장, 반복/누락.
4. **SEO 및 스타일 가이드 준수**: 제목 28~34자, 메타 110~140자, 슬러그 형식, **본문 400~700 어절(한국어 단어)**, 핵심요약 박스/H2 2개+/FAQ 3개, 출처 링크, 존댓말 유지. (시스템이 계산한 **구조 메타** 가 함께 제공됩니다 — word_count, h2_count, faq_count, has_infobox 등. word_count 가 400~700 범위면 통과로 보세요. 350 미만이거나 800 초과만 FIX.)

## 출력

**raw JSON 1개만** 반환 (마크다운 코드블록 금지). 스키마:

```
{
  "action": "fix" | "notify" | "pass",
  "reason": "한 줄 요약 (한국어)",
  "issues": ["상세 이슈 1 (한국어)", "상세 이슈 2 (한국어)"],
  "recommended_action": "(notify 시) '삭제'|'통합'|'유지' 중 하나 + 한 줄 근거",
  "new_content": "(action=fix 일 때만) 수정된 전체 HTML 본문. 주석 헤더 없이 body 만. 한국어로.",
  "new_meta_desc": "(선택, 메타가 바뀔 때만) 한국어, 110~140자",
  "new_title": "(선택, 제목이 바뀔 때만) 한국어, 28~34자"
}
```

## 액션 규칙

- `fix`: 맞춤법/가독성/SEO/구조 경미한 수정. `new_content` 에 수정된 HTML 제공. 팩트 바꾸지 말 것. 이미지 태그(`<img>`, `<figure>`) 는 넣지 말 것 (시스템이 자동 삽입).
- `notify`: 팩트 오류 의심, 중복 확실, 심각한 구조 결함. `new_content` 안 보냄. `issues` 에 구체 근거 + `recommended_action` 필수.
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


def fetch_source_summary(url: str, max_chars: int = SOURCE_SUMMARY_MAX_CHARS) -> str | None:
    """원문 URL fetch → 본문 2500자. 실패 시 다른 UA 로 1회 재시도."""
    if not url or not url.startswith("http"):
        return None

    from bs4 import BeautifulSoup

    last_err: Exception | None = None
    for attempt, ua in enumerate(BROWSER_UAS):
        try:
            resp = requests.get(
                url,
                timeout=SOURCE_FETCH_TIMEOUT,
                headers={
                    "User-Agent": ua,
                    "Accept": "text/html,application/xhtml+xml",
                    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
                },
            )
            resp.raise_for_status()
            resp.encoding = resp.apparent_encoding
            soup = BeautifulSoup(resp.text, "html.parser")
            for tag in soup.select("script, style, nav, footer, aside, .ad"):
                tag.decompose()
            text = soup.get_text(separator="\n", strip=True)
            lines = [ln for ln in text.splitlines() if ln.strip()]
            return "\n".join(lines)[:max_chars]
        except Exception as e:
            last_err = e
            logger.warning(
                f"원문 fetch 실패 attempt {attempt + 1}/{len(BROWSER_UAS)} ({url}): {e}"
            )
    logger.warning(f"원문 fetch 최종 실패 ({url}): {last_err}")
    return None


def count_words(text: str) -> int:
    """HTML 태그 제거 후 대략적인 단어/어절 수."""
    plain = TAG_STRIP_RE.sub(" ", text)
    plain = re.sub(r"\s+", " ", plain).strip()
    if not plain:
        return 0
    return len(plain.split())


def analyze_article_structure(body_html: str, meta: dict) -> dict:
    """Python 측 pre-check 메타. Claude 에게 판단 힌트로 전달.

    리턴: word_count, h2_count, faq_count, has_infobox, has_source_link,
    has_wp_post_id, image_count, raw_quote_in_body
    """
    h2_count = len(re.findall(r"<h2[^>]*>", body_html, re.IGNORECASE))
    faq_count = len(re.findall(r"<details[^>]*>", body_html, re.IGNORECASE))
    image_count = len(re.findall(r"<img[^>]*>|<figure[^>]*>", body_html, re.IGNORECASE))

    has_infobox = bool(
        re.search(r"<div[^>]*background[^>]*>[^<]*<strong>\s*핵심", body_html, re.IGNORECASE)
    )
    has_source_link = "출처:" in body_html or bool(
        re.search(r'<a[^>]+href=[^>]+>\s*(?:출처|원문)', body_html)
    )
    has_wp_post_id = bool(meta.get("wppostid"))

    # 본문 텍스트(태그 제외) 에 ASCII 큰따옴표가 2개 이상이면 쌍으로 있을 가능성
    plain = TAG_STRIP_RE.sub(" ", body_html)
    raw_quote_count = plain.count('"')

    return {
        "word_count": count_words(body_html),
        "h2_count": h2_count,
        "faq_count": faq_count,
        "image_count": image_count,
        "has_infobox": has_infobox,
        "has_source_link": has_source_link,
        "has_wp_post_id": has_wp_post_id,
        "raw_quote_count_in_body": raw_quote_count,
    }


def _parse_created_iso(meta: dict) -> datetime | None:
    """HTML 주석의 Created 또는 PublishedAt ISO 타임스탬프 파싱."""
    for key in ("publishedat", "created"):
        v = meta.get(key, "").strip()
        if not v:
            continue
        try:
            return datetime.fromisoformat(v)
        except ValueError:
            continue
    return None


def collect_recent_titles(exclude_path: Path) -> list[str]:
    """최근 7일 내 published 글 제목 (자신 제외).

    mtime 대신 HTML 주석의 Created/PublishedAt 을 사용 — GitHub Actions 의
    actions/checkout 이 모든 파일 mtime 을 체크아웃 시점으로 덮어쓰기 때문.
    """
    now = datetime.now()
    titles = []
    for html_path in PUBLISHED_DIR.glob("*.html"):
        if html_path == exclude_path:
            continue
        text = html_path.read_text(encoding="utf-8")[:3000]
        meta = parse_comment_meta(text)
        created = _parse_created_iso(meta)
        if created is None:
            continue
        # timezone 섞여있을 수 있으므로 naive 로 비교
        created_naive = created.replace(tzinfo=None) if created.tzinfo else created
        if (now - created_naive).total_seconds() > DEDUP_LOOKBACK_DAYS * 86400:
            continue
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
    structure: dict,
    similarity: tuple[int, str | None],
) -> str:
    bigram_score, similar_title = similarity
    similarity_line = (
        f"{bigram_score} (최고 유사 제목: {similar_title!r})"
        if similar_title
        else f"{bigram_score}"
    )
    structure_lines = "\n".join(f"  - {k}: {v}" for k, v in structure.items())
    return (
        "## 검토 대상 글\n\n"
        f"**제목**: {article_meta.get('title', '')}\n"
        f"**메타**: {article_meta.get('meta', '')}\n"
        f"**슬러그**: {article_meta.get('slug', '')}\n"
        f"**원문 제목**: {article_meta.get('originaltitle', '')}\n"
        f"**원문 URL**: {article_meta.get('originalurl', '')}\n\n"
        "### 시스템이 계산한 구조 메타 (B: Python pre-check)\n\n"
        f"{structure_lines}\n\n"
        "### 시스템이 계산한 최근 7일 제목 bigram 유사도 (C)\n\n"
        f"  공통 bigram 최대 개수: {similarity_line}\n"
        f"  (임계 참고: 8 이상이면 중복 확실, 5~7 주의, 4 이하 무관)\n\n"
        "### 본문 HTML\n\n"
        f"{article_body[:BODY_HTML_MAX_CHARS]}\n\n"
        "### 원문 본문 발췌 (팩트 대조용)\n\n"
        f"{source_summary or '(원문 fetch 실패 — 팩트체크 스킵)'}\n\n"
        "### 최근 7일 발행 제목 (중복 체크용)\n\n"
        + "\n".join(f"- {t}" for t in recent_titles[:RECENT_TITLES_MAX])
        + "\n\n위 스키마에 맞게 JSON 만 반환하세요."
    )


def review_one_file(filepath: Path, recent_titles: list[str]) -> dict:
    html = filepath.read_text(encoding="utf-8")
    meta = parse_comment_meta(html)
    body_only = strip_comment_headers(html)

    # B: 구조 메타 pre-check
    structure = analyze_article_structure(body_only, meta)

    # C: bigram 유사도는 힌트로만 Claude 에 전달 (자동 notify 없음)
    title = meta.get("title", "")
    similarity = best_bigram_overlap(title, recent_titles)

    # D: fetch 개선된 source summary
    source_url = meta.get("originalurl", "")
    source_summary = fetch_source_summary(source_url) if source_url else None

    try:
        result, api_meta = call_json(
            system=build_system_prompt(),
            user=build_review_input(
                body_only, meta, source_summary, recent_titles, structure, similarity
            ),
            max_tokens=RESPONSE_MAX_TOKENS,
            temperature=0.2,
            model=REVIEW_MODEL,
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
    result["_structure"] = structure
    result["_similarity"] = similarity
    return result


def main() -> int:
    logger.info("=" * 50)
    logger.info(f"Reviewer 시작: {datetime.now().isoformat()}")

    if not PUBLISHED_DIR.exists():
        logger.info("published 디렉토리 없음. 종료.")
        slack_notify("🔍 *Reviewer*: published 디렉토리 없음")
        return 0

    # GitHub Actions 의 actions/checkout 이 모든 파일 mtime 을 체크아웃 시점으로
    # 덮어쓰므로 mtime 필터는 쓸 수 없다. HTML 주석의 Created/PublishedAt 을 사용.
    now = datetime.now()
    cutoff = now - timedelta(hours=REVIEW_WINDOW_HOURS)

    def _file_created(p: Path) -> datetime | None:
        text = p.read_text(encoding="utf-8")[:3000]
        meta = parse_comment_meta(text)
        dt = _parse_created_iso(meta)
        if dt is None:
            return None
        return dt.replace(tzinfo=None) if dt.tzinfo else dt

    candidates = []
    for p in PUBLISHED_DIR.glob("*.html"):
        created = _file_created(p)
        if created is None or created < cutoff:
            continue
        candidates.append((created, p))
    files = [p for _, p in sorted(candidates, key=lambda x: x[0])]
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
                "recommended_action": result.get("recommended_action", ""),
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
            if ra := n.get("recommended_action"):
                lines.append(f"  → 권장: {ra}")
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
