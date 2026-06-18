"""
run_telegram_alert.py — 텔레그램 수시 알림 전용 스크립트
=========================================================
- 이메일 발송 없음
- 대시보드 업데이트 없음
- Red 시그널만 텔레그램 발송
- Groq 캐시(48h) 활용 → 이미 분류된 기사는 API 재호출 없음
"""
import os
import sys
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv

load_dotenv()

from collector import Collector
from classifier_groq import Classifier
from dispatcher import Dispatcher


def main():
    collector  = Collector("portfolio.yaml", "config.yaml")
    classifier = Classifier("config.yaml", "portfolio.yaml")
    dispatcher = Dispatcher("config.yaml")

    articles = collector.run()
    if not articles:
        print("수집된 기사 없음 → 종료")
        return

    signals = classifier.run(articles)
    if not signals:
        print("분류된 시그널 없음 → 종료")
        return

    # 최근 2시간 이내 발행 기사만 (수시 알림 중복 방지)
    # 단, 하루 첫 회차(KST 09시 이전)는 밤사이 기사까지 12시간 커버
    kst_now = datetime.now(timezone(timedelta(hours=9)))
    window_h = 12 if kst_now.hour < 9 else 2
    cutoff = datetime.now(timezone.utc) - timedelta(hours=window_h)
    fresh = []
    for s in signals:
        try:
            pub = s.published_at
            if not pub:
                continue
            pub_dt = datetime.fromisoformat(pub.replace("Z", "+00:00"))
            if pub_dt.tzinfo is None:
                pub_dt = pub_dt.replace(tzinfo=timezone.utc)
            if pub_dt >= cutoff:
                fresh.append(s)
        except Exception:
            pass

    if not fresh:
        print(f"최근 {window_h}시간 내 신규 기사 없음 ({len(signals)}건 수집됨) → 종료")
        return

    print(f"전체 시그널 {len(fresh)}건 텔레그램 발송")
    dispatcher.send_telegram_realtime(fresh)


if __name__ == "__main__":
    main()
