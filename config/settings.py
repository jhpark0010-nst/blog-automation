"""
블로그 자동화 시스템 설정
GitHub Actions에서 RSS 수집 + 1단계 필터링용
"""
from pathlib import Path

# ── 경로 ──
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

# ── 데이터 파일 ──
PROCESSED_PATH = DATA_DIR / "processed.json"    # 수집 완료된 GUID 목록
PENDING_PATH = DATA_DIR / "pending.json"         # 1단계 통과, Claude 평가 대기
PUBLISHED_PATH = DATA_DIR / "published.json"     # 발행 완료 로그

# ── RSS 피드 소스 ──
RSS_FEEDS = {
    # ── 정부 정책 (priority 1: 기본 통과) ──
    "정책브리핑_정책뉴스": {
        "url": "https://www.korea.kr/rss/policy.xml",
        "category": "정책",
        "priority": 1,
    },
    "정책브리핑_보도자료": {
        "url": "https://www.korea.kr/rss/pressrelease.xml",
        "category": "정책",
        "priority": 1,
    },
    "국토교통부_보도": {
        "url": "https://www.molit.go.kr/dev/board/board_rss.jsp?rss_id=NEWS",
        "category": "교통/주거",
        "priority": 1,
    },
    "국토교통부_공지": {
        "url": "https://www.molit.go.kr/dev/board/board_rss.jsp?rss_id=N01_B",
        "category": "교통/주거",
        "priority": 1,
    },
    # ── 연합뉴스 (priority 2: 키워드 매칭 필요) ──
    "연합뉴스_정치": {
        "url": "https://www.yna.co.kr/rss/politics.xml",
        "category": "정치",
        "priority": 2,
    },
    "연합뉴스_경제": {
        "url": "https://www.yna.co.kr/rss/economy.xml",
        "category": "경제",
        "priority": 2,
    },
    "연합뉴스_사회": {
        "url": "https://www.yna.co.kr/rss/society.xml",
        "category": "사회",
        "priority": 2,
    },
}

# ── 1단계 필터링: 제외 키워드 ──
EXCLUDE_KEYWORDS = [
    "장관 참석", "차관 참석", "간담회 개최", "행사 참석",
    "기자회견", "브리핑", "축사", "기념식",
    "정정합니다", "수정합니다", "보도자료 정정", "제목 수정",
    "사진설명", "포토", "영상",
]

# ── 1단계 필터링: 우선 키워드 (생활 밀접) ──
PRIORITY_KEYWORDS = [
    # 혜택/금액
    "지원금", "보조금", "수당", "급여", "감면", "면제", "할인",
    "대출", "이자", "금리", "세금", "공제", "환급",
    # 신청/마감
    "신청", "접수", "마감", "모집", "선발",
    # 생활 밀접
    "교통", "주거", "임대", "전세", "월세", "청약",
    "육아", "출산", "양육", "돌봄", "어린이집",
    "취업", "채용", "일자리", "근로", "최저임금",
    "의료", "건강보험", "진료", "병원",
    "연금", "퇴직", "실업급여", "고용보험",
    "장학금", "등록금", "학자금",
]

# ── 중복 체크 ──
DUPLICATE_CHECK_DAYS = 7

# ── 2단계 Claude 평가 기준 ──
SCORE_THRESHOLD = 70

# ── 데이터 파일 추가 (API 전환 후) ──
CANDIDATES_PATH = DATA_DIR / "candidates.json"
REVIEW_ACTIONS_PATH = DATA_DIR / "review-actions.json"

# ── Pending 만료 (Evaluator가 참조) ──
PENDING_MAX_AGE_DAYS = 7

# ── Evaluator ──
EVAL_BATCH_SIZE = 30  # 1회 처리 최대 건수

# ── Writer ──
# 정책/생활정보 재구성. 한국어 어절 기준 (영어 word 와 다름).
# 2026-04-25 실측: Claude 가 700+ 어절 일괄 생성을 어려워해 평균 410 출력.
# 모바일 가독성도 1500~2000자가 적정 → 기준 현실화.
WRITER_TARGET_WORD_COUNT_MIN = 400  # 어절
WRITER_TARGET_WORD_COUNT_MAX = 700  # 어절
WRITER_ARTICLES_PER_RUN = 1  # 1회 실행당 1건 (사용자 지시)
