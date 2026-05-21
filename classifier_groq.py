from __future__ import annotations
import json, logging, os, sqlite3, threading, time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Optional
import requests
from dotenv import load_dotenv
from collector import RawArticle, Portfolio, load_portfolios, load_config

load_dotenv()
logger = logging.getLogger(__name__)

# Anthropic Claude Haiku — 빠르고 저렴, rate limit 없음
CLAUDE_MODEL = "claude-haiku-4-5-20251001"
ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"


# ── 데이터 구조 ───────────────────────────────────────────────────────────────

@dataclass
class ClassifiedSignal:
    portfolio_id: str; portfolio_name: str; url: str; title: str
    summary: str; source: str; source_tier: int; published_at: str
    sentiment: str; signal_type: str; action_flag: str; relevance: str
    summary_ko: str; summary_en: str; classified_at: str; model_used: str; content_hash: str

    @property
    def flag_emoji(self):
        return {"red": "🔴", "yellow": "🟡", "white": "⚪"}.get(self.action_flag, "⚪")


# ── 분류 캐시 (SQLite, thread-safe) ──────────────────────────────────────────

class ClassificationCache:
    """48시간 TTL 캐시. 동일 기사를 재분류하지 않아 API 호출 최소화."""

    def __init__(self, db_path: str = "data/cache.db", ttl_hours: int = 48):
        os.makedirs("data", exist_ok=True)
        self._lock = threading.Lock()
        self._db   = db_path
        self.ttl   = ttl_hours
        with self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS classification_cache "
                "(content_hash TEXT PRIMARY KEY, result_json TEXT NOT NULL, created_at TEXT NOT NULL)"
            )
            conn.execute("PRAGMA journal_mode=WAL")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def get(self, h: str) -> Optional[dict]:
        with self._lock:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT result_json, created_at FROM classification_cache WHERE content_hash=?", (h,)
                ).fetchone()
        if not row:
            return None
        age_h = (datetime.now(timezone.utc) - datetime.fromisoformat(row[1])).total_seconds() / 3600
        if age_h > self.ttl:
            with self._lock:
                with self._connect() as conn:
                    conn.execute("DELETE FROM classification_cache WHERE content_hash=?", (h,))
            return None
        return json.loads(row[0])

    def set(self, h: str, result: dict):
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO classification_cache VALUES (?,?,?)",
                    (h, json.dumps(result, ensure_ascii=False), datetime.now(timezone.utc).isoformat())
                )


# ── Anthropic API 호출 ────────────────────────────────────────────────────────

def _call(prompt: str, retries: int = 3) -> Optional[str]:
    """Anthropic Claude API 호출. 실패 시 지수 백오프."""
    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        logger.error("[Claude] ANTHROPIC_API_KEY 없음")
        return None

    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    payload = {
        "model": CLAUDE_MODEL,
        "max_tokens": 1024,
        "messages": [{"role": "user", "content": prompt}],
    }

    for attempt in range(retries):
        try:
            r = requests.post(ANTHROPIC_API_URL, headers=headers, json=payload, timeout=60)
            if r.status_code == 529:  # Anthropic overloaded
                wait = 10 * (attempt + 1)
                logger.warning(f"[Claude] 과부하 → {wait}초 대기 (시도 {attempt+1}/{retries})")
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json()["content"][0]["text"]
        except requests.exceptions.HTTPError as e:
            wait = 5 * (attempt + 1)
            logger.warning(f"[Claude] HTTP 오류: {e} → {wait}초 대기 (시도 {attempt+1}/{retries})")
            time.sleep(wait)
        except Exception as e:
            wait = 5 * (attempt + 1)
            logger.warning(f"[Claude] 오류: {e} → {wait}초 대기 (시도 {attempt+1}/{retries})")
            time.sleep(wait)

    logger.error(f"[Claude] {retries}회 재시도 모두 실패")
    return None


def _parse(raw: Optional[str]) -> Optional[list]:
    """LLM 응답에서 JSON 배열 추출."""
    if not raw:
        return None
    try:
        t = raw.strip()
        if t.startswith("```"):
            t = t.split("```")[1]
            if t.startswith("json"):
                t = t[4:]
        i = t.find("[")
        return json.loads(t[i:] if i >= 0 else t)
    except Exception:
        return None


# ── 프롬프트 ─────────────────────────────────────────────────────────────────

TRIAGE = """아래 기사가 {company} 투자 모니터링에 관련 있는지 판단하라.
relevant: 회사 사업/재무/인사/규제/경쟁 동향. irrelevant: 무관.
기사: {articles_json}
JSON만 출력: [{{"idx":0,"relevant":true}}]"""

CLASSIFY = """투자심사역 지원 에이전트. 아래 기사를 분류하고 요약 작성.
회사: {company}({stage},{sector}) 지분:{stake}% 이사회:{board_seat} 국가:{country}
Sentiment: Positive|Neutral|Negative|Mixed
Signal Type: 펀딩·밸류에이션|경영진 변동|파트너십·협업|제품·기술 출시|규제·법률 리스크|재무·실적|M&A·Exit|평판·ESG|기타
Action Flag: red=IPO/M&A/C레벨사임/소송/파산, yellow=펀딩협상/임원이동/신제품, white=일반동향/PR
Relevance: High|Medium|Low

요약 규칙:
1. 국내(KR) 기업: summary_ko만 작성, summary_en은 "" 로 둘 것
   - 신문 헤드라인 스타일, 명사형 종결 (동사 종결 금지)
   - 좋은 예: "컴투스, 마이뮤직테이스트 인수" / "업스테이지, 시리즈C 추가 투자 유치 협의"
   - 나쁜 예: "컴투스는 마이뮤직테이스트를 인수했다" (문장형 금지)
2. 해외(KR 아닌) 기업: summary_en만 작성, summary_ko는 "" 로 둘 것
   - Headline style, noun phrase (no conjugated verb endings)
   - Good: "Standard AI raises Series B, appoints new CFO"
   - Bad: "Standard AI has raised Series B and appointed a new CFO."
3. 길이: 15~35자(국문) / 10~15 words(영문) 이내

기사: {articles_json}
JSON만 출력: [{{"idx":0,"sentiment":"...","signal_type":"...","action_flag":"...","relevance":"...","summary_ko":"...","summary_en":"..."}}]"""


