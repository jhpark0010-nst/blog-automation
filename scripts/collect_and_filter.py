#!/usr/bin/env python3
"""
GitHub Actions 진입점
RSS 수집 → 1단계 필터링 → pending.json 업데이트
"""
import json
import sys
import logging
from pathlib import Path
from datetime import datetime, timedelta

# 프로젝트 루트를 path에 추가
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import PROCESSED_PATH, PENDING_PATH, PUBLISHED_PATH, DUPLICATE_CHECK_DAYS
from src.rss_collector import collect_all_feeds
from src.content_filter import filter_items

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


def load_json(path: Path, default=None):
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default if default is not None else []


def save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_recent_titles(published: list, pending_items: list) -> list[str]:
    """최근 N일 내 발행/대기 글 제목 수집 (중복 체크용)"""
    cutoff = (datetime.now() - timedelta(days=DUPLICATE_CHECK_DAYS)).isoformat()
    titles = []

    for item in published:
        if item.get("published_at", "") >= cutoff:
            titles.append(item.get("title", ""))

    for item in pending_items:
        if item.get("collected_at", "") >= cutoff:
            titles.append(item.get("title", ""))

    return titles


def cleanup_old_pending(items: list, max_age_days: int = 7) -> list:
    """7일 이상 지난 pending 항목 제거 (평가 기회 만료)"""
    cutoff = (datetime.now() - timedelta(days=max_age_days)).isoformat()
    before = len(items)
    items = [i for i in items if i.get("collected_at", "") >= cutoff]
    removed = before - len(items)
    if removed:
        logger.info(f"[정리] 7일+ 지난 pending 항목 {removed}건 제거 (expired)")
    return items


def main():
    logger.info("=" * 50)
    logger.info(f"RSS 수집 시작: {datetime.now().isoformat()}")

    # 데이터 로드
    processed_guids = set(load_json(PROCESSED_PATH, []))
    pending_data = load_json(PENDING_PATH, {"last_updated": "", "items": []})
    pending_items = pending_data.get("items", [])
    published = load_json(PUBLISHED_PATH, [])

    logger.info(f"기존 상태: 처리됨 {len(processed_guids)}건, 대기 {len(pending_items)}건")

    # 1. RSS 수집 (이미 처리된 GUID 스킵)
    new_items = collect_all_feeds(processed_guids)
    logger.info(f"신규 수집: {len(new_items)}건")

    if not new_items:
        logger.info("신규 항목 없음. 종료.")
        return

    # 2. 1단계 필터링
    recent_titles = get_recent_titles(published, pending_items)
    passed_items = filter_items(new_items, recent_titles)

    # 3. 상태 업데이트
    #    모든 수집 항목의 GUID를 processed에 추가 (통과 여부 무관)
    for item in new_items:
        processed_guids.add(item["guid"])

    #    통과 항목을 pending에 추가
    pending_items.extend(passed_items)

    #    오래된 pending 정리
    pending_items = cleanup_old_pending(pending_items)

    # 4. 저장
    save_json(PROCESSED_PATH, sorted(processed_guids))
    save_json(PENDING_PATH, {
        "last_updated": datetime.now().isoformat(),
        "items": pending_items,
    })

    # 5. 결과 출력 (GitHub Actions 로그)
    logger.info(f"{'=' * 50}")
    logger.info(f"수집 완료:")
    logger.info(f"  신규 수집: {len(new_items)}건")
    logger.info(f"  필터 통과: {len(passed_items)}건")
    logger.info(f"  현재 대기: {len(pending_items)}건")
    logger.info(f"  누적 처리: {len(processed_guids)}건")

    # GitHub Actions output
    print(f"::notice::수집 {len(new_items)}건, 필터 통과 {len(passed_items)}건, 대기 {len(pending_items)}건")


if __name__ == "__main__":
    main()
