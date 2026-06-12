"""
signal_db.py — SQLite 기반 시그널 누적 저장소
==============================================
Weekly / Monthly 라이브 대시보드를 위해 과거 시그널을 DB에 저장.

테이블: signals
  - id, portfolio_id, portfolio_name, url, title, summary_ko, summary_en
  - source, source_tier, published_at, sentiment, signal_type, action_flag
  - relevance, classified_at, content_hash
  - saved_at (로컬 저장 시각)

사용:
  db = SignalDB()
  db.upsert(signals)            # 시그널 저장/업데이트 (content_hash 기준 중복 제거)
  db.get_recent(days=1)         # 최근 N일치 반환
  db.get_weekly()               # 최근 7일치
  db.get_monthly()              # 최근 30일치
"""

import calendar
import logging
import os
import sqlite3
from datetime import date, datetime, timedelta, timezone
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(__file__), "signals.db")

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS signals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    content_hash    TEXT    UNIQUE NOT NULL,
    portfolio_id    TEXT    NOT NULL,
    portfolio_name  TEXT    NOT NULL,
    url             TEXT,
    title           TEXT,
    summary_ko      TEXT,
    summary_en      TEXT,
    source          TEXT,
    source_tier     INTEGER,
    published_at    TEXT,
    sentiment       TEXT,
    signal_type     TEXT,
    action_flag     TEXT,
    relevance       TEXT,
    classified_at   TEXT,
    model_used      TEXT,
    saved_at        TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_saved_at    ON signals(saved_at);
