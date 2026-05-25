"""Reviewer가 남긴 data/review-actions.json 의 FIX 액션을 WP REST API로 적용.

- action=fix 만 처리 (삭제는 사람이 수동)
- slug 으로 WP post id 조회 후 content/title/meta 업데이트
- 처리 완료한 액션은 review-actions.json에서 제거
- Slack Webhook으로 개별 결과 알림
"""
from __future__ import annotations

import base64
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import requests

WP_URL = os.environ["WP_URL"].rstrip("/")
WP_USERNAME = os.environ["WP_USERNAME"]
WP_APP_PASSWORD = os.environ["WP_APP_PASSWORD"]
SLACK_WEBHOOK = os.environ.get("SLACK_WEBHOOK_URL", "")

ACTIONS_PATH = Path("data/review-actions.json")


# 2026-05-17 부터 update_post 호출이 Cloudways nginx 단에서 HTTP 415 로 차단됨.
# WAF / bot 탐지가 기본 python-requests UA 를 의심스럽게 보는 패턴으로 추정.
# 브라우저 형태 UA + 명시적 Accept 헤더 + charset=utf-8 로 우회.
_BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "application/json",
}


def auth_header() -> dict:
    token = base64.b64encode(f"{WP_USERNAME}:{WP_APP_PASSWORD}".encode()).decode()
    return {"Authorization": f"Basic {token}", **_BROWSER_HEADERS}


def find_post_id(slug: str) -> int | None:
    r = requests.get(
        f"{WP_URL}/wp-json/wp/v2/posts",
        params={"slug": slug, "_fields": "id"},
        headers=auth_header(),
        timeout=15,
    )
    r.raise_for_status()
    posts = r.json()
    return posts[0]["id"] if posts else None


def update_post(post_id: int, payload: dict) -> dict:
    r = requests.post(
        f"{WP_URL}/wp-json/wp/v2/posts/{post_id}",
        headers={**auth_header(), "Content-Type": "application/json; charset=utf-8"},
        json=payload,
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def slack_notify(text: str) -> None:
    if not SLACK_WEBHOOK:
        return
    try:
        requests.post(SLACK_WEBHOOK, json={"text": text}, timeout=10)
    except Exception as e:
        print(f"Slack 알림 실패: {e}", file=sys.stderr)


def build_payload(action: dict) -> dict:
    payload: dict = {}
    if "new_content" in action:
        payload["content"] = action["new_content"]
    if "new_title" in action:
        payload["title"] = action["new_title"]
    if "new_slug" in action:
        payload["slug"] = action["new_slug"]
    if "new_meta_desc" in action:
        payload["meta"] = {"yoast_wpseo_metadesc": action["new_meta_desc"]}
    return payload


def main() -> int:
    if not ACTIONS_PATH.exists():
        print("review-actions.json 없음")
        return 0

    with ACTIONS_PATH.open(encoding="utf-8") as f:
        data = json.load(f)

    actions = data.get("actions", [])
    if not actions:
        print("처리할 액션 없음")
        return 0

    print(f"처리 대상: {len(actions)}건")
    remaining: list[dict] = []
    success_count = 0

    for action in actions:
        kind = action.get("action")
        if kind != "fix":
            # 이 스크립트는 fix만 처리. 다른 액션은 그대로 보존
            remaining.append(action)
            continue

        slug = action.get("slug")
        if not slug:
            slack_notify(f"❌ *review-apply 실패*\n사유: slug 누락\n{json.dumps(action, ensure_ascii=False)[:200]}")
            continue

        try:
            post_id = action.get("post_id") or find_post_id(slug)
            if not post_id:
                slack_notify(f"❌ *review-apply 실패*\nslug={slug} — WP에서 post 찾기 실패")
                continue

            payload = build_payload(action)
            if not payload:
                slack_notify(f"⚠️ *review-apply 건너뜀*\nslug={slug} — 업데이트할 필드 없음")
                continue

            result = update_post(post_id, payload)
            success_count += 1
            changed = ", ".join(payload.keys())
            slack_notify(
                f"✏️ *수정 적용 완료*\n제목: {result.get('title', {}).get('rendered', slug)}\n"
                f"변경: {changed}\n사유: {action.get('reason', '-')}\n"
                f"링크: {result.get('link', '-')}"
            )
            print(f"OK: {slug} (post_id={post_id})")

        except requests.HTTPError as e:
            err = f"HTTP {e.response.status_code}: {e.response.text[:200]}"
            slack_notify(f"❌ *review-apply 에러*\nslug={slug}\n{err}")
            print(f"FAIL: {slug} - {err}", file=sys.stderr)
            remaining.append(action)  # 재시도 가능하게 남김
        except Exception as e:
            slack_notify(f"❌ *review-apply 에러*\nslug={slug}\n{e}")
            print(f"FAIL: {slug} - {e}", file=sys.stderr)
            remaining.append(action)

    # 처리 후 상태 저장
    with ACTIONS_PATH.open("w", encoding="utf-8") as f:
        json.dump(
            {"last_updated": datetime.now().isoformat(), "actions": remaining},
            f,
            ensure_ascii=False,
            indent=2,
        )

    slack_notify(
        f"📊 *리뷰 적용 요약*\n처리: {success_count}/{len(actions)}건\n잔여: {len(remaining)}건"
    )
    return 0 if not remaining else 1


if __name__ == "__main__":
    sys.exit(main())
