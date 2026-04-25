# publickorea.org 스타일 가이드

한국 정부정책·생활정보 블로그. 독자는 한국어 일반 사용자. 목표는 "원문보다 이해하기 쉽고 실용성 높은 재구성".

## 독자·톤

- 대상: 일반 국민 (30~60대 포괄). 정책 문서보다 이해가 쉬워야 함.
- 톤: **존댓말, ~요/~세요 종결**. 딱딱한 공문체 금지, 과장·감탄 금지.
- 관점: 독자 입장. "당신도 신청할 수 있습니다" 가 아니라 "~하신 분은 신청 가능합니다" 같이 정중하되 친근.

## 본문 구조 (필수)

1. **리드 문단** — 핵심 사실 요약. 메인 키워드(제도/혜택명) + 숫자/날짜 포함. 2~3문장.
2. **핵심 요약 인포박스** — 3~4줄 bullet. 배경색 있는 `<div>` 박스.
3. **본론 섹션 2~4개** — 각 `<h2>` + 본문. 제도 배경·대상·조건·신청 방법·주의사항 등.
4. **FAQ** — `<details>` 3개. Q&A 형식. 실제 독자가 궁금할 질문.
5. **한 줄 정리** — 마지막에 핵심 행동 요약 1~2문장.

총 **400~700 어절** (공백 기준 한국어 어절. 약 1300~2000자, 모바일 가독성 적정 분량).

## HTML 스타일 (정확한 인라인 스타일)

### 문단
```html
<p style="margin-bottom:1.5em;line-height:1.8;">...</p>
```

### 핵심 요약 박스
```html
<div style="background:#EFF6FF;border-left:4px solid #2563EB;padding:20px 24px;border-radius:8px;margin:24px 0;">
  <strong>핵심 요약</strong>
  <ul style="margin-top:12px;line-height:2;">
    <li>...</li>
  </ul>
</div>
```

### H2 섹션 제목
```html
<h2 style="margin-top:40px;border-bottom:2px solid #E2E8F0;padding-bottom:8px;">...</h2>
```

### 번호 리스트
```html
<ol style="line-height:2;margin-bottom:1.5em;">
  <li>...</li>
</ol>
```

### FAQ
```html
<details style="margin-bottom:12px;border:1px solid #E2E8F0;border-radius:6px;padding:12px;">
  <summary><strong>Q. ...</strong></summary>
  <p style="margin-top:8px;">A. ...</p>
</details>
```

### 썸네일 (원문에 이미지 있을 때만)
```html
<figure style="margin:0 0 24px 0;">
  <img src="{썸네일 URL}" alt="{키워드 포함 alt}" style="width:100%;border-radius:8px;"/>
</figure>
```

## 강조 규칙

- **금액·날짜·연령·기준선은 `<strong>` 로 굵게**: `<strong>19만 명</strong>`, `<strong>2026년 4월 22일</strong>`, `<strong>만 65세</strong>`
- **문의 번호도 굵게**: `<strong>1599-2000</strong>`, `<strong>126</strong>`
- 굵게 남용 금지. 한 문단에 2~3개 선까지.

## SEO 규칙

- Title: **28~34자**. 메인 키워드를 앞 15자 안에 배치.
- Meta: **110~140자**. 메인 키워드 + 구체 숫자/날짜 포함.
- Slug: 영문 소문자 + 하이픈. 3~6단어. (예: `basic-pension-benefits-guide`)
- Tags: 3~5개. 제도명·혜택유형·대상·관련 기관.
- **첫 문단에 메인 키워드 + 숫자/날짜** 필수.

## 재구성 원칙 (중요)

원문 그대로 베껴쓰지 말 것. 독자가 얻어갈 정보 관점으로 재배열:

1. 원문이 발표문/보도자료처럼 건조하면 → 독자 관점 문제 제기로 리드 전환
2. 원문에 빠진 **신청처·번호·홈페이지 URL** 을 기본 지식 수준으로 **보강**. 단 불확실한 숫자/기한은 절대 추측해서 넣지 말 것.
3. FAQ 는 원문에 없어도 독자가 궁금할 만한 Q 3개 필수 작성.
4. 원문에 없는 **금액·수치·기한·인명** 은 발명 금지. 모르면 "자세한 조건은 ~에서 확인하세요" 로 회피.

## 금지

- 광고성 문구 ("절대 놓치지 마세요!", "꿀팁!" 등)
- 감탄부호, 물음표 남발
- 애매한 표현 ("어쩌면", "~일지도")
- 원문에 없는 정치적 해석/입장
- 다른 매체 인용 (WebSearch 금지 환경)
