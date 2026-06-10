"""
collector.py — 데이터 수집 에이전트
=====================================
★ Claude API 비용 절감 설계
  - LLM 호출 전 규칙 기반 1차 필터로 수집량의 ~70% 제거
  - URL·제목 해시 중복 제거로 재처리 방지
  - 광고·PR·짧은 기사 자동 폐기
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import time
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import quote_plus

import feedparser          # pip install feedparser
import requests            # pip install requests
import yaml                # pip install pyyaml

logger = logging.getLogger(__name__)


# =============================================================================
# 데이터 모델
# =============================================================================

@dataclass
class RawArticle:
    """수집 원문 기사. 분류 전 상태."""
    url: str
    title: str
    summary: str
    content: str
    source: str            # google_news | dart | linkedin | crunchbase | twitter_x | sec
    source_tier: int       # 1=공식, 2=미디어, 3=시그널, 4=소셜
    published_at: datetime
    portfolio_id: str      # 어느 포트폴리오사 키워드로 수집됐는지
    lang: str = "ko"       # ko | en
    content_hash: str = field(init=False)

    def __post_init__(self):
        raw = (self.url + self.title).encode()
        self.content_hash = hashlib.sha256(raw).hexdigest()[:16]


@dataclass
class Portfolio:
    """portfolio.yaml 단일 포트폴리오사."""
    id: str
    name: str
    name_en: str
    sector: str
    stage: str
    country: str
    priority: str
    alert_language: str
    keywords_primary: list[str]
    keywords_secondary: list[str]
    keywords_exclude: list[str]
    sources: dict[str, bool]
    context_memo: str
    our_stake_pct: float
    board_seat: bool
    active: bool


# =============================================================================
# 설정 로더
# =============================================================================

def load_portfolios(path: str = "portfolio.yaml") -> list[Portfolio]:
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)

    defaults = data.get("defaults", {})
    portfolios = []
    for item in data.get("portfolios", []):
        if not item.get("active", True):
            logger.info(f"[SKIP] {item['id']} — active: false")
            continue

        src = {**defaults.get("sources", {}), **item.get("sources", {})}
        kw  = item.get("keywords", {})
        portfolios.append(Portfolio(
            id=item["id"],
            name=item["name"],
            name_en=item.get("name_en", item["name"]),
            sector=item.get("sector", ""),
            stage=item.get("stage", ""),
            country=item.get("country", "KR"),
            priority=item.get("priority", defaults.get("priority", "medium")),
            alert_language=item.get("alert_language", defaults.get("alert_language", "ko")),
            keywords_primary=kw.get("primary", []),
            keywords_secondary=kw.get("secondary", []),
            keywords_exclude=kw.get("exclude", []),
            sources=src,
            context_memo=item.get("context_memo", ""),
            our_stake_pct=item.get("our_stake_pct", 0.0),
            board_seat=item.get("board_seat", False),
            active=item.get("active", True),
        ))
    logger.info(f"포트폴리오 {len(portfolios)}개 로드 완료")
    return portfolios


def load_config(path: str = "config.yaml") -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


# =============================================================================
# 1차 필터 (규칙 기반) — LLM 호출 없음
# ★ 여기서 걸러내면 Haiku 호출도 발생하지 않음
# =============================================================================

class PreFilter:
    def __init__(self, config: dict):
        pf = config.get("pre_filter", {})
        self.min_relevance_score = pf.get("min_relevance_score", 0.5)
        self.min_content_length  = pf.get("min_content_length", 100)
        self.ad_patterns = [
            re.compile(p, re.IGNORECASE)
            for p in pf.get("ad_patterns", [])
        ]
        dedup_cfg = pf.get("dedup", {})
        self.dedup_enabled = dedup_cfg.get("enabled", True)
        self.dedup_window_hours = dedup_cfg.get("window_hours", 24)
        self._seen: set[str] = set()   # 인메모리 (실운영은 DB/Redis 사용)

    def relevance_score(self, article: RawArticle, portfolio: Portfolio) -> float:
        text = f"{article.title} {article.summary}".lower()

        # 제외 키워드 포함 → 즉시 0점
        for exc in portfolio.keywords_exclude:
            if exc.lower() in text:
                return 0.0

        score = 0.0
        for kw in portfolio.keywords_primary:
            if kw.lower() in text:
                score = max(score, 1.0)
        for kw in portfolio.keywords_secondary:
            if kw.lower() in text:
                score = max(score, 0.5)
        return score

    def is_ad(self, article: RawArticle) -> bool:
        for pat in self.ad_patterns:
            if pat.search(article.title):
                return True
        return False

    def is_duplicate(self, article: RawArticle) -> bool:
        if not self.dedup_enabled:
            return False
        if article.content_hash in self._seen:
            return True
        self._seen.add(article.content_hash)
        return False

    def passes(self, article: RawArticle, portfolio: Portfolio) -> bool:
        if len(article.content) < self.min_content_length:
            logger.debug(f"[FILTER-SHORT] {article.url}")
            return False
        if self.is_ad(article):
            logger.debug(f"[FILTER-AD] {article.url}")
            return False
        if self.is_duplicate(article):
            logger.debug(f"[FILTER-DEDUP] {article.url}")
            return False
        score = self.relevance_score(article, portfolio)
        if score < self.min_relevance_score:
            logger.debug(f"[FILTER-RELEVANCE:{score:.1f}] {article.url}")
            return False
        return True


# =============================================================================
# 수집 모듈 (소스별)
# =============================================================================

class GoogleNewsCollector:
    """
    Google News RSS 피드 기반 수집.
    API 키 불필요. 무료. 소스 계층 2 (미디어).
    """
    TIER = 2

    def collect(self, portfolio: Portfolio) -> list[RawArticle]:
        articles = []
        is_overseas = portfolio.country not in ("KR",)

        if is_overseas:
            # ── 해외사: 영문 Google News (US 기준)
            queries_en = portfolio.keywords_primary + [portfolio.name_en]
            for idx_q, query in enumerate(queries_en):
                if idx_q > 0:
                    time.sleep(0.5)  # Google News 과부하 방지
                url = (
                    f"https://news.google.com/rss/search?q={quote_plus(query)}"
                    f"&hl=en&gl=US&ceid=US:en"
                )
                try:
                    feed = feedparser.parse(url)
                    for entry in feed.entries[:10]:
                        articles.append(RawArticle(
                            url=entry.get("link", ""),
                            title=entry.get("title", ""),
                            summary=entry.get("summary", ""),
                            content=entry.get("summary", ""),
                            source="google_news_en",
                            source_tier=self.TIER,
                            published_at=self._parse_date(entry.get("published", "")),
                            portfolio_id=portfolio.id,
                            lang="en",
                        ))
                except Exception as e:
                    logger.warning(f"[GoogleNews-EN] {query}: {e}")
        else:
            # ── 국내사: 한국어 Google News
            queries_ko = portfolio.keywords_primary
            for idx_q, query in enumerate(queries_ko):
                if idx_q > 0:
                    time.sleep(0.5)  # Google News 과부하 방지
                url = (
                    f"https://news.google.com/rss/search?q={quote_plus(query)}"
                    f"&hl=ko&gl=KR&ceid=KR:ko"
                )
                try:
                    feed = feedparser.parse(url)
                    for entry in feed.entries[:10]:
                        articles.append(RawArticle(
                            url=entry.get("link", ""),
                            title=entry.get("title", ""),
                            summary=entry.get("summary", ""),
                            content=entry.get("summary", ""),
                            source="google_news",
                            source_tier=self.TIER,
                            published_at=self._parse_date(entry.get("published", "")),
                            portfolio_id=portfolio.id,
                            lang="ko",
                        ))
                except Exception as e:
                    logger.warning(f"[GoogleNews-KO] {query}: {e}")

            # 국내사도 영문 name_en 쿼리로 보완 (해외 미디어 커버리지)
            if portfolio.name_en and portfolio.name_en not in queries_ko:
                url = (
                    f"https://news.google.com/rss/search?q={quote_plus(portfolio.name_en)}"
                    f"&hl=en&gl=US&ceid=US:en"
                )
                try:
                    feed = feedparser.parse(url)
                    for entry in feed.entries[:5]:
                        articles.append(RawArticle(
                            url=entry.get("link", ""),
                            title=entry.get("title", ""),
                            summary=entry.get("summary", ""),
                            content=entry.get("summary", ""),
                            source="google_news_en",
                            source_tier=self.TIER,
                            published_at=self._parse_date(entry.get("published", "")),
                            portfolio_id=portfolio.id,
                            lang="en",
                        ))
                except Exception as e:
                    logger.warning(f"[GoogleNews-EN fallback] {portfolio.name_en}: {e}")

        return articles

    @staticmethod
    def _parse_date(s: str) -> datetime:
        try:
            import email.utils
            return datetime(*email.utils.parsedate(s)[:6], tzinfo=timezone.utc)
        except Exception:
            return datetime.now(timezone.utc)


class NaverNewsCollector:
    """
    네이버 공식 검색 API 기반 수집 (RSS → API 교체).
    국내사(KR) 전용. NAVER_CLIENT_ID / NAVER_CLIENT_SECRET 환경변수 필요.
    GitHub Actions 서버 IP 차단 문제 해결. 소스 계층 2.
    """
    TIER = 2
    API_URL = "https://openapi.naver.com/v1/search/news.json"

    def collect(self, portfolio: Portfolio) -> list[RawArticle]:
        if portfolio.country != "KR":
            return []

        client_id     = os.environ.get("NAVER_CLIENT_ID", "")
        client_secret = os.environ.get("NAVER_CLIENT_SECRET", "")
        if not client_id or not client_secret:
            msg = "[NaverNews] NAVER_CLIENT_ID/SECRET 환경변수 없음 — 스킵"
            logger.warning(msg)
            print(msg, flush=True)
            return []

        print(f"[NaverNews] API 호출 시작 (client_id={client_id[:4]}****)", flush=True)
        headers = {
            "X-Naver-Client-Id":     client_id,
            "X-Naver-Client-Secret": client_secret,
        }

        articles = []
        queries = portfolio.keywords_primary
        for idx_q, query in enumerate(queries):
            if idx_q > 0:
                time.sleep(0.3)
            params = {"query": query, "display": 10, "sort": "date"}
            try:
                resp = requests.get(self.API_URL, headers=headers, params=params, timeout=10)
                if resp.status_code != 200:
                    err_msg = f"[NaverNews] HTTP {resp.status_code} ({query}): {resp.text[:200]}"
                    logger.warning(err_msg)
                    print(err_msg, flush=True)
                    continue
                resp.raise_for_status()
                items = resp.json().get("items", [])
                print(f"[NaverNews] {query}: {len(items)}건 수집", flush=True)
                for item in items:
                    title   = re.sub(r"<[^>]+>", "", item.get("title", "")).strip()
                    summary = re.sub(r"<[^>]+>", "", item.get("description", "")).strip()
                    articles.append(RawArticle(
                        url=item.get("originallink") or item.get("link", ""),
                        title=title,
                        summary=summary,
                        content=summary,
                        source="naver_news",
                        source_tier=self.TIER,
                        published_at=self._parse_date(item.get("pubDate", "")),
                        portfolio_id=portfolio.id,
                        lang="ko",
                    ))
            except Exception as e:
                err_msg = f"[NaverNews] {query} 예외: {type(e).__name__}: {e}"
                logger.warning(err_msg)
                print(err_msg, flush=True)
        return articles

    @staticmethod
    def _parse_date(s: str) -> datetime:
        try:
            import email.utils
            return datetime(*email.utils.parsedate(s)[:6], tzinfo=timezone.utc)
        except Exception:
            return datetime.now(timezone.utc)


class TechCrunchCollector:
    """
    TechCrunch RSS 피드 — 해외 스타트업 특화.
    API 키 불필요. 무료. 소스 계층 2.
    해외사(country != KR) 대상으로 영문 키워드 매칭.
    """
    TIER = 2
    FEED_URL = "https://techcrunch.com/feed/"

    def collect(self, portfolio: Portfolio) -> list[RawArticle]:
        if portfolio.country == "KR":
            return []   # 국내사는 스킵

        articles = []
        keywords = [k.lower() for k in portfolio.keywords_primary + [portfolio.name_en]]
        try:
            feed = feedparser.parse(self.FEED_URL)
            for entry in feed.entries[:50]:
                title   = entry.get("title", "")
                summary = entry.get("summary", "")
                text    = (title + " " + summary).lower()
                if any(kw in text for kw in keywords):
                    articles.append(RawArticle(
                        url=entry.get("link", ""),
                        title=title,
                        summary=summary,
                        content=summary,
                        source="techcrunch",
                        source_tier=self.TIER,
                        published_at=GoogleNewsCollector._parse_date(
                            entry.get("published", "")),
                        portfolio_id=portfolio.id,
                        lang="en",
                    ))
        except Exception as e:
            logger.warning(f"[TechCrunch] {portfolio.name_en}: {e}")
        return articles

class DartCollector:
    """
    금감원 DART Open API — 공시 수집.
    소스 계층 1 (공식). API 키 필요.
    https://opendart.fss.or.kr/
    """
    TIER = 1
    BASE = "https://opendart.fss.or.kr/api"

    def __init__(self, api_key: str):
        self.api_key = api_key

    def collect(self, portfolio: Portfolio) -> list[RawArticle]:
        if not portfolio.sources.get("dart"):
            return []
        if portfolio.country != "KR":
            return []

        corp_code = self._resolve_corp_code(portfolio.name)
        if not corp_code:
            logger.warning(f"[DART] {portfolio.name} 기업코드 없음")
            return []

        articles = []
        try:
            resp = requests.get(
                f"{self.BASE}/list.json",
                params={"crtfc_key": self.api_key, "corp_code": corp_code,
                        "bgn_de": self._today_yyyymmdd(), "page_count": 20},
                timeout=10,
            )
            data = resp.json()
            for item in data.get("list", []):
                title = item.get("report_nm", "")
                rcp_no = item.get("rcp_no", "")
                url = f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcp_no}"
                articles.append(RawArticle(
                    url=url,
                    title=f"[DART] {title}",
                    summary=title,
                    content=title,
                    source="dart",
                    source_tier=self.TIER,
                    published_at=datetime.now(timezone.utc),
                    portfolio_id=portfolio.id,
                    lang="ko",
                ))
        except Exception as e:
            logger.warning(f"[DART] {portfolio.name}: {e}")
        return articles

    def _resolve_corp_code(self, name: str) -> Optional[str]:
        # 실운영: 기업명→DART 기업코드 매핑 DB 구축 필요
        # 여기서는 placeholder 반환
        MOCK_MAP = {"Company A": "00000001", "Company B": "00000002"}
        return MOCK_MAP.get(name)

    @staticmethod
    def _today_yyyymmdd() -> str:
        return datetime.now().strftime("%Y%m%d")


class CrunchbaseCollector:
    """
    Crunchbase API — 펀딩·M&A 시그널 수집.
    소스 계층 3 (시그널). API 키 필요.
    """
    TIER = 3
    BASE = "https://api.crunchbase.com/api/v4"

    def __init__(self, api_key: str):
        self.api_key = api_key

    def collect(self, portfolio: Portfolio) -> list[RawArticle]:
        if not portfolio.sources.get("crunchbase"):
            return []

        articles = []
        try:
            resp = requests.get(
                f"{self.BASE}/searches/funding_rounds",
                params={"user_key": self.api_key},
                json={"query": [{"type": "predicate", "field_id": "org_name",
                                 "operator_id": "contains",
                                 "values": [portfolio.name_en]}],
                      "field_ids": ["org_name", "announced_on", "investment_type",
                                    "money_raised", "short_description"],
                      "limit": 5},
                timeout=15,
            )
            for entity in resp.json().get("entities", []):
                p = entity.get("properties", {})
                title = (f"[Crunchbase] {p.get('org_name','')} "
                         f"{p.get('investment_type','')} 펀딩 발표")
                articles.append(RawArticle(
                    url=f"https://www.crunchbase.com/search/funding_rounds",
                    title=title,
                    summary=p.get("short_description", ""),
                    content=p.get("short_description", title),
                    source="crunchbase",
                    source_tier=self.TIER,
                    published_at=datetime.now(timezone.utc),
                    portfolio_id=portfolio.id,
                    lang="en",
                ))
        except Exception as e:
            logger.warning(f"[Crunchbase] {portfolio.name}: {e}")
        return articles


class TwitterXCollector:
    """
    X(트위터) 언급량 급증 감지.
    소스 계층 4 (소셜). Bearer Token 필요.
    ★ 비용 절감: 언급 '건수'만 집계 → 급증 시에만 기사 생성 (불필요한 LLM 호출 차단)
    """
    TIER = 4
    BASE = "https://api.twitter.com/2"
    SPIKE_THRESHOLD = 50   # 2시간 내 언급 50건 이상 시 시그널 생성

    def __init__(self, bearer_token: str):
        self.headers = {"Authorization": f"Bearer {bearer_token}"}

    def collect(self, portfolio: Portfolio) -> list[RawArticle]:
        if not portfolio.sources.get("twitter_x"):
            return []

        articles = []
        query = " OR ".join(
            f'"{kw}"' for kw in portfolio.keywords_primary[:3]
        )
        try:
            resp = requests.get(
                f"{self.BASE}/tweets/counts/recent",
                headers=self.headers,
                params={"query": query, "granularity": "hour"},
                timeout=10,
            )
            data = resp.json()
            total = sum(b.get("tweet_count", 0) for b in data.get("data", [])[-2:])
            if total >= self.SPIKE_THRESHOLD:
                articles.append(RawArticle(
                    url=f"https://twitter.com/search?q={quote_plus(query)}",
                    title=f"[X 언급 급증] {portfolio.name} — 최근 2시간 {total}건",
                    summary=f"{portfolio.name} 관련 X 언급이 {total}건으로 급증.",
                    content=f"{portfolio.name} 관련 X 언급이 {total}건으로 급증. 관련 트윗 모니터링 필요.",
                    source="twitter_x",
                    source_tier=self.TIER,
                    published_at=datetime.now(timezone.utc),
                    portfolio_id=portfolio.id,
                    lang="ko",
                ))
        except Exception as e:
            logger.warning(f"[TwitterX] {portfolio.name}: {e}")
        return articles


# =============================================================================
# 수집 오케스트레이터
# =============================================================================

class Collector:
    def __init__(self, portfolio_path: str = "portfolio.yaml",
                 config_path: str = "config.yaml"):
        self.portfolios = load_portfolios(portfolio_path)
        self.config     = load_config(config_path)
        self.pre_filter = PreFilter(self.config)

        import os
        self.collectors = {
            "google_news": GoogleNewsCollector(),
            "naver_news":  NaverNewsCollector(),
            "dart":        DartCollector(os.getenv("DART_API_KEY", "")),
            "crunchbase":  CrunchbaseCollector(os.getenv("CRUNCHBASE_API_KEY", "")),
            "twitter_x":   TwitterXCollector(os.getenv("TWITTER_BEARER_TOKEN", "")),
            "techcrunch":  TechCrunchCollector(),
        }

    def _collect_one(self, portfolio) -> tuple[list[RawArticle], list[RawArticle]]:
        """단일 포트폴리오사 수집 (병렬 실행 단위)."""
        raw: list[RawArticle] = []
        for source_name, collector in self.collectors.items():
            if portfolio.sources.get(source_name):
                try:
                    fetched = collector.collect(portfolio)
                    raw.extend(fetched)
                    print(f"  [{portfolio.name}] {source_name}: {len(fetched)}건", flush=True)
                except Exception as e:
                    logger.warning(f"  [{portfolio.name}] {source_name} 수집 오류: {e}")
        passed = [a for a in raw if self.pre_filter.passes(a, portfolio)]
        print(f"[{portfolio.name}] {len(passed)}/{len(raw)}건 통과", flush=True)
        return raw, passed

    def run(self, max_workers: int = 5) -> list[RawArticle]:
        """전체 포트폴리오사 병렬 수집 → 1차 필터 적용 후 반환.

        max_workers=5: 5개사 동시 수집 → 순차 대비 3~5배 빠름
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed

        all_raw: list[RawArticle]    = []
        all_passed: list[RawArticle] = []

        print(f"[수집 시작] {len(self.portfolios)}개사 병렬 수집 (workers={max_workers})", flush=True)

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(self._collect_one, p): p
                for p in self.portfolios
            }
            for future in as_completed(futures):
                p = futures[future]
                try:
                    raw, passed = future.result()
                    all_raw.extend(raw)
                    all_passed.extend(passed)
                except Exception as e:
                    logger.error(f"[{p.name}] 수집 실패: {e}")

        total = len(all_raw)
        logger.info(
            f"[수집 완료] 전체 {total}건 수집 → "
            f"{len(all_passed)}건 LLM 분류 대상 "
            f"({len(all_passed)/total*100:.0f}% 통과)"
            if total else "[수집 완료] 수집된 기사 없음"
        )
        return all_passed


# =============================================================================
# 실행 진입점 (단독 테스트용)
# =============================================================================
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    collector = Collector()
