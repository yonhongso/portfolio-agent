import os, sys

print("=== Portfolio Agent 시작 ===", flush=True)

# 환경변수 확인
anthropic_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
telegram_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
smtp_pw = os.getenv("SMTP_PASSWORD", "").strip()

print(f"ANTHROPIC_API_KEY: {'✅ 있음' if anthropic_key else '❌ 없음'}", flush=True)
print(f"TELEGRAM_BOT_TOKEN: {'✅ 있음' if telegram_token else '❌ 없음'}", flush=True)
print(f"SMTP_PASSWORD: {'✅ 있음' if smtp_pw else '❌ 없음'}", flush=True)

from collector import Collector
from classifier_groq import Classifier
from dispatcher import Dispatcher, save_dashboard

collector  = Collector()
classifier = Classifier()
dispatcher = Dispatcher()

print("[1/3] 기사 수집 시작...", flush=True)
articles = collector.run()
print(f"[1/3] 수집 완료: {len(articles)}건", flush=True)

print("[2/3] 분류 시작...", flush=True)
signals = classifier.run(articles)
print(f"[2/3] 분류 완료: {len(signals)}건", flush=True)

print("[3/3] 대시보드 생성 및 텔레그램 발송...", flush=True)
save_dashboard(signals)
dispatcher.send_daily(signals)
print("[3/3] 완료!", flush=True)
