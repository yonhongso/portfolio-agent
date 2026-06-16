"""
telegram_log_collector.py
=========================
텔레그램 봇 /log 명령어를 폴링하여 data/action_log.json 에 저장.

사용법:
  - 텔레그램에서 봇에게 /log [내용] 형식으로 메시지 전송
  - GitHub Actions (telegram_log.yml) 에서 30분마다 실행
  - 수집된 로그는 Monthly M4 섹션에 표시됨

/log 명령어 형식:
  /log 컬리 평판 이슈 경영진과 대응 협의 완료
  /log 업스테이지 추가투자 검토 착수 (내부 IR 검토 의뢰)
"""
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")
LOG_FILE  = Path("data/action_log.json")
OFFSET_FILE = Path("data/telegram_log_offset.txt")

KST = timezone(timedelta(hours=9))


def get_updates(offset: int = 0) -> list:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
    resp = requests.get(url, params={"offset": offset, "timeout": 10}, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        return []
    return data.get("result", [])


def load_log() -> list:
    if LOG_FILE.exists():
        try:
            return json.loads(LOG_FILE.read_text(encoding="utf-8"))
        except Exception:
            return []
    return []


def save_log(entries: list) -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    LOG_FILE.write_text(
        json.dumps(entries, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )


def load_offset() -> int:
    if OFFSET_FILE.exists():
        try:
            return int(OFFSET_FILE.read_text().strip())
        except Exception:
            return 0
    return 0


def save_offset(offset: int) -> None:
    OFFSET_FILE.parent.mkdir(parents=True, exist_ok=True)
    OFFSET_FILE.write_text(str(offset))


def send_reply(chat_id, text: str) -> None:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": chat_id, "text": text}, timeout=10)
    except Exception:
        pass


def main():
    if not BOT_TOKEN:
        print("TELEGRAM_BOT_TOKEN 없음 → 종료")
        sys.exit(0)

    offset = load_offset()
    updates = get_updates(offset)

    if not updates:
        print("새 메시지 없음")
        return

    log_entries = load_log()
    new_count = 0
    max_update_id = offset

    for upd in updates:
        upd_id = upd.get("update_id", 0)
        max_update_id = max(max_update_id, upd_id)

        msg = upd.get("message") or upd.get("edited_message")
        if not msg:
            continue

        text = (msg.get("text") or "").strip()
        if not text.lower().startswith("/log"):
            continue

        # /log 뒤 내용 추출
        content = text[4:].strip()
        if not content:
            send_reply(msg["chat"]["id"], "⚠️ 내용을 입력해주세요.\n예시: /log 컬리 경영진 미팅 완료")
            continue

        # KST 타임스탬프
        ts = datetime.now(KST).strftime("%Y-%m-%d %H:%M")
        from_user = msg.get("from", {})
        author = from_user.get("first_name", "") or from_user.get("username", "")

        entry = {
            "ts": ts,
            "author": author,
            "content": content,
        }
        log_entries.append(entry)
        new_count += 1

        send_reply(
            msg["chat"]["id"],
            f"✅ 액션 로그 저장됨\n📅 {ts}\n📝 {content}"
        )
        print(f"[LOG] {ts} | {author} | {content}")

    # 오프셋 저장 (다음 실행 시 중복 방지)
    save_offset(max_update_id + 1)

    if new_count > 0:
        # 최신 순 정렬 (최대 100건 유지)
        log_entries = sorted(log_entries, key=lambda x: x.get("ts", ""), reverse=True)[:100]
        save_log(log_entries)
        print(f"총 {new_count}건 로그 저장 완료")
    else:
        print("신규 /log 명령어 없음")


if __name__ == "__main__":
    main()
