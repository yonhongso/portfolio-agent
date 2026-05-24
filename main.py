"""
main.py — 파이프라인 오케스트레이터
======================================
실행 모드:
  python main.py                    # 1회 즉시 실행 (기본)
  python main.py --mode schedule    # 내장 스케줄러로 상시 운영
  python main.py --mode daily       # Daily 이메일만 즉시 발송
  python main.py --mode weekly      # Weekly 이메일만 즉시 발송
  python main.py --mode monthly     # Monthly 리포트만 즉시 발송
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone

import schedule
from dotenv import load_dotenv

load_dotenv()

from collector       import Collector
from classifier_groq import Classifier
from dispatcher      import Dispatcher, save_dashboard


def _filter_by_published(signals, days: int) -> list:
    """published_at 기준 days일 이내 시그널만 반환 (Telegram 과거 기사 방지)."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    result = []
    for s in signals:
        try:
            pub = s.published_at
            if not pub:
                continue
            pub_dt = datetime.fromisoformat(pub.replace("Z", "+00:00"))
            if pub_dt.tzinfo is None:
                pub_dt = pub_dt.replace(tzinfo=timezone.utc)
            if pub_dt >= cutoff:
                result.append(s)
        except Exception:
            pass
    return result or signals  # 전부 필터링되면 원본 반환


# ── 로깅 ──────────────────────────────────────────────────────────────────────

def setup_logging(log_file: str = "logs/agent.log", level: str = "INFO"):
    os.makedirs("logs", exist_ok=True)
    fmt = "%(asctime)s [%(levelname)s] %(name)s — %(message)s"
    logging.basicConfig(
        level=getattr(logging, level),
        format=fmt,
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_file, encoding="utf-8"),
        ],
    )

logger = logging.getLogger(__name__)

# ── 중복 실행 방지 ─────────────────────────────────────────────────────────────

_running = False


def _safe_run(fn, label: str, dispatcher=None):
    """실행 중 예외가 나도 프로세스를 죽이지 않음. 텔레그램으로 장애 알림."""
    global _running
    if _running:
        logger.warning(f"[{label}] 이전 실행이 아직 진행 중 → 건너뜀")
        return
    _running = True
    start = time.time()
    try:
        logger.info(f"[{label}] 시작")
        fn()
        logger.info(f"[{label}] 완료 ({time.time()-start:.0f}초)")
    except Exception:
        tb = traceback.format_exc()
        logger.error(f"[{label}] 실패 ({time.time()-start:.0f}초)\n{tb}")
        if dispatcher:
            try:
                dispatcher._send_telegram(f"🚨 *파이프라인 오류* [{label}]\n```{tb[-800:]}```")
            except Exception:
                pass
    finally:
        _running = False


# ── 파이프라인 단계 ───────────────────────────────────────────────────────────

def _collect_classify(collector, classifier) -> list:
    logger.info("=" * 60)
    logger.info(f"파이프라인 시작: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    articles = collector.run()
    if not articles:
        logger.info("수집된 기사 없음 → 종료")
        return []
    signals = classifier.run(articles)
    logger.info(f"분류 완료: {len(signals)}건")
    return signals


def _save_and_refresh(signals, dispatcher):
    """DB 저장 + 대시보드 갱신 (공통)."""
    from signal_db import SignalDB
    db = SignalDB()
    db.upsert(signals)
    try:
        weekly_signals  = db.get_weekly()  or signals
        monthly_signals = db.get_monthly() or signals
        save_dashboard(signals,
                       weekly_signals=weekly_signals,
                       monthly_signals=monthly_signals)
    except Exception as e:
        logger.warning(f"대시보드 갱신 실패 (무시): {e}")


def run_once(collector, classifier, dispatcher):
    signals = _collect_classify(collector, classifier)
    if not signals:
        return
    # 월요일은 토·일 포함 72시간, 평일은 24시간 이내 기사만 발송
    _is_monday = datetime.now(timezone.utc).weekday() == 0
    fresh = _filter_by_published(signals, days=3 if _is_monday else 1)
    dispatcher.send_telegram_alerts(fresh)
    dispatcher.send_daily_email(fresh)
    _save_and_refresh(signals, dispatcher)


def run_daily(collector, classifier, dispatcher):
    signals = _collect_classify(collector, classifier)
    if not signals:
        return
    _is_monday = datetime.now(timezone.utc).weekday() == 0
    fresh = _filter_by_published(signals, days=3 if _is_monday else 1)
    dispatcher.send_telegram_alerts(fresh)
    dispatcher.send_daily_email(fresh)
    _save_and_refresh(signals, dispatcher)


def run_weekly(collector, classifier, dispatcher):
    signals = _collect_classify(collector, classifier)
    if signals:
        dispatcher.send_weekly_email(signals)
        _save_and_refresh(signals, dispatcher)


def run_monthly(collector, classifier, dispatcher):
    signals = _collect_classify(collector, classifier)
    if signals:
        dispatcher.send_monthly_email(signals)
        _save_and_refresh(signals, dispatcher)


# ── 스케줄러 ──────────────────────────────────────────────────────────────────

def start_scheduler(collector, classifier, dispatcher):
    logger.info("스케줄러 시작")

    schedule.every(1).hours.do(
        lambda: _safe_run(lambda: run_once(collector, classifier, dispatcher), "실시간 수집", dispatcher))

    schedule.every().day.at("08:00").do(
        lambda: _safe_run(lambda: run_daily(collector, classifier, dispatcher), "Daily 브리핑", dispatcher))

    schedule.every().monday.at("09:00").do(
        lambda: _safe_run(lambda: run_weekly(collector, classifier, dispatcher), "Weekly 리포트", dispatcher))

    def _monthly_check():
        if datetime.now().day == 1:
            _safe_run(lambda: run_monthly(collector, classifier, dispatcher), "Monthly 리포트", dispatcher)

    schedule.every().day.at("09:01").do(_monthly_check)
    schedule.every(30).seconds.do(dispatcher.poll_commands)

    logger.info("스케줄 등록: 매시간 수집 | 08:00 Daily | 월요일 Weekly | 매월1일 Monthly")

    while True:
        try:
            schedule.run_pending()
        except Exception as e:
            logger.error(f"스케줄러 루프 오류 (계속 실행): {e}")
        time.sleep(30)


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="포트폴리오 인텔리전스 에이전트")
    parser.add_argument("--mode", choices=["once", "schedule", "daily", "weekly", "monthly"], default="once")
    parser.add_argument("--portfolio", default="portfolio.yaml")
    parser.add_argument("--config",    default="config.yaml")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args()

    setup_logging(level=args.log_level)
    logger.info(f"에이전트 시작 | mode={args.mode} | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    collector  = Collector(args.portfolio, args.config)
    classifier = Classifier(args.config, args.portfolio)
    dispatcher = Dispatcher(args.config)

    {
        "once":     lambda: _safe_run(lambda: run_once(collector, classifier, dispatcher),    "once",    dispatcher),
        "daily":    lambda: _safe_run(lambda: run_daily(collector, classifier, dispatcher),   "daily",   dispatcher),
        "weekly":   lambda: _safe_run(lambda: run_weekly(collector, classifier, dispatcher),  "weekly",  dispatcher),
        "monthly":  lambda: _safe_run(lambda: run_monthly(collector, classifier, dispatcher), "monthly", dispatcher),
        "schedule": lambda: start_scheduler(collector, classifier, dispatcher),
    }[args.mode]()


if __name__ == "__main__":
    main()