CREATE INDEX IF NOT EXISTS idx_action_flag ON signals(action_flag);
CREATE INDEX IF NOT EXISTS idx_portfolio   ON signals(portfolio_id);
"""

class SignalDB:
    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")    # 동시 읽기/쓰기 허용
        conn.execute("PRAGMA synchronous=NORMAL")  # WAL 최적 설정
        return conn

    def _init_db(self):
        with self._conn() as conn:
            conn.executescript(CREATE_SQL)
        logger.debug(f"[SignalDB] 초기화 완료: {self.db_path}")

    def upsert(self, signals: list) -> int:
        """시그널 목록을 DB에 저장. content_hash 중복 시 IGNORE. 저장 건수 반환."""
        now = datetime.now(timezone.utc).isoformat()
        rows = []
        for s in signals:
            rows.append((
                s.content_hash,
                s.portfolio_id,
                s.portfolio_name,
                getattr(s, "url", ""),
                getattr(s, "title", ""),
                getattr(s, "summary_ko", ""),
                getattr(s, "summary_en", ""),
                getattr(s, "source", ""),
                getattr(s, "source_tier", 2),
                getattr(s, "published_at", ""),
                getattr(s, "sentiment", ""),
                getattr(s, "signal_type", ""),
                getattr(s, "action_flag", "white"),
                getattr(s, "relevance", ""),
                getattr(s, "classified_at", ""),
                getattr(s, "model_used", ""),
                now,
            ))
        with self._conn() as conn:
            cur = conn.executemany("""
                INSERT OR IGNORE INTO signals
                  (content_hash, portfolio_id, portfolio_name, url, title,
                   summary_ko, summary_en, source, source_tier, published_at,
                   sentiment, signal_type, action_flag, relevance, classified_at,
                   model_used, saved_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, rows)
            saved = cur.rowcount
        logger.info(f"[SignalDB] {saved}/{len(signals)}건 저장 (중복 제외)")
        return saved

    def _fetch(self, since_iso: str, flags: Optional[list] = None) -> list:
        """since_iso 이후 시그널 반환. flags 필터 옵션."""
        from classifier_groq import ClassifiedSignal
        with self._conn() as conn:
            if flags:
                placeholders = ",".join("?" * len(flags))
                rows = conn.execute(f"""
                    SELECT * FROM signals
                    WHERE saved_at >= ? AND action_flag IN ({placeholders})
                    ORDER BY saved_at DESC
                """, [since_iso] + flags).fetchall()
            else:
                rows = conn.execute("""
                    SELECT * FROM signals
                    WHERE saved_at >= ?
                    ORDER BY saved_at DESC
                """, [since_iso]).fetchall()

        result = []
        for r in rows:
            try:
                result.append(ClassifiedSignal(
                    portfolio_id   = r["portfolio_id"],
                    portfolio_name = r["portfolio_name"],
                    url            = r["url"] or "",
                    title          = r["title"] or "",
                    summary        = r["summary_ko"] or "",
                    source         = r["source"] or "",
                    source_tier    = r["source_tier"] or 2,
                    published_at   = r["published_at"] or "",
                    sentiment      = r["sentiment"] or "",
                    signal_type    = r["signal_type"] or "기타",
                    action_flag    = r["action_flag"] or "white",
                    relevance      = r["relevance"] or "",
                    summary_ko     = r["summary_ko"] or "",
                    summary_en     = r["summary_en"] or "",
                    classified_at  = r["classified_at"] or "",
                    model_used     = r["model_used"] or "",
                    content_hash   = r["content_hash"],
                ))
            except Exception as e:
                logger.warning(f"[SignalDB] 행 복원 실패: {e}")
        return result

    def get_recent(self, days: int = 1) -> list:
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        return self._fetch(since)

    def get_daily(self) -> list:
        """Daily 탭 전용 — published_at 기준으로 최근 1일(월요일 3일) 이내 기사만 반환.
        saved_at 기준인 get_recent()와 달리, seed된 과거 기사가 섞이지 않음."""
        from classifier_groq import ClassifiedSignal
        is_monday = datetime.now(timezone.utc).weekday() == 0
        days = 3 if is_monday else 1
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        with self._conn() as conn:
            rows = conn.execute("""
                SELECT * FROM signals
                WHERE published_at >= ?
                ORDER BY published_at DESC
            """, [cutoff]).fetchall()
        result = []
        for r in rows:
            try:
                result.append(ClassifiedSignal(
                    portfolio_id   = r["portfolio_id"],
                    portfolio_name = r["portfolio_name"],
                    url            = r["url"] or "",
                    title          = r["title"] or "",
                    summary        = r["summary_ko"] or "",
                    source         = r["source"] or "",
                    source_tier    = r["source_tier"] or 2,
                    published_at   = r["published_at"] or "",
                    sentiment      = r["sentiment"] or "",
                    signal_type    = r["signal_type"] or "기타",
                    action_flag    = r["action_flag"] or "white",
                    relevance      = r["relevance"] or "",
                    summary_ko     = r["summary_ko"] or "",
                    summary_en     = r["summary_en"] or "",
                    classified_at  = r["classified_at"] or "",
                    model_used     = r["model_used"] or "",
                    content_hash   = r["content_hash"],
                ))
            except Exception as e:
                logger.warning(f"[SignalDB] 행 복원 실패: {e}")
        return result

    @staticmethod
    def weekly_range() -> Tuple[datetime, datetime]:
        """가장 최근 완결된 주: 월요일 00:00 ~ 일요일 23:59 (KST 기준).
        일요일에 실행되면 그날로 끝나는 주(월~오늘), 평일이면 지난주를 반환."""
        kst = timezone(timedelta(hours=9))
        today = datetime.now(kst).replace(tzinfo=None)
        days_since_sunday = (today.weekday() + 1) % 7  # 일=0, 월=1 … 토=6
        sunday = (today - timedelta(days=days_since_sunday)).replace(
            hour=23, minute=59, second=59, microsecond=0)
        monday = (sunday - timedelta(days=6)).replace(
            hour=0, minute=0, second=0, microsecond=0)
        return monday, sunday

    @staticmethod
    def monthly_range() -> Tuple[datetime, datetime]:
        """전월 1일 00:00 ~ 전월 말일 23:59 반환 (KST 기준, 완결된 달)."""
        kst = timezone(timedelta(hours=9))
        today = datetime.now(kst).replace(tzinfo=None)
        if today.month == 1:
            year, month = today.year - 1, 12
        else:
            year, month = today.year, today.month - 1
        last_day = calendar.monthrange(year, month)[1]
        return (datetime(year, month, 1, 0, 0, 0),
                datetime(year, month, last_day, 23, 59, 59))

    def get_weekly(self) -> list:
        """Weekly 탭 — 최근 완결 주(월~일) published_at 필터."""
        start, end = self.weekly_range()
        return self._fetch_by_range(start, end)

    def get_monthly(self) -> list:
        """Monthly 탭 — 전월(1일~말일) published_at 필터."""
        start, end = self.monthly_range()
        return self._fetch_by_range(start, end)

    def _fetch_by_range(self, start: datetime, end: datetime) -> list:
        """published_at 기준 범위 필터."""
        from classifier_groq import ClassifiedSignal
        with self._conn() as conn:
            rows = conn.execute("""
                SELECT * FROM signals
                WHERE published_at >= ? AND published_at <= ?
                ORDER BY published_at DESC
            """, [start.isoformat(), end.isoformat()]).fetchall()
        result = []
        for r in rows:
            try:
                result.append(ClassifiedSignal(
                    portfolio_id   = r["portfolio_id"],
                    portfolio_name = r["portfolio_name"],
                    url            = r["url"] or "",
                    title          = r["title"] or "",
                    summary        = r["summary_ko"] or "",
                    source         = r["source"] or "",
                    source_tier    = r["source_tier"] or 2,
                    published_at   = r["published_at"] or "",
                    sentiment      = r["sentiment"] or "",
                    signal_type    = r["signal_type"] or "기타",
                    action_flag    = r["action_flag"] or "white",
                    relevance      = r["relevance"] or "",
                    summary_ko     = r["summary_ko"] or "",
                    summary_en     = r["summary_en"] or "",
                    classified_at  = r["classified_at"] or "",
                    model_used     = r["model_used"] or "",
                    content_hash   = r["content_hash"],
                ))
            except Exception as e:
                logger.warning(f"[SignalDB] 행 복원 실패: {e}")
        return result

    def summary_stats(self, days: int = 30) -> dict:
        """기간별 통계 (대시보드 헤더용)."""
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        with self._conn() as conn:
            row = conn.execute("""
                SELECT
                  COUNT(*)                                         AS total,
                  SUM(action_flag='red')                          AS reds,
                  SUM(action_flag='yellow')                       AS yellows,
                  SUM(action_flag='white')                        AS whites,
                  COUNT(DISTINCT portfolio_id)                    AS companies
                FROM signals WHERE saved_at >= ?
            """, [since]).fetchone()
        return dict(row) if row else {}

    def top_companies(self, days: int = 7, limit: int = 5) -> list[dict]:
        """이슈 많은 상위 회사 (주간 대시보드용)."""
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        with self._conn() as conn:
            rows = conn.execute("""
                SELECT portfolio_name,
                       COUNT(*) as total,
                       SUM(action_flag='red') as reds,
                       SUM(action_flag='yellow') as yellows
                FROM signals WHERE saved_at >= ?
                GROUP BY portfolio_name
                ORDER BY reds DESC, yellows DESC, total DESC
                LIMIT ?
            """, [since, limit]).fetchall()
        return [dict(r) for r in rows]
