"""
classifier_gemini.py — 분류 에이전트 (Google Gemini 신버전)
google-genai 패키지 기반 (구버전 google-generativeai 대체)
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Optional

from google import genai                # pip install google-genai
from google.genai import types
import yaml
from dotenv import load_dotenv

from collector import RawArticle, Portfolio, load_portfolios, load_config

load_dotenv()
logger = logging.getLogger(__name__)

GEMINI_MODEL = "gemini-2.0-flash"

# =============================================================================
# 데이터 모델
# =============================================================================

@dataclass
class ClassifiedSignal:
    portfolio_id: str
    portfolio_name: str
    url: str
    title: str
    summary: str
    source: str
    source_tier: int
    published_at: str
    sentiment: str
    signal_type: str
    action_flag: str
    relevance: str
    summary_ko: str
    summary_en: str
    classified_at: str
    model_used: str
    content_hash: str

    @property
    def flag_emoji(self) -> str:
        return {"red": "🔴", "yellow": "🟡", "white": "⚪"}.get(self.action_flag, "⚪")


# =============================================================================
# 캐시
# =============================================================================

class ClassificationCache:
    def __init__(self, db_path: str = "data/cache.db", ttl_hours: int = 48):
        os.makedirs("data", exist_ok=True)
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.ttl_hours = ttl_hours
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS classification_cache (
                content_hash TEXT PRIMARY KEY,
                result_json  TEXT NOT NULL,
                created_at   TEXT NOT NULL
            )
        """)
        self.conn.commit()

    def get(self, content_hash: str) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT result_json, created_at FROM classification_cache WHERE content_hash=?",
            (content_hash,)
        ).fetchone()
        if not row:
            return None
        created = datetime.fromisoformat(row[1])
        age_h = (datetime.now(timezone.utc) - created).total_seconds() / 3600
        if age_h > self.ttl_hours:
            self.conn.execute(
                "DELETE FROM classification_cache WHERE content_hash=?", (content_hash,)
            )
            return None
        return json.loads(row[0])

    def set(self, content_hash: str, result: dict):
        self.conn.execute(
            "INSERT OR REPLACE INTO classification_cache VALUES (?,?,?)",
            (content_hash, json.dumps(result, ensure_ascii=False),
             datetime.now(timezone.utc).isoformat())
        )
        self.conn.commit()


# =============================================================================
# Gemini 호출 헬퍼 (신버전 google-genai)
# =============================================================================

_client: Optional[genai.Client] = None

def get_client() -> genai.Client:
    global _client
    if _client is None:
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise ValueError(".env 파일에 GEMINI_API_KEY 가 없습니다.")
        _client = genai.Client(api_key=api_key)
    return _client


def _call_gemini(prompt: str, retries: int = 3) -> Optional[str]:
    client = get_client()
    for attempt in range(retries):
        try:
            response = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0,
                    max_output_tokens=1024,
                )
            )
            return response.text
        except Exception as e:
            err = str(e)
            if "429" in err or "quota" in err.lower():
                wait = 60 * (attempt + 1)
                logger.warning(f"[Gemini] 한도 초과 → {wait}초 대기 후 재시도")
                time.sleep(wait)
            else:
                logger.warning(f"[Gemini] 오류 (시도 {attempt+1}): {e}")
                time.sleep(6)
    return None


def _parse_json(raw: str) -> Optional[list]:
    """Gemini 응답에서 JSON 배열 파싱. 코드블록 감싸임 처리 포함."""
    try:
        text = raw.strip()
        # ```json ... ``` 또는 ``` ... ``` 블록 제거
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        text = text.strip()
        return json.loads(text)
    except Exception as e:
        logger.warning(f"[JSON 파싱 오류] {e}\n원문: {raw[:200]}")
        return None


# =============================================================================
# Step 1 — 트리아지 (관련성 판단)
# 왜 필요한가: Gemini 분류 호출 전에 무관한 기사를 걸러내어 API 호출 횟수 절감
# =============================================================================

