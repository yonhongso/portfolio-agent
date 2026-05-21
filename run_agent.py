import os, sys, traceback

print("=== Portfolio Agent 시작 ===", flush=True)

# 환경변수 확인
anthropic_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
telegram_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
smtp_pw = os.getenv("SMTP_PASSWORD", "").strip()

print(f"ANTHROPIC_API_KEY: {'✅ 있음' if anthropic_key else '❌ 없음'}", flush=True)
print(f"TELEGRAM_BOT_TOKEN: {'✅ 있음' if telegram_token else '❌ 없음'}", flush=True)
print(f"SMTP_PASSWORD: {'✅ 있음' if smtp_pw else '❌ 없음'}", flush=True)

try:
    from collector import Collector
    from classifier_groq import Classifier
    from dispatcher import Dispatcher, save_dashboard
    print("✅ 모듈 로드 완료", flush=True)
except Exception as e:
    print(f"❌ 모듈 로드 실패: {e}", flush=True)
    traceback.print_exc()
    sys.exit(1)

try:
    collector  = Collector()
    classifier = Classifier()
    dispatcher = Dispatcher()
    print("✅ 객체 초기화 완료", flush=True)
except Exception as e:
    print(f"❌ 초기화 실패: {e}", flush=True)
    traceback.print_exc()
    sys.exit(1)

print("[1/3] 기사 수집 시작...", flush=True)
try:
    articles = collector.run()
    print(f"[1/3] 수집 완료: {len(articles)}건", flush=True)
except Exception as e:
    print(f"❌ 수집 실패: {e}", flush=True)
    traceback.print_exc()
    sys.exit(1)

print("[2/3] 분류 시작...", flush=True)
try:
    signals = classifier.run(articles)
    print(f"[2/3] 분류 완료: {len(signals)}건", flush=True)
except Exception as e:
    print(f"❌ 분류 실패: {e}", flush=True)
    traceback.print_exc()
    sys.exit(1)

print("[3/3] 대시보드 생성...", flush=True)
try:
    save_dashboard(signals)
    print("✅ 대시보드 저장 완료", flush=True)
except Exception as e:
    print(f"❌ 대시보드 생성 실패: {e}", flush=True)
    traceback.print_exc()
    sys.exit(1)

print("[3/3] 텔레그램 알림 발송...", flush=True)
try:
    dispatcher.send_telegram_alerts(signals)
    print("✅ 텔레그램 발송 완료", flush=True)
except Exception as e:
    print(f"❌ 텔레그램 발송 실패: {e}", flush=True)
    traceback.print_exc()

print("[3/3] 이메일 발송...", flush=True)
try:
    dispatcher.send_daily_email(signals)
    print("✅ 이메일 발송 완료", flush=True)
except Exception as e:
    print(f"❌ 이메일 발송 실패: {e}", flush=True)
    traceback.print_exc()

print("=== 완료! ===", flush=True)
