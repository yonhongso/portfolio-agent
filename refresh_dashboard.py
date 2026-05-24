"""Groq 호출 없이 DB에서 읽어 dashboard.html만 갱신."""
from signal_db import SignalDB
from dispatcher import save_dashboard

db = SignalDB()
daily   = db.get_daily()   or []   # published_at 기준 — 과거 seed 기사 자동 제외
weekly  = db.get_weekly()  or []
monthly = db.get_monthly() or []

save_dashboard(daily, weekly_signals=weekly, monthly_signals=monthly)
print(f"완료 — daily: {len(daily)}건 / weekly: {len(weekly)}건 / monthly: {len(monthly)}건")