TRIAGE_PROMPT = """\
아래 기사 목록에서 각 기사가 {company} 투자 모니터링에 관련 있는지 판단하라.

- relevant: 회사 사업/재무/인사/규제/경쟁 동향 해당
- irrelevant: 동명이인, 무관 업계, 단순 언급

기사 목록:
{articles_json}

JSON 배열만 출력. 다른 텍스트 없이.
형식: [{{"idx": 0, "relevant": true}}, {{"idx": 1, "relevant": false}}]
"""

def triage_batch(articles: list[RawArticle], portfolio: Portfolio) -> list[bool]:
    items = [{"idx": i, "title": a.title, "summary": a.summary[:200]}
             for i, a in enumerate(articles)]
    prompt = TRIAGE_PROMPT.format(
        company=portfolio.name,
        articles_json=json.dumps(items, ensure_ascii=False),
    )
    raw = _call_gemini(prompt)
    if not raw:
        return [True] * len(articles)
    results = _parse_json(raw)
    if not results:
        return [True] * len(articles)
    flags = [False] * len(articles)
    for r in results:
        if isinstance(r, dict) and "idx" in r:
            flags[r["idx"]] = r.get("relevant", False)
    return flags


# =============================================================================
# Step 2 — 3축 분류
# 왜 필요한가: 수집된 기사를 감성/신호유형/액션플래그로 분류하여
#             투자심사역이 즉시 의사결정에 활용할 수 있는 형태로 가공
# =============================================================================

CLASSIFY_PROMPT = """\
당신은 투자심사역을 지원하는 포트폴리오 인텔리전스 에이전트입니다.
아래 기사들을 3축으로 분류하고 투자 관련 한줄 요약을 작성하라.

## 포트폴리오사 정보
회사명: {company} ({stage}, {sector})
당사 지분: {stake}%
이사회 참여: {board_seat}
참고 메모: {context_memo}

## 분류 기준

Sentiment: Positive | Neutral | Negative | Mixed

Signal Type (하나 선택):
펀딩·밸류에이션 | 경영진 변동 | 파트너십·협업 | 제품·기술 출시 |
규제·법률 리스크 | 재무·실적 | M&A·Exit | 평판·ESG | 기타

Action Flag:
- red: IPO, M&A, C레벨 사임, 소송, 파산 등 즉각적 투자 판단 영향
- yellow: 펀딩 협상, 임원 이동, MOU, 신제품, 단발 부정 보도
- white: 일반 업계 동향, PR, 채용

Relevance: High | Medium | Low

## 분류할 기사
{articles_json}

## 출력 형식
JSON 배열만. 코드블록 없이.
[{{"idx":0,"sentiment":"...","signal_type":"...","action_flag":"...","relevance":"...","summary_ko":"한국어 1~2문장","summary_en":"English 1-2 sentences"}}]
"""

def classify_batch(articles: list[RawArticle], portfolio: Portfolio) -> list[Optional[dict]]:
    items = [{"idx": i, "title": a.title, "summary": a.summary[:400], "source": a.source}
             for i, a in enumerate(articles)]
    prompt = CLASSIFY_PROMPT.format(
        company=portfolio.name,
        stage=portfolio.stage,
        sector=portfolio.sector,
        stake=portfolio.our_stake_pct,
        board_seat="예" if portfolio.board_seat else "아니오",
        context_memo=portfolio.context_memo or "없음",
        articles_json=json.dumps(items, ensure_ascii=False),
    )
    raw = _call_gemini(prompt)
    if not raw:
        return [None] * len(articles)
    results = _parse_json(raw)
    if not results:
        return [None] * len(articles)
    out = [None] * len(articles)
    for r in results:
        if isinstance(r, dict) and "idx" in r:
            out[r["idx"]] = r
    return out