# ── 분류기 ────────────────────────────────────────────────────────────────────

class Classifier:
    BATCH_TRIAGE   = 20   # 트리아지 배치 크기 (관련성 필터)
    BATCH_CLASSIFY = 10   # 분류 배치 크기
    MIN_DELAY      = 0.5  # 배치 간 최소 대기 (초)

    def __init__(self, config_path: str = "config.yaml", portfolio_path: str = "portfolio.yaml"):
        self.config     = load_config(config_path)
        self.portfolios = {p.id: p for p in load_portfolios(portfolio_path)}
        self.cache      = ClassificationCache()
        api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
        if not api_key:
            raise ValueError(".env 또는 GitHub Secrets에 ANTHROPIC_API_KEY가 없습니다.")
        logger.info(f"[Claude] 초기화 완료 | 모델: {CLAUDE_MODEL}")

    def run(self, articles: list) -> list[ClassifiedSignal]:
        # 회사별로 그룹핑
        groups: dict[str, list] = {}
        for a in articles:
            groups.setdefault(a.portfolio_id, []).append(a)

        signals: list[ClassifiedSignal] = []

        for pid, arts in groups.items():
            p = self.portfolios.get(pid)
            if not p:
                continue
            logger.info(f"[분류] {p.name}: {len(arts)}건")

            # ① 캐시 히트 먼저 처리
            to_process = []
            for a in arts:
                hit = self.cache.get(a.content_hash)
                if hit:
                    try:
                        signals.append(ClassifiedSignal(**hit))
                    except Exception:
                        to_process.append(a)
                else:
                    to_process.append(a)

            if not to_process:
                logger.info(f"  [{p.name}] 전체 캐시 히트 → API 호출 없음")
                continue

            # ② 트리아지 (관련성 필터) — 배치 처리
            relevant = []
            for i in range(0, len(to_process), self.BATCH_TRIAGE):
                batch = to_process[i:i + self.BATCH_TRIAGE]
                items = [{"idx": j, "title": a.title, "summary": a.summary[:200]}
                         for j, a in enumerate(batch)]
                raw   = _call(TRIAGE.format(
                    company=p.name,
                    articles_json=json.dumps(items, ensure_ascii=False)
                ))
                res   = _parse(raw)
                flags = {r["idx"]: r.get("relevant", False) for r in (res or []) if "idx" in r}
                relevant.extend(
                    a for j, a in enumerate(batch)
                    if flags.get(j, True)
                )
                if i + self.BATCH_TRIAGE < len(to_process):
                    time.sleep(self.MIN_DELAY)

            logger.info(f"  트리아지 통과: {len(relevant)}/{len(to_process)}건")

            # ③ 상세 분류 — 배치 처리
            for i in range(0, len(relevant), self.BATCH_CLASSIFY):
                batch = relevant[i:i + self.BATCH_CLASSIFY]
                items = [{"idx": j, "title": a.title, "summary": a.summary[:400], "source": a.source}
                         for j, a in enumerate(batch)]
                raw   = _call(CLASSIFY.format(
                    company=p.name, stage=p.stage, sector=p.sector,
                    stake=p.our_stake_pct,
                    board_seat="예" if p.board_seat else "아니오",
                    country=getattr(p, "country", "KR"),
                    articles_json=json.dumps(items, ensure_ascii=False)
                ))
                res = _parse(raw)
                out = {r["idx"]: r for r in (res or []) if "idx" in r}

                for j, a in enumerate(batch):
                    r = out.get(j)
                    if not r:
                        continue
                    s = ClassifiedSignal(
                        portfolio_id   = p.id,
                        portfolio_name = p.name,
                        url            = a.url,
                        title          = a.title,
                        summary        = a.summary,
                        source         = a.source,
                        source_tier    = a.source_tier,
                        published_at   = a.published_at.isoformat(),
                        sentiment      = r.get("sentiment",   "Neutral"),
                        signal_type    = r.get("signal_type", "기타"),
                        action_flag    = r.get("action_flag", "white"),
                        relevance      = r.get("relevance",   "Low"),
                        summary_ko     = r.get("summary_ko",  ""),
                        summary_en     = r.get("summary_en",  ""),
                        classified_at  = datetime.now(timezone.utc).isoformat(),
                        model_used     = CLAUDE_MODEL,
                        content_hash   = a.content_hash,
                    )
                    signals.append(s)
                    self.cache.set(a.content_hash, asdict(s))

                if i + self.BATCH_CLASSIFY < len(relevant):
                    time.sleep(self.MIN_DELAY)

        # 최종 집계
        fc = {"red": 0, "yellow": 0, "white": 0}
        for s in signals:
            fc[s.action_flag] = fc.get(s.action_flag, 0) + 1
        logger.info(
            f"[분류 완료] {len(signals)}건 | "
            f"🔴{fc['red']} 🟡{fc['yellow']} ⚪{fc['white']}"
        )
        return signals
