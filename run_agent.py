import os, sys, json, glob, traceback
from datetime import datetime, timedelta, timezone
from dataclasses import asdict


KST = timezone(timedelta(hours=9))

def filter_by_published(signals, days: int) -> list:
    """published_at 기준 days일 이내 시그널만 반환."""
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
    return result

def filter_by_today_kst(signals) -> list:
    """KST 당일 0시 이후 published_at 기사만 반환."""
    now_kst = datetime.now(KST)
    today_midnight_kst = now_kst.replace(hour=0, minute=0, second=0, microsecond=0)
    result = []
    for s in signals:
        try:
            pub = s.published_at
            if not pub:
                continue
            pub_dt = datetime.fromisoformat(pub.replace("Z", "+00:00"))
            if pub_dt.tzinfo is None:
                pub_dt = pub_dt.replace(tzinfo=timezone.utc)
            if pub_dt >= today_midnight_kst:
                result.append(s)
        except Exception:
            pass
    return result

print("=== Portfolio Agent 시작 ===", flush=True)

# 환경변수 확인
anthropic_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
telegram_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
smtp_pw = os.getenv("SMTP_PASSWORD", "").strip()

print(f"ANTHROPIC_API_KEY: {'OK' if anthropic_key else 'MISSING'}", flush=True)
print(f"TELEGRAM_BOT_TOKEN: {'OK' if telegram_token else 'MISSING'}", flush=True)
print(f"SMTP_PASSWORD: {'OK' if smtp_pw else 'MISSING'}", flush=True)

try:
    from collector import Collector
    from classifier_groq import Classifier, ClassifiedSignal
    from dispatcher import Dispatcher, save_dashboard
    from signal_db import SignalDB
    print("module load OK", flush=True)
except Exception as e:
    print(f"module load FAIL: {e}", flush=True)
    traceback.print_exc()
    sys.exit(1)

try:
    collector  = Collector()
    classifier = Classifier()
    dispatcher = Dispatcher()
    print("init OK", flush=True)
except Exception as e:
    print(f"init FAIL: {e}", flush=True)
    traceback.print_exc()
    sys.exit(1)


def save_signals_json(signals, date_str=None):
    os.makedirs("data", exist_ok=True)
    if date_str is None:
        date_str = datetime.now().strftime("%Y-%m-%d")
    path = "data/signals_{}.json".format(date_str)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump([asdict(s) for s in signals], fh, ensure_ascii=False, indent=2)
    print("signals saved: {} ({} items)".format(path, len(signals)), flush=True)
    return path


def load_historical_signals(days):
    result = []
    today = datetime.now().date()
    loaded = 0
    for i in range(days):
        day = today - timedelta(days=i)
        path = "data/signals_{}.json".format(day)
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as fh:
                    data = json.load(fh)
                for d in data:
                    result.append(ClassifiedSignal(**d))
                loaded += 1
            except Exception as ex:
                print("load fail {}: {}".format(path, ex), flush=True)
    print("historical {}-day: {} files, {} signals".format(days, loaded, len(result)), flush=True)
    return result


def filter_by_range(signals, start, end) -> list:
    """published_at이 [start, end] (KST naive) 범위에 드는 기사만 반환."""
    result = []
    for s in signals:
        try:
            pub = s.published_at
            if not pub:
                continue
            pub_dt = datetime.fromisoformat(pub.replace("Z", "+00:00"))
            if pub_dt.tzinfo is None:
                pub_dt = pub_dt.replace(tzinfo=timezone.utc)
            pub_kst = pub_dt.astimezone(KST).replace(tzinfo=None)
            if start <= pub_kst <= end:
                result.append(s)
        except Exception:
            pass
    return result


print("[1/3] collecting articles...", flush=True)
try:
    articles = collector.run()
    print("[1/3] collected: {}".format(len(articles)), flush=True)
except Exception as e:
    print("collect FAIL: {}".format(e), flush=True)
    traceback.print_exc()
    sys.exit(1)

print("[2/3] classifying...", flush=True)
try:
    signals = classifier.run(articles)
    print("[2/3] classified: {}".format(len(signals)), flush=True)
except Exception as e:
    print("classify FAIL: {}".format(e), flush=True)
    traceback.print_exc()
    sys.exit(1)

try:
    save_signals_json(signals)
except Exception as e:
    print("signals save FAIL (continuing): {}".format(e), flush=True)

# ── DB에 오늘 시그널 저장 (누적)
try:
    db = SignalDB()
    saved_count = db.upsert(signals)
    print("DB upsert: {}건 저장 (중복제외)".format(saved_count), flush=True)
    weekly_signals  = db.get_weekly()
    monthly_signals = db.get_monthly()
    print("weekly: {}건 / monthly: {}건 (DB, published_at 기준)".format(
        len(weekly_signals), len(monthly_signals)), flush=True)
except Exception as e:
    print("SignalDB FAIL, JSON fallback: {}".format(e), flush=True)
    weekly_signals  = []
    monthly_signals = []

# ── JSON 파일 fallback (DB 실패 시)
if not weekly_signals:
    try:
        weekly_signals = load_historical_signals(7)
    except Exception:
        weekly_signals = signals
if not monthly_signals:
    try:
        monthly_signals = load_historical_signals(30)
    except Exception:
        monthly_signals = signals

# ── published_at 기준 날짜 필터 적용
_is_monday = datetime.now(KST).weekday() == 0
if _is_monday:
    signals = filter_by_published(signals, days=3)
    print("월요일 모드: 토·일 포함 72시간 기사 수집", flush=True)
else:
    signals = filter_by_today_kst(signals)
_w_start, _w_end = SignalDB.weekly_range()
_m_start, _m_end = SignalDB.monthly_range()
weekly_signals  = filter_by_range(weekly_signals,  _w_start, _w_end)
monthly_signals = filter_by_range(monthly_signals, _m_start, _m_end)

print("filtered → daily: {} / weekly: {} / monthly: {}".format(
    len(signals), len(weekly_signals), len(monthly_signals)), flush=True)

print("[3/3] building dashboard...", flush=True)
try:
    save_dashboard(
        signals,
        weekly_signals=weekly_signals,
        monthly_signals=monthly_signals,
    )
    print("dashboard OK", flush=True)
except Exception as e:
    print("dashboard WARN (AI 인사이트 실패, 기본값 사용): {}".format(e), flush=True)
    try:
        save_dashboard(signals)
        print("dashboard OK (AI 없이)", flush=True)
    except Exception as e2:
        print("dashboard FAIL: {}".format(e2), flush=True)
        traceback.print_exc()

print("[3/3] telegram alerts... (telegram_realtime.yml 워크플로우가 담당)", flush=True)
print("telegram OK", flush=True)

print("[3/3] email...", flush=True)
try:
    dispatcher.send_daily_email(
        signals,
        weekly_signals=weekly_signals,
        monthly_signals=monthly_signals,
    )
    print("email OK", flush=True)
except Exception as e:
    print("email FAIL (계속 진행): {}".format(e), flush=True)

print("=== DONE ===", flush=True)
