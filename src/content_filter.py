"""
1단계 자동 필터링 (GitHub Actions용)
- 제외 키워드로 걸러내기
- 우선 키워드 매칭
- 뉴스 소스는 키워드 필수
- 최근 published/candidates/pending 과의 유사도 체크 (bigram 기반)
"""
import logging
import re
from datetime import datetime, timedelta

from config.settings import EXCLUDE_KEYWORDS, PRIORITY_KEYWORDS, DUPLICATE_CHECK_DAYS

logger = logging.getLogger(__name__)


def _title_bigrams(title: str) -> set:
    """제목에서 한글/영숫자만 남기고 bigram 집합 반환"""
    cleaned = re.sub(r'[^\w가-힣]', '', title)
    return {cleaned[i:i+2] for i in range(len(cleaned) - 1)}


def check_exclude(title: str, summary: str) -> str | None:
    text = f"{title} {summary}".lower()
    for kw in EXCLUDE_KEYWORDS:
        if kw.lower() in text:
            return f"제외 키워드: {kw}"
    return None


def count_priority_keywords(title: str, summary: str, content: str) -> tuple[int, list[str]]:
    text = f"{title} {summary} {content}".lower()
    matched = [kw for kw in PRIORITY_KEYWORDS if kw.lower() in text]
    return len(matched), matched


def has_deadline_signal(title: str, summary: str, content: str) -> bool:
    text = f"{title} {summary} {content}"
    return any(kw in text for kw in [
        "마감", "접수기간", "신청기간", "~까지", "까지 신청",
        "선착순", "모집 마감", "접수 마감",
    ])


def has_amount_signal(title: str, summary: str, content: str) -> bool:
    text = f"{title} {summary} {content}"
    return any(kw in text for kw in [
        "만원", "만 원", "억원", "억 원", "%", "퍼센트",
        "지원금", "보조금", "수당", "감면",
    ])


def is_similar(title: str, existing_titles: list[str], min_common_bigrams: int = 8) -> bool:
    """
    Bigram 기반 유사도 체크.
    공통 bigram이 min_common_bigrams 이상이면 유사 판정.
    한국어 제목(조사/특수문자 변형)에 강건한 방식.
    """
    tbg = _title_bigrams(title)
    if not tbg:
        return False
    for existing in existing_titles:
        ebg = _title_bigrams(existing)
        common = tbg & ebg
        if len(common) >= min_common_bigrams:
            return True
    return False


def best_bigram_overlap(title: str, existing_titles: list[str]) -> tuple[int, str | None]:
    """제목과 existing_titles 중 최고 bigram 공통 개수 + 그 제목 반환.

    Reviewer 에서 중복 의심도 수치화 용. is_similar 가 boolean 만 주는 것 보강.
    """
    tbg = _title_bigrams(title)
    if not tbg:
        return (0, None)
    best = 0
    best_title: str | None = None
    for existing in existing_titles:
        ebg = _title_bigrams(existing)
        common = len(tbg & ebg)
        if common > best:
            best = common
            best_title = existing
    return (best, best_title)


def filter_items(
    items: list[dict],
    recent_titles: list[str] | None = None,
) -> list[dict]:
    """
    1단계 필터링 수행.
    recent_titles: 최근 발행된 글 제목 목록 (중복 체크용)
    Returns: 통과한 항목 리스트 (filter_reason 필드 추가됨)
    """
    if recent_titles is None:
        recent_titles = []

    passed = []
    rejected = 0

    for item in items:
        title = item.get("title", "")
        summary = item.get("summary", "")
        content = item.get("content", "")

        # 1) 제외 키워드
        exclude = check_exclude(title, summary)
        if exclude:
            logger.debug(f"  [제외] {title[:50]} — {exclude}")
            rejected += 1
            continue

        # 2) 중복 체크
        if is_similar(title, recent_titles):
            logger.debug(f"  [제외] {title[:50]} — 유사 글 존재")
            rejected += 1
            continue

        # 3) 제목 최소 길이
        if len(title) < 5:
            rejected += 1
            continue

        # 4) 우선 키워드
        priority_count, matched_kws = count_priority_keywords(title, summary, content)
        has_deadline = has_deadline_signal(title, summary, content)
        has_amount = has_amount_signal(title, summary, content)

        # 5) 뉴스 소스는 키워드 매칭 필수
        is_news = item.get("priority", 1) == 2
        if is_news and priority_count == 0:
            logger.debug(f"  [제외] {title[:50]} — 뉴스: 정책 키워드 없음")
            rejected += 1
            continue

        # 필터 사유 기록
        reasons = []
        if matched_kws:
            reasons.append(f"키워드: {', '.join(matched_kws[:5])}")
        if has_deadline:
            reasons.append("마감일 있음")
        if has_amount:
            reasons.append("금액/혜택 명시")

        item["filter_reason"] = " | ".join(reasons) if reasons else "기본 통과"
        passed.append(item)
        logger.info(f"  [통과] {title[:50]} — {item['filter_reason']}")

    logger.info(f"[필터링] 전체 {len(items)}건 → 통과 {len(passed)}건, 제외 {rejected}건")
    return passed