# =============================================================================
# 분류 오케스트레이터
# =============================================================================

class Classifier:
    BATCH_TRIAGE   = 5
    BATCH_CLASSIFY = 5

    def __init__(self, config_path: str = "config.yaml",
                 portfolio_path: str = "portfolio.yaml"):
        self.config     = load_config(config_path)
        self.portfolios = {p.id: p for p in load_portfolios(portfolio_path)}
        self.cache      = ClassificationCache()
        # 클라이언트 초기화 확인
        get_client()
        logger.info(f"[Gemini] 초기화 완료 | 모델: {GEMINI_MODEL}")

    def run(self, articles: list[RawArticle]) -> list[ClassifiedSignal]:
        groups: dict[str, list[RawArticle]] = {}
        for a in articles:
            groups.setdefault(a.portfolio_id, []).append(a)

        signals: list[ClassifiedSignal] = []

        for pid, arts in groups.items():
            portfolio = self.portfolios.get(pid)
            if not portfolio:
                continue

            logger.info(f"[분류] {portfolio.name}: {len(arts)}건")

            # 캐시 확인
            to_process = []
            for a in arts:
                hit = self.cache.get(a.content_hash)
                if hit:
                    signals.append(ClassifiedSignal(**hit))
                    logger.debug(f"[캐시 히트] {a.content_hash}")
                else:
                    to_process.append(a)

            if not to_process:
                continue

            # Step 1: 트리아지
            relevant = []
            for i in range(0, len(to_process), self.BATCH_TRIAGE):
                batch = to_process[i:i + self.BATCH_TRIAGE]
                flags = triage_batch(batch, portfolio)
                for art, flag in zip(batch, flags):
                    if flag:
                        relevant.append(art)
                time.sleep(5)

            logger.info(f"  트리아지 통과: {len(relevant)}/{len(to_process)}건")

            # Step 2: 분류
            for i in range(0, len(relevant), self.BATCH_CLASSIFY):
                batch = relevant[i:i + self.BATCH_CLASSIFY]
                results = classify_batch(batch, portfolio)
                for art, res in zip(batch, results):
                    if not res:
                        continue
                    signal = ClassifiedSignal(
                        portfolio_id=portfolio.id,
                        portfolio_name=portfolio.name,
                        url=art.url,
                        title=art.title,
                        summary=art.summary,
                        source=art.source,
                        source_tier=art.source_tier,
                        published_at=art.published_at.isoformat(),
                        sentiment=res.get("sentiment", "Neutral"),
                        signal_type=res.get("signal_type", "기타"),
                        action_flag=res.get("action_flag", "white"),
                        relevance=res.get("relevance", "Low"),
                        summary_ko=res.get("summary_ko", ""),
                        summary_en=res.get("summary_en", ""),
                        classified_at=datetime.now(timezone.utc).isoformat(),
                        model_used=GEMINI_MODEL,
                        content_hash=art.content_hash,
                    )
                    signals.append(signal)
                    self.cache.set(art.content_hash, asdict(signal))
                time.sleep(6)

        flag_counts = {"red": 0, "yellow": 0, "white": 0}
        for s in signals:
            flag_counts[s.action_flag] = flag_counts.get(s.action_flag, 0) + 1
        logger.info(
            f"[분류 완료] 총 {len(signals)}건 | "
            f"🔴{flag_counts['red']} 🟡{flag_counts['yellow']} ⚪{flag_counts['white']} | "
            f"비용: $0.00 (Gemini 무료)"
        )
        return signals


# =============================================================================
# 단독 실행 테스트
# =============================================================================
if __name__ == "__main__":
    from collector import Collector
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    articles = Collector().run()
    signals  = Classifier().run(articles)
    print(f"\n분류 결과:")
    for s in signals[:5]:
        print(f"  {s.flag_emoji} [{s.signal_type}] {s.portfolio_name}")
        print(f"     {s.summary_ko}")