"""
test_quick.py — 빠른 파이프라인 테스트 (30초 내 완료)
-------------------------------------------------------
사용법:
  python test_quick.py              # 컬리+업스테이지 2개사, primary 키워드만
  python test_quick.py --company portone
  python test_quick.py --skip-collect  # 수집 건너뛰고 더미 데이터로 분류/발송만 테스트
"""

import argparse, logging, sys, os
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                    handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger(__name__)

# ── 인자 파싱 ──────────────────────────────────────────────────────────────
parser = argparse.ArgumentParser()
parser.add_argument("--company", default=None, help="테스트할 회사 ID (예: portone)")
parser.add_argument("--skip-collect", action="store_true", help="수집 생략, 더미 데이터 사용")
args = parser.parse_args()

# ── 1. 수집 ───────────────────────────────────────────────────────────────
if args.skip_collect:
    log.info("수집 생략 → 더미 데이터 사용")
    articles = [
        {"title": "[테스트] 업스테이지 시리즈B 투자 유치", "url": "https://example.com/1",
         "content": "업스테이지가 Amazon, AMD로부터 추가 투자를 유치했다.",
         "source": "GoogleNews", "company": "upstage", "date": datetime.now().isoformat()},
        {"title": "[테스트] 포트원 동남아 파트너십 체결", "url": "https://example.com/2",
         "content": "포트원이 싱가포르 핀테크 기업과 결제 인프라 파트너십을 맺었다.",
         "source": "TechCrunch", "company": "portone", "date": datetime.now().isoformat()},
        {"title": "[테스트] 에버온 EV 충전 보조금 정책 변화", "url": "https://example.com/3",
         "content": "정부의 EV 충전 인프라 보조금 정책이 개편되며 에버온에 영향이 예상된다.",
         "source": "GoogleNews", "company": "everon", "date": datetime.now().isoformat()},
    ]
else:
    from collector import Collector, load_portfolios

    # 테스트 대상 회사 제한 (빠르게 하기 위해)
    target = [args.company] if args.company else ["upstage", "kurly"]
    all_portfolios = load_portfolios("portfolio.yaml")
    test_portfolios = [p for p in all_portfolios if p.id in target]

    if not test_portfolios:
        log.error(f"회사를 찾을 수 없습니다: {target}")
        sys.exit(1)

    log.info(f"수집 대상: {[p.name for p in test_portfolios]}")

    # primary 키워드만 사용 (secondary 제외 → 속도 2-3배 향상)
    for p in test_portfolios:
        p.keywords_secondary = []  # secondary 스킵

    collector = Collector("portfolio.yaml")
    collector.portfolios = test_portfolios  # 타겟 회사만 교체

    log.info("수집 시작 (primary 키워드만, 약 10-20초 소요)...")
    articles = collector.run()
    log.info(f"수집 완료: {len(articles)}건")

if not articles:
    log.warning("수집된 기사 없음 (네트워크 확인 필요)")
    sys.exit(0)

# ── 2. AI 분류 ─────────────────────────────────────────────────────────────
log.info("Groq AI 분류 시작...")
from classifier_groq import Classifier
signals = Classifier().run(articles)
log.info(f"분류 완료: {len(signals)}건")

for s in signals[:5]:
    print(f"  [{s.get('importance','?').upper()}] {s.get('company','')} | {s.get('type','')} | {s.get('headline','')[:50]}")

# ── 3. HTML 리포트 생성 ────────────────────────────────────────────────────
log.info("Daily HTML 리포트 생성...")
from dispatcher import Dispatcher
d = Dispatcher()
html = d.build_daily_html(signals, drafts_data=None)
out = f"test_daily_output_{datetime.now().strftime('%H%M%S')}.html"
with open(out, "w", encoding="utf-8") as f:
    f.write(html)
log.info(f"리포트 저장: {out}")

# ── 4. 이메일 발송 (선택) ──────────────────────────────────────────────────
ans = input("\n이메일 발송 테스트하시겠습니까? (y/N): ").strip().lower()
if ans == "y":
    log.info("이메일 발송 중...")
    d.send_daily_email(signals)
    log.info("발송 완료")
else:
    log.info("이메일 발송 건너뜀")

log.info("✅ 테스트 완료")
