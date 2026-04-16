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
    # 정부 정책 (priority 1: 기본 통과)
    "정책브리핑": {
        "url": "https://www.korea.kr/rss/policy.xml",
        "category": "정책",
        "priority": 1,
    },
    "복지부": {
        "url": "https://www.mohw.go.kr/rsm/rss/rss.jsp",
        "category": "복지",
        "priority": 1,
    },
    "국토교통부": {
        "url": "https://www.molit.go.kr/rss/rss.jsp",
        "category": "교통/주거",
        "priority": 1,
    },
    "기획재정부": {
        "url": "https://www.moef.go.kr/rss/rss.jsp",
        "category": "경제",
        "priority": 1,
    },
    "고용노동부": {
        "url": "https://www.moel.go.kr/rss/rss.jsp",
        "category": "고용",
        "priority": 1,
    },
    # 연합뉴스 (priority 2: 키워드 매칭 필요)
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
    "연합뉴스_생활": {
        "url": "https://www.yna.co.kr/rss/life.xml",
        "category": "생활",
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
