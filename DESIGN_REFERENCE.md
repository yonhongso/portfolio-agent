# Portfolio Intelligence Agent — 레이아웃 디자인 레퍼런스

> **🚨 [최우선 규칙] 어떤 작업을 하더라도 이 파일에 정의된 Weekly/Monthly 레이아웃 구조·포맷은 절대 변경하지 않는다.**
> **콘텐츠(텍스트·데이터)는 변경 가능. CSS 구조·섹션 구성·컬러는 변경 불가.**
> **dispatcher.py의 `_build_weekly_section()` / `_build_monthly_section()` 수정 시 반드시 이 파일을 먼저 읽고 체크리스트를 통과한 후에만 수정한다.**

## 확정 디자인 기준 커밋: `6b822ee` + 2026-05-23 복원 패치

---

## Weekly 탭 (WEEKLY-AUTO 섹션) 필수 구조

### W1: AI 총평 박스
- 배경: `background:linear-gradient(135deg,#1a2e7a,#2d1b69)` **반드시 이 색상**
- 인사이트 행 형식: `<div style='display:flex;gap:10px;align-items:flex-start'><span>🔴/🟡/💡</span><span><b>기업명</b> — 요약</span></div>`
- 헤더 텍스트 스타일: `font-size:10px;font-weight:700;opacity:.6;letter-spacing:1px;text-transform:uppercase`

### W2: 히트맵 테이블
- 헤더 배경: `background:#1a1a2e;color:#fff` (검정에 가까운 네이비)
- 셀 크기: `width:32px;height:32px;border-radius:6px` (정사각형)
- 빈 셀 색: `background:#f0f0f0;color:#aaa`
- 즉시검토 셀: `background:#fde8e8;color:#c0392b`
- 동향주시 셀: `background:#fef3cd;color:#856404`
- 정기모니터링 셀: `background:#e8f8f0;color:#27ae60`
- 섹션 제목: `border-left:4px solid #c0392b;padding-left:10px`

### W3: 수렴 시그널
- HIGH CONVERGENCE 라벨: `background:#8e44ad`, 카드 배경: `background:#fdf6ff`
- CONCENTRATED 라벨: `background:#e74c3c`, 카드 배경: `background:#fff9f9`
- 태그 pills: `border-radius:5px` (둥근 직사각형, 999px 아님)
- 섹션 제목: `border-left:4px solid #8e44ad`

### W4: 다음 주 모니터링
- 노란 카드: `background:#fffbea;border:1.5px solid #f39c12;border-radius:10px`

---

## Monthly 탭 (MONTHLY-AUTO 섹션) 필수 구조

### M1: 월간 총평 박스
- 배경: `background:linear-gradient(135deg,#1a3a2e,#0d2137)` **반드시 이 색상**
- 인사이트 행 형식: Weekly와 동일 (🔴/🟡/📈 emoji + `<b>기업명</b>` 형식)
- 헤더 텍스트: `📋 월간 포트폴리오 리뷰 — YYYY년 M월`

### M2: 리스크 등급 매트릭스 테이블
- **반드시 포함**: `이전 → 현재` 컬럼 구조 테이블
- 헤더 배경: `background:#1a1a2e;color:#fff`
- 섹션 제목: `border-left:4px solid #2980b9`
- 등급 뱃지: `border-radius:6px;padding:4px 10px`

### M3: ~~Exit 파이프라인~~ (삭제 — 2026-05-23, 자동 분류 신뢰도 이슈로 제거)

### M4: 이달 팀 액션 로그
- **반드시 포함**: 점선 박스 플레이스홀더
- 섹션 제목: `border-left:4px solid #7f8c8d`

### M5: 다음 달 주요 이벤트
- **반드시 포함**: 왼쪽 테두리 카드 목록
- 섹션 제목: `border-left:4px solid #2980b9`

---

## Claude에게 — 수정 금지 체크리스트

> **어떤 작업(버그수정, 기능추가, 이메일 포맷 수정 등)이라도 아래 항목을 건드리면 절대 안 된다.**

`_build_weekly_section()` 또는 `_build_monthly_section()` 수정 시:
1. [ ] Weekly 그라디언트 `#1a2e7a,#2d1b69` 유지 여부 확인
2. [ ] Monthly 그라디언트 `#1a3a2e,#0d2137` 유지 여부 확인
3. [ ] Weekly 섹션 제목 모두 `border-left:4px solid` 방식 유지 (border-bottom으로 바꾸지 말 것)
4. [ ] Weekly W4 컨테이너 노란 배경 `#fffbea;border:1.5px solid #f39c12` 유지
5. [ ] 수렴 시그널 태그 pills `border-radius:5px` 유지 (999px으로 바꾸지 말 것)
6. [ ] AI 인사이트 bullet `●` 추가하지 말 것 — 이모지는 AI 텍스트에서 직접 표시
7. [ ] Monthly에 5개 섹션(M1~M5) 모두 포함 여부 확인
8. [ ] Weekly에 4개 섹션(W1~W4) 모두 포함 여부 확인
