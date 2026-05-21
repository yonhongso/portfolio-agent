"""
test_drafts.py — 커뮤니케이션 초안 기능 테스트
================================================
가상의 🔴 시그널을 만들어 다음 3가지를 테스트합니다:
  1. Groq AI 경영층 문자 초안 생성
  2. Groq AI 포트폴리오사 문의 초안 생성
  3. drafts.html 인터랙티브 페이지 생성
  4. Daily 이메일 발송 (실제 메일)

실행: python test_drafts.py
"""

import logging
import sys
from datetime import datetime, timezone

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

# ── Mock 시그널 생성
from classifier_groq import ClassifiedSignal

mock_signals = [
    ClassifiedSignal(
        portfolio_id    = "kurly",
        portfolio_name  = "컬리",
        url             = "https://www.hankyung.com/economy",   # 실제 기사 URL로 교체 필요
        title           = "컬리, 글로벌 유통사와 M&A 협상 중",
        summary         = "컬리가 글로벌 유통기업과 인수합병 협상을 진행 중인 것으로 알려졌다.",
        source          = "한국경제",
        source_tier     = 1,
        published_at    = datetime.now(timezone.utc).isoformat(),
        sentiment       = "Mixed",
        signal_type     = "M&A·Exit",
        action_flag     = "red",
        relevance       = "High",
        summary_ko      = "컬리가 글로벌 유통기업과 M&A 협상을 진행 중이며, 인수 규모는 약 5,000억 원으로 추정된다.",
        summary_en      = "Kurly is reportedly in M&A negotiations with a global retailer, with the deal estimated at KRW 500bn.",
        classified_at   = datetime.now(timezone.utc).isoformat(),
        model_used      = "test",
        content_hash    = "test_hash_001",
    ),
    ClassifiedSignal(
        portfolio_id    = "portfolio_a",
        portfolio_name  = "테스트A사",
        url             = "https://www.mk.co.kr/economy",          # 실제 기사 URL로 교체 필요
        title           = "테스트A사, IPO 재추진 공식 선언",
        summary         = "테스트A사가 올해 하반기 IPO를 목표로 주관사 선정에 나선다.",
        source          = "매일경제",
        source_tier     = 1,
        published_at    = datetime.now(timezone.utc).isoformat(),
        sentiment       = "Positive",
        signal_type     = "펀딩·밸류에이션",
        action_flag     = "red",
        relevance       = "High",
        summary_ko      = "테스트A사가 하반기 IPO를 공식 선언하고 주관사 선정에 착수했다. 예상 기업가치 1조 원 수준.",
        summary_en      = "Company A has officially announced its IPO plans for H2, initiating lead underwriter selection with an estimated valuation of KRW 1tn.",
        classified_at   = datetime.now(timezone.utc).isoformat(),
        model_used      = "test",
        content_hash    = "test_hash_002",
    ),
    ClassifiedSignal(
        portfolio_id    = "kurly",
        portfolio_name  = "컬리",
        url             = "https://www.thebell.co.kr",              # 실제 기사 URL로 교체 필요
        title           = "컬리 CFO 교체설 불거져",
        summary         = "컬리의 CFO가 개인 사유로 사임할 예정이라는 보도가 나왔다.",
        source          = "더벨",
        source_tier     = 2,
        published_at    = datetime.now(timezone.utc).isoformat(),
        sentiment       = "Negative",
        signal_type     = "경영진 변동",
        action_flag     = "yellow",
        relevance       = "Medium",
        summary_ko      = "컬리 CFO가 개인 사유로 사임을 앞두고 있으며 후임 인선이 진행 중이다.",
        summary_en      = "Kurly's CFO is reportedly set to resign for personal reasons, with a successor search underway.",
        classified_at   = datetime.now(timezone.utc).isoformat(),
        model_used      = "test",
        content_hash    = "test_hash_003",
    ),
    ClassifiedSignal(
        portfolio_id    = "portfolio_a",
        portfolio_name  = "테스트A사",
        url             = "https://techcrunch.com",                 # 실제 기사 URL로 교체 필요
        title           = "테스트A사, 신제품 AI 플랫폼 출시",
        summary         = "테스트A사가 기업용 AI 플랫폼을 출시했다.",
        source          = "테크크런치",
        source_tier     = 2,
        published_at    = datetime.now(timezone.utc).isoformat(),
        sentiment       = "Positive",
        signal_type     = "제품·기술 출시",
        action_flag     = "white",
        relevance       = "Low",
        summary_ko      = "테스트A사가 기업 대상 AI 플랫폼 신제품을 출시하고 베타 서비스를 시작했다.",
        summary_en      = "Company A launched its new enterprise AI platform and commenced beta service.",
        classified_at   = datetime.now(timezone.utc).isoformat(),
        model_used      = "test",
        content_hash    = "test_hash_004",
    ),
]

def main():
    from dispatcher import (
        _draft_contact_messages, build_daily_html,
        save_dashboard, Dispatcher
    )
    from dotenv import load_dotenv
    load_dotenv()

    reds = [s for s in mock_signals if s.action_flag == "red"]
    date_str = datetime.now().strftime("%Y-%m-%d")

    # ── Step 1: 커뮤니케이션 초안 생성
    print("\n" + "="*55)
    print("  STEP 1: Groq AI 커뮤니케이션 초안 생성")
    print("="*55)

    drafts_data = []
    for s in reds:
        print(f"\n[{s.portfolio_name} | {s.signal_type}] 초안 생성 중...")
        msg_exec, msg_portfolio = _draft_contact_messages(s)
        drafts_data.append({
            "portfolio_name": s.portfolio_name,
            "signal_type":    s.signal_type,
            "summary_ko":     s.summary_ko,
            "msg_exec":       msg_exec,
            "msg_portfolio":  msg_portfolio,
        })
        print(f"\n  📤 경영층 문자:\n{msg_exec}")
        print(f"\n  💼 포트폴리오사 문의:\n{msg_portfolio}")
        print("-"*50)

    # ── Step 2: dashboard.html 저장 (Tab1: 현황 + Tab2: 커뮤니케이션 초안)
    print("\n" + "="*55)
    print("  STEP 2: dashboard.html 저장 (탭 구조)")
    print("="*55)
    path = save_dashboard(mock_signals, drafts_data=drafts_data or None)
    print(f"  ✅ 저장 완료: {path}")

    # ── Step 3: Daily 이메일 발송
    print("\n" + "="*55)
    print("  STEP 3: Daily 이메일 발송 테스트")
    print("="*55)
    try:
        disp = Dispatcher()
        disp.send_daily_email(mock_signals)
        print("  ✅ 이메일 발송 완료 — 받은편지함 확인")
    except Exception as e:
        print(f"  ⚠️  이메일 발송 실패: {e}")
        print("  (SMTP 설정 확인 필요 — dashboard.html은 정상 생성됨)")

    print("\n" + "="*55)
    print("  테스트 완료!")
    print(f"  dashboard.html → C:\\portfolio-agent\\dashboard.html 열어보세요")
    print(f"  📱 커뮤니케이션 초안 탭에서 초안 확인·복사 가능")
    print("="*55 + "\n")


if __name__ == "__main__":
    main()
