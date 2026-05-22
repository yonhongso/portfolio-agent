#!/usr/bin/env python3
"""
seed_signals.py — 과거 시그널 데이터 시딩 (최초 실행 1회)
오늘치 signals JSON을 기반으로 과거 30일치 데이터를 생성합니다.
이미 파일이 있으면 덮어쓰지 않습니다.
"""
import os, json, random
from datetime import datetime, timedelta

def seed():
    today = datetime.now().date()
    today_path = "data/signals_{}.json".format(today)

    if not os.path.exists(today_path):
        print("[seed] 오늘 시그널 파일 없음 — 시딩 불가 (run_agent.py 먼저 실행)")
        return

    with open(today_path, encoding="utf-8") as f:
        today_signals = json.load(f)

    if not today_signals:
        print("[seed] 오늘 시그널 0건 — 시딩 불가")
        return

    seeded = 0
    for i in range(1, 31):                  # 과거 1일~30일
        day = today - timedelta(days=i)
        path = "data/signals_{}.json".format(day)
        if os.path.exists(path):
            continue                         # 이미 있으면 스킵

        # 오늘 시그널 복사, classified_at / published_at 날짜 조정
        past_signals = []
        # 날마다 약간씩 다른 비율로 샘플링 (자연스러운 변화)
        sample_rate = random.uniform(0.6, 1.0)
        sample_n    = max(5, int(len(today_signals) * sample_rate))
        sampled     = random.sample(today_signals, min(sample_n, len(today_signals)))

        day_iso = day.isoformat()
        for sig in sampled:
            s = dict(sig)
            s["classified_at"] = "{}T09:00:00+00:00".format(day_iso)
            s["published_at"]  = "{}T07:30:00+00:00".format(day_iso)
            past_signals.append(s)

        with open(path, "w", encoding="utf-8") as f:
            json.dump(past_signals, f, ensure_ascii=False, indent=2)
        seeded += 1

    print("[seed] 과거 {}일치 시드 파일 생성 완료".format(seeded))

if __name__ == "__main__":
    os.makedirs("data", exist_ok=True)
    seed()
