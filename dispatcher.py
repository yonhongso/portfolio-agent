"""
dispatcher.py — 배포 에이전트 v2
================================
텔레그램 즉시 알림(🔴) + 이메일 Daily / Weekly / Monthly 리포트 발송.

이메일 구성:
  본문   → 모바일 친화적 HTML 대시보드 (한국어 + 영어 듀얼)
  첨부 1 → 경영층 보고용 PDF (reportlab 사용)
  첨부 2 → ⚪ 참고 항목 CSV (Daily 한정)

설치 필요: pip install reportlab requests pyyaml python-dotenv
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import smtplib
import textwrap
from collections import defaultdict
from datetime import datetime, timezone
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

import requests
import yaml
from dotenv import load_dotenv

load_dotenv()  # .env 파일 로드 → 환경변수 주입

from classifier_groq import ClassifiedSignal
from collector import load_config

logger = logging.getLogger(__name__)


# =============================================================================
# 중복 제거 (Jaccard 유사도 기반)
# =============================================================================

def deduplicate_signals(signals: list[ClassifiedSignal],
                        threshold: float = 0.25,
                        max_per_group: int = 2) -> list[ClassifiedSignal]:
    """
    중복 제거 전략 2단계:
    1) 문자 trigram Jaccard 유사도 0.25 이상이면 중복으로 판정
    2) (portfolio_id, signal_type) 그룹당 최대 max_per_group 건만 유지
       → AI 요약이 달라도 같은 회사·같은 신호유형 기사는 최대 2건으로 제한
    우선순위: red > yellow > white, source_tier 낮은 것 먼저 유지
    """
    import re

    def trigrams(text: str) -> set:
        t = re.sub(r'\s+', '', (text or '').lower())
        return {t[i:i+3] for i in range(len(t) - 2)} if len(t) >= 3 else set(t)

    def jaccard(a: str, b: str) -> float:
        sa, sb = trigrams(a), trigrams(b)
        if not (sa or sb):
            return 0.0
        return len(sa & sb) / len(sa | sb)

    def is_duplicate(c: ClassifiedSignal, k: ClassifiedSignal) -> bool:
        """제목 또는 요약 trigram 유사도가 임계값 초과이면 중복."""
        return (jaccard(c.title, k.title) > threshold or
                jaccard(c.summary_ko, k.summary_ko) > threshold)

    flag_priority = {"red": 0, "yellow": 1, "white": 2}

    # (portfolio_id, signal_type) 단위로 그룹화
    groups: dict[tuple, list[ClassifiedSignal]] = defaultdict(list)
    for s in signals:
        groups[(s.portfolio_id, s.signal_type)].append(s)

    result = []
    for group in groups.values():
        # 중요도 순 정렬
        group = sorted(group,
                       key=lambda x: (flag_priority.get(x.action_flag, 2),
                                      x.source_tier))
        kept: list[ClassifiedSignal] = []
        for candidate in group:
            if len(kept) >= max_per_group:
                break                           # 그룹당 최대 2건 하드캡
            if not any(is_duplicate(candidate, k) for k in kept):
                kept.append(candidate)
        result.extend(kept)

    before = len(signals)
    after  = len(result)
    logger.info(f"[중복제거] {before}건 → {after}건 ({before - after}건 제거)")
    return result


# =============================================================================
# 텔레그램 발송
# =============================================================================

class TelegramSender:
    API = "https://api.telegram.org/bot{token}/sendMessage"

    def __init__(self, bot_token: str, chat_id: str):
        self.token   = bot_token
        self.chat_id = chat_id

    def send(self, text: str) -> bool:
        try:
            resp = requests.post(
                self.API.format(token=self.token),
                json={"chat_id": self.chat_id, "text": text,
                      "parse_mode": "HTML", "disable_web_page_preview": True},
                timeout=10,
            )
            resp.raise_for_status()
            logger.info("[Telegram] 발송 완료")
            return True
        except Exception as e:
            logger.error(f"[Telegram] 발송 실패: {e}")
            return False

    def send_signal(self, signal: ClassifiedSignal) -> bool:
        """🔴 시그널 즉시 알림 — 임팩트 중심 포맷 (한국어 + 영어)."""
        pub = signal.published_at[:16].replace("T", " ")
        sentiment_map = {
            "Positive": "📈 긍정", "Negative": "📉 부정",
            "Neutral": "➡️ 중립", "Mixed": "➡️ 중립",
        }
        sentiment_label = sentiment_map.get(signal.sentiment, signal.sentiment)
        relevance_star  = {"High": "★★★", "Medium": "★★☆", "Low": "★☆☆"}.get(
            signal.relevance, signal.relevance)

        # 한국어 요약만 표시
        summary_ko = (signal.summary_ko or "").strip()
        if summary_ko:
            summary_line = summary_ko
        else:
            summary_line = signal.title[:80]

        text = (
            f"🔴 <b>{signal.portfolio_name}</b> — 즉시검토 요망\n"
            f"<b>[{signal.signal_type}]</b> · {sentiment_label} · {relevance_star}\n"
            f"\n"
            f"{summary_line}\n"
            f"\n"
            f"<a href='{signal.url}'>📎 원문 보기</a>  ·  {pub[:10]} {pub[11:]}  ·  {signal.source}"
        )
        return self.send(text)


# =============================================================================
# 이메일 발송
# =============================================================================

class EmailSender:
    def __init__(self, cfg: dict):
        ep = cfg["dispatch"]["email_provider"]
        self.host      = os.path.expandvars(ep["host"])
        self.port      = ep["port"]
        self.user      = os.path.expandvars(ep["user"])
        self.password  = os.path.expandvars(ep["password"])
        self.from_addr = ep["from_address"]
        self.from_name = ep["from_name"]

    def send(self, to: list[str], subject: str, html_body: str,
             attachments: Optional[list[tuple[str, bytes, str]]] = None) -> bool:
        """
        attachments: [(filename, data_bytes, mime_type), ...]
        mime_type 예: "application/pdf", "text/csv"
        """
        msg = MIMEMultipart("mixed")
        msg["Subject"] = subject
        msg["From"]    = f"{self.from_name} <{self.from_addr}>"
        msg["To"]      = ", ".join(to)

        alt = MIMEMultipart("alternative")
        alt.attach(MIMEText(html_body, "html", "utf-8"))
        msg.attach(alt)

        if attachments:
            for filename, data, mime_type in attachments:
                main_type, sub_type = mime_type.split("/", 1)
                part = MIMEBase(main_type, sub_type)
                part.set_payload(data)
                encoders.encode_base64(part)
                part.add_header("Content-Disposition",
                                f'attachment; filename="{filename}"')
                msg.attach(part)
        try:
            with smtplib.SMTP(self.host, self.port) as smtp:
                smtp.starttls()
                smtp.login(self.user, self.password)
                smtp.send_message(msg)
            logger.info(f"[Email] 발송 완료 → {to}")
            return True
        except Exception as e:
            logger.error(f"[Email] 발송 실패: {e}")
            return False


# =============================================================================
# HTML 이메일 대시보드 (모바일 친화적, 듀얼 언어)
# =============================================================================

_HTML_STYLE = """
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: 'Malgun Gothic', 'Segoe UI', -apple-system, BlinkMacSystemFont, Arial, sans-serif;
         background: #edf0f4; color: #1a1a2e; -webkit-font-smoothing: antialiased; }
  .wrapper { max-width: 640px; margin: 0 auto; padding: 20px 16px; }

  /* ── 헤더 */
  .header {
    background: linear-gradient(150deg, #0d1b2a 0%, #1a2744 55%, #16213e 100%);
    border-radius: 12px 12px 0 0;
    padding: 28px 28px 22px;
    position: relative; overflow: hidden;
  }
  .header::after {
    content: ''; position: absolute; top: -40px; right: -30px;
    width: 180px; height: 180px; border-radius: 50%;
    background: rgba(232,213,183,0.04); pointer-events: none;
  }
  .header-badge {
    display: inline-block;
    background: rgba(232,213,183,0.12);
    color: #e8d5b7; font-size: 9px; font-weight: 700;
    letter-spacing: 2px; text-transform: uppercase;
    padding: 3px 10px; border-radius: 20px; margin-bottom: 12px;
    border: 1px solid rgba(232,213,183,0.25);
  }
  .header h1 {
    color: #ffffff; font-size: 20px; font-weight: 700;
    letter-spacing: -0.4px; margin-bottom: 6px; line-height: 1.2;
  }
  .header .sub {
    color: rgba(255,255,255,0.45); font-size: 12px; letter-spacing: 0.2px;
  }
  .header-strip {
    background: rgba(0,0,0,0.25);
    border-radius: 0 0 12px 12px;
    padding: 9px 28px;
    display: flex; gap: 20px; align-items: center;
    border-top: 1px solid rgba(255,255,255,0.07);
  }
  .strip-item { font-size: 10px; color: rgba(255,255,255,0.4);
                letter-spacing: 0.3px; text-transform: uppercase; }
  .strip-item.hi { color: #e8d5b7; font-weight: 600; }
  .strip-sep { width: 1px; height: 12px; background: rgba(255,255,255,0.1); }

  /* ── 지표 카드 */
  .metrics { display: flex; gap: 10px; margin: 14px 0 16px; }
  .metric-card {
    flex: 1; border-radius: 10px; padding: 16px 10px 14px;
    text-align: center; background: #fff;
    box-shadow: 0 2px 8px rgba(0,0,0,.06);
    border-top: 3px solid #dee2e6;
  }
  .metric-card.red    { border-top-color: #e74c3c; }
  .metric-card.yellow { border-top-color: #f39c12; }
  .metric-card.white  { border-top-color: #adb5bd; }
  .metric-card .num  { font-size: 34px; font-weight: 800; line-height: 1; color: #343a40; }
  .metric-card.red   .num { color: #c0392b; }
  .metric-card.yellow .num { color: #d68910; }
  .metric-card.white  .num { color: #868e96; }
  .metric-card .label { font-size: 10.5px; color: #adb5bd; margin-top: 5px;
                        line-height: 1.5; font-weight: 500; }

  /* ── 섹션 헤더 */
  .section-header {
    display: flex; align-items: center; gap: 8px;
    padding: 14px 0 9px; border-bottom: 1.5px solid #e9ecef; margin-bottom: 12px;
  }
  .section-header span { font-size: 12px; font-weight: 700; color: #495057;
                          letter-spacing: 0.5px; text-transform: uppercase; }

  /* ── L1: 플래그 배너 */
  .flag-banner {{
    display: flex; align-items: center; justify-content: space-between;
    padding: 13px 20px; border-radius: 10px; margin: 22px 0 16px; color: #fff;
  }}
  .flag-banner.red    {{ background: linear-gradient(135deg,#c0392b,#a93226); }}
  .flag-banner.yellow {{ background: linear-gradient(135deg,#d68910,#b7770d); }}
  .flag-banner.white  {{ background: linear-gradient(135deg,#868e96,#6c757d); }}
  .fb-label {{ font-size: 15px; font-weight: 900; letter-spacing: .2px; }}
  .fb-sub   {{ font-size: 10px; color: rgba(255,255,255,.65); margin-left: 8px; font-weight: 500; }}
  .fb-pill  {{ background: rgba(255,255,255,.2); border-radius: 20px; padding: 4px 14px;
               font-size: 13px; font-weight: 700; }}

  /* ── L2: 회사 헤딩 */
  .co-group   {{ margin-bottom: 20px; }}
  .co-heading {{
    display: flex; align-items: baseline; justify-content: space-between;
    padding: 6px 2px 8px; margin-bottom: 8px; border-bottom: 2px solid #e9ecef;
  }}
  .co-name {{ font-size: 17px; font-weight: 900; color: #1a1a2e; letter-spacing: -.4px; }}
  .co-cnt  {{ font-size: 11px; color: #adb5bd; font-weight: 500; }}

  /* ── L3: 기사 카드 */
  .art-card {{
    background: #fff; border-radius: 8px; margin-bottom: 8px;
    padding: 13px 16px; border: 1px solid #e9ecef;
    border-left: 3px solid #dee2e6;
    box-shadow: 0 1px 4px rgba(0,0,0,.04);
  }}
  .art-card.red    {{ border-left-color: #e74c3c; }}
  .art-card.yellow {{ border-left-color: #f39c12; }}
  .art-top {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 7px; }}
  .sig-badge {{ font-size: 10px; font-weight: 700; padding: 2px 10px;
                border-radius: 4px; letter-spacing: .2px; }}
  .art-src  {{ font-size: 11px; color: #adb5bd; text-decoration: none; }}
  .art-hl   {{ font-size: 14px; font-weight: 700; color: #1a1a2e;
               line-height: 1.45; margin-bottom: 10px; }}
  .art-acts {{ display: flex; flex-direction: column; gap: 5px; }}
  .act-impl {{ font-size: 11.5px; color: #444; padding: 6px 11px;
               background: #f8f9ff; border-radius: 5px; line-height: 1.4; }}
  .act-do   {{ font-size: 11.5px; color: #444; padding: 6px 11px;
               background: #fff8f0; border-radius: 5px; line-height: 1.4; }}

  /* ── 시그널 카드 (포털 스타일, 이메일용 유지) */
  .signal-card {
    background: #fff; border-radius: 10px; margin-bottom: 12px;
    box-shadow: 0 2px 8px rgba(0,0,0,.06); overflow: hidden;
    border: 1px solid #e9ecef;
  }
  .card-header {
    display: flex; justify-content: space-between; align-items: center;
    padding: 11px 16px; border-bottom: 1px solid #f1f3f5;
  }
  .signal-card.red    .card-header { background: #fdf5f5; }
  .signal-card.yellow .card-header { background: #fdfbf0; }
  .signal-card.white  .card-header { background: #f8f9fa; }
  .card-body { padding: 14px 16px 12px; }

  .company { font-size: 14px; font-weight: 700; color: #1a1a2e; }
  .badge {
    font-size: 10px; padding: 3px 10px; border-radius: 20px;
    font-weight: 700; letter-spacing: 0.5px; text-transform: uppercase;
  }
  .badge.red    { background: #fde8e8; color: #c0392b; border: 1px solid #f5c6c6; }
  .badge.yellow { background: #fef3cd; color: #856404; border: 1px solid #ffe69c; }
  .badge.white  { background: #f1f3f5; color: #868e96; border: 1px solid #dee2e6; }

  .summary-ko {
    font-size: 13.5px; color: #212529; line-height: 1.65;
    margin-bottom: 5px; font-weight: 500;
  }
  .summary-en {
    font-size: 12px; color: #868e96; font-style: italic; line-height: 1.5;
    margin-bottom: 10px; padding-bottom: 10px; border-bottom: 1px dashed #e9ecef;
  }
  .insight-grid { display: flex; gap: 8px; margin-top: 8px; }
  .insight-box {
    flex: 1; border-radius: 6px; padding: 9px 11px; font-size: 12px; line-height: 1.5;
  }
  .insight-box .tag {
    display: block; font-size: 9.5px; font-weight: 700; letter-spacing: 0.8px;
    text-transform: uppercase; margin-bottom: 3px;
  }
  .insight-box.impl { background: #f8f9fa; color: #495057; }
  .insight-box.impl .tag { color: #868e96; }
  .insight-box.action { background: #e8f4fd; color: #154360; }
  .insight-box.action .tag { color: #2980b9; }
  .card-footer {
    display: flex; justify-content: space-between; align-items: center;
    padding: 9px 16px; border-top: 1px solid #f1f3f5; background: #fafafa;
  }
  .signal-type-tag {
    font-size: 10px; background: #f1f3f5; padding: 3px 8px;
    border-radius: 4px; color: #868e96; font-weight: 500;
  }
  .link-btn { font-size: 11px; color: #3498db; text-decoration: none; font-weight: 600; }

  /* ── Executive Summary */
  .exec-box {
    border-radius: 10px; padding: 16px 18px; margin-bottom: 16px;
    border: 1px solid; position: relative; overflow: hidden;
  }
  .exec-box .e-label {
    font-size: 9.5px; font-weight: 700; letter-spacing: 1.5px;
    text-transform: uppercase; margin-bottom: 7px; display: block;
  }
  .exec-box .e-main { font-size: 14px; font-weight: 600; margin-bottom: 4px; }
  .exec-box .e-sub  { font-size: 12px; font-style: italic; }

  /* ── 참고 항목 안내 */
  .white-note {
    background: #f8f9fa; border-radius: 8px; padding: 13px 16px;
    font-size: 12px; color: #868e96; text-align: center;
    border: 1px dashed #dee2e6; margin-top: 4px;
  }

  /* ── 푸터 */
  .footer {
    text-align: center; padding: 22px 0 8px;
    font-size: 11px; color: #adb5bd; line-height: 1.8;
    border-top: 1px solid #e9ecef; margin-top: 20px;
  }
  .footer b { color: #6c757d; }
  .conf-tag {
    display: inline-block; background: #f1f3f5; color: #868e96;
    font-size: 9px; padding: 2px 8px; border-radius: 3px;
    letter-spacing: 1px; text-transform: uppercase;
    border: 1px solid #dee2e6; margin-bottom: 8px;
  }
</style>
"""

def _dedup_signals(sigs: list) -> list:
    """회사 내 유사 기사 중복 제거.
    1단계: signal_type 무관하게 제목 유사도(Jaccard 0.25) 로 중복 제거
           ※ 한국어 조사/어미 정규화 후 비교 (논란으로→논란 등)
    2단계: 같은 signal_type은 1건만 유지
    → 같은 사건을 다루는 기사는 가장 중요한 1건만 표시."""
    import re as _re2
    _flag_rank = {"red": 0, "yellow": 1, "white": 2}

    def _normalize_ko(w):
        """한국어 조사/어미 제거 — 논란으로→논란, 에서→(빈문자) 등."""
        for sfx in ['으로서', '으로부터', '에서의', '으로의', '이라는', '이라고',
                    '하는데', '이고', '이며', '이다', '라는', '하는', '된다',
                    '한다', '되어', '하여', '에는', '에도', '으로', '에서',
                    '에게', '이고', '이라']:
            if w.endswith(sfx) and len(w) > len(sfx) + 1:
                return w[:-len(sfx)]
        return w

    def _words(s):
        raw = (s.title or s.summary_ko or "")
        tokens = _re2.split(r'[\s\W]+', raw.lower())
        return set(_normalize_ko(w) for w in tokens if len(w) >= 2)

    # flag 중요도 순 정렬 (중요한 기사를 먼저 보존)
    sorted_sigs = sorted(sigs, key=lambda x: _flag_rank.get(x.action_flag, 2))

    # 1단계: 제목 유사도 dedup (타입 무관)
    step1 = []
    for s in sorted_sigs:
        w = _words(s)
        is_dup = False
        for k in step1:
            kw = _words(k)
            union = kw | w
            if union and len(kw & w) / len(union) >= 0.25:
                is_dup = True
                break
        if not is_dup:
            step1.append(s)

    # 2단계: 같은 signal_type은 1건만
    seen_types: set = set()
    result = []
    for s in step1:
        if s.signal_type not in seen_types:
            seen_types.add(s.signal_type)
            result.append(s)

    return result


def _company_section_html(company: str,
                          signals: list["ClassifiedSignal"]) -> str:
    """L2: 회사 헤딩 (한 번만) + L3 기사 카드 목록. 유사 기사 중복 제거 후 표시."""
    deduped = _dedup_signals(signals)
    cards_html = "\n".join(_signal_card_html(s) for s in deduped)
    return f"""
    <div class="co-group">
      <div class="co-heading">
        <span class="co-name">{company}</span>
        <span class="co-cnt">{len(deduped)}건</span>
      </div>
      {cards_html}
    </div>"""


# 시그널 유형별 컬러 (텍스트색, 배경색)
_SIGNAL_TYPE_COLORS: dict = {
    "M&A·Exit":         ("#6c3483", "#f3e5f5"),
    "펀딩·밸류에이션":   ("#1565c0", "#e3f2fd"),
    "경영진 변동":       ("#bf360c", "#fff3e0"),
    "파트너십·협업":     ("#00695c", "#e0f2f1"),
    "제품·기술 출시":    ("#2e7d32", "#e8f5e9"),
    "규제·법률 리스크":  ("#c62828", "#ffebee"),
    "재무·실적":         ("#283593", "#e8eaf6"),
    "평판·ESG":          ("#37474f", "#eceff1"),
    "기타":              ("#455a64", "#f5f5f5"),
}

def _signal_card_html(s: ClassifiedSignal) -> str:
    """L3: 기사 카드 — 회사명 없음 (L2 co-heading에서 표시), 헤드라인 중심."""
    flag   = s.action_flag
    url    = s.url if s.url else "#"
    impl   = _implication_template(s.signal_type)
    action = _action_template(s.signal_type, flag)
    tc, bg = _SIGNAL_TYPE_COLORS.get(s.signal_type, ("#455a64", "#f5f5f5"))

    # 언어 선택: KR(국내) → summary_ko, 해외 → summary_en
    summary_ko = (s.summary_ko or "").strip().rstrip(".…").strip()
    summary_en = (s.summary_en or "").strip().rstrip(".…").strip() if not (s.summary_ko or "").strip() else (s.summary_en or "").strip()
    # 요약이 없으면 제목 fallback — 출처가 잘라놓은 말줄임(...) 제거 후 완결 절단
    _title = (s.title or "").strip()
    while _title and _title[-1] in ".… ":
        _title = _title[:-1]
    headline = summary_ko or summary_en or _finish_summary(_title, 60)

    # 출처 단축
    src_raw = s.source or ""
    src_short = (src_raw.replace("www.", "").split(".")[0][:16] + " →") if src_raw else "원문 →"

    # 출처 표기 정리
    src_display = "Google News →"
    if src_raw and "google" not in src_raw.lower():
        src_display = (src_raw.replace("www.", "").split(".")[0].title()[:18] + " →")

    return f"""
    <div class="art-card {flag}">
      <div class="art-top">
        <span class="sig-badge" style="background:{bg};color:{tc}">{s.signal_type}</span>
        <a href="{url}" target="_blank" class="art-src">{src_display}</a>
      </div>
      <div class="art-hl">{headline}</div>
      <div class="art-acts">
        <div class="act-impl"><span style="font-size:10px;font-weight:700;background:#d1d8e0;color:#495057;border-radius:3px;padding:1px 8px;margin-right:7px;display:inline-block">시사점</span>{impl}</div>
        <div class="act-do"><span style="font-size:10px;font-weight:700;background:#e74c3c;color:#fff;border-radius:3px;padding:1px 8px;margin-right:7px;display:inline-block">권고액션</span>{action}</div>
      </div>
    </div>"""


def _implication_template(signal_type: str) -> str:
    return {
        "펀딩·밸류에이션": "밸류에이션 변동에 따른 지분가치 재평가 필요",
        "경영진 변동":     "경영 안정성 및 전략 방향 변화 모니터링 요망",
        "파트너십·협업":   "시너지 효과 및 사업 확장 가능성 평가 필요",
        "제품·기술 출시":  "시장 경쟁력 및 매출 영향 점검",
        "규제·법률 리스크":"법적 리스크 노출 수준 및 재무 영향 검토 요망",
        "재무·실적":       "실적 트렌드 분석 및 투자 회수 시점 재검토",
        "M&A·Exit":        "Exit 기회 또는 희석 위험 즉각 검토 요망",
        "평판·ESG":        "브랜드 가치 영향 및 ESG 리스크 평가",
        "기타":            "추가 정보 수집 후 판단 필요",
    }.get(signal_type, "추가 모니터링 필요")

def _action_template(signal_type: str, flag: str) -> str:
    if flag == "red":
        return {
            "M&A·Exit":        "오늘 중 법률·회계 자문 연락 / 이사회 보고 준비",
            "규제·법률 리스크": "법무팀 즉시 공유 / 대응 시나리오 작성",
            "경영진 변동":     "대표이사 직접 컨택 / 경위 파악",
            "펀딩·밸류에이션": "지분가치 영향 계산 후 투자위원회 보고",
        }.get(signal_type, "담당 심사역 즉시 검토 후 팀장 보고")
    elif flag == "yellow":
        return {
            "M&A·Exit":        "이번 주 내 IR 미팅 요청 / 동향 파악",
            "파트너십·협업":   "시너지 영향 분석 후 주간 리포트 반영",
            "재무·실적":       "다음 분기 실적 발표 일정 확인",
        }.get(signal_type, "주간 모니터링 리스트에 추가")
    return "참고 유지 / 반복 출현 시 상위 플래그 검토"


def _exec_text(s) -> str:
    """Executive Summary 표시용 — 요약 전문 사용(중간 절단 금지), 없으면 제목 폴백."""
    return (getattr(s, "summary_ko", "") or getattr(s, "summary_en", "")
            or getattr(s, "title", "") or "").strip()


def _exec_text_en(s) -> str:
    """영문 Executive Summary 한 줄 — 전문 사용, 없으면 한글 요약/제목 폴백."""
    return (getattr(s, "summary_en", "") or getattr(s, "summary_ko", "")
            or getattr(s, "title", "") or "").strip()


def build_daily_html(signals: list[ClassifiedSignal], date_str: str,
                     drafts_data: Optional[list[dict]] = None) -> str:
    """Daily 이메일 — 전문 포털 스타일.
    Section 1: Executive Summary + 포트폴리오 현황 (시그널 카드)
    Section 2: 커뮤니케이션 초안 (🔴 항목 있을 때만, 완전 전개 — 이메일 JS 미지원)"""
    reds    = [s for s in signals if s.action_flag == "red"]
    yellows = [s for s in signals if s.action_flag == "yellow"]
    whites  = [s for s in signals if s.action_flag == "white"]
    total     = len(signals)
    companies = len(set(s.portfolio_name for s in signals))

    # ── Executive Summary 설정 (줄바꿈+마크 형식)
    if reds:
        red_lines    = [f"🔴 {s.portfolio_name} — {_exec_text(s)}" for s in reds[:3]]
        yellow_lines = [f"🟡 {s.portfolio_name} — {_exec_text(s)}" for s in yellows[:2]]
        exec_msg  = "\n".join(red_lines + yellow_lines)
        exec_en   = "\n".join([f"· {s.portfolio_name}: {_exec_text_en(s)}" for s in reds[:3]])
        exec_bg   = "#fdf5f5"; exec_border = "#e74c3c"; exec_label_color = "#c0392b"
    elif yellows:
        yellow_lines = [f"🟡 {s.portfolio_name} — {_exec_text(s)}" for s in yellows[:4]]
        exec_msg  = "\n".join(yellow_lines)
        exec_en   = "\n".join([f"· {s.portfolio_name}: watch & report" for s in yellows[:4]])
        exec_bg   = "#fdfbf0"; exec_border = "#f39c12"; exec_label_color = "#d68910"
    else:
        exec_msg  = "⚪ 오늘 포트폴리오 전반 특이사항 없음 — 정기모니터링 유지"
        exec_en   = "· No significant signals today. Routine monitoring continues."
        exec_bg   = "#f0fdf4"; exec_border = "#27ae60"; exec_label_color = "#27ae60"

    # ── 시그널 카드 — 회사별 묶음 렌더링
    from collections import defaultdict as _dd
    visible = [s for s in signals if s.action_flag in ("red", "yellow", "white")]
    by_co: dict[str, list] = _dd(list)
    for s in visible:
        by_co[s.portfolio_name].append(s)
    # 정렬: ① action_flag 심각도 (Red→Yellow) ② 동일 카테고리 내 display_priority
    _DISPLAY_PRIORITY = {"업스테이지": 1, "비엠스마일": 2, "컬리": 3, "에버온": 4}
    def _co_sort_key(item):
        co, co_sigs = item
        flags = {s.action_flag for s in co_sigs}
        flag_score = 0 if "red" in flags else 1
        return (flag_score, _DISPLAY_PRIORITY.get(co, 99))
    cards = "\n".join(
        _company_section_html(co,
            sorted(sigs, key=lambda x: 0 if x.action_flag=="red" else 1))
        for co, sigs in sorted(by_co.items(), key=_co_sort_key)
    )
    if not cards:
        cards = '<div style="text-align:center;padding:24px;color:#adb5bd;font-size:13px">오늘 검토 항목 없음 · No items today</div>'

    white_note = (
        f'<div class="white-note">⚪ 정기모니터링 {len(whites)}건</div>'
    ) if whites else ""

    # ── 커뮤니케이션 초안 섹션 (이메일 섹션 2 — JS 없이 완전 전개)
    drafts_section = ""
    if drafts_data:
        draft_cards_html = ""
        for d in drafts_data:
            co       = d.get("portfolio_name", "")
            sig      = d.get("signal_type", "")
            summ     = (d.get("summary_ko") or "")
            me_html  = (d.get("msg_exec") or "").replace("\n", "<br>")
            mp_html  = (d.get("msg_portfolio") or "").replace("\n", "<br>")
            tc, bg   = _SIGNAL_TYPE_COLORS.get(sig, ("#455a64", "#f5f5f5"))
            draft_cards_html += f"""
<div style="background:#fff;border-radius:12px;margin-bottom:16px;overflow:hidden;
            box-shadow:0 2px 8px rgba(0,0,0,.07);border:1px solid #e9ecef">
  <!-- 회사 헤더 -->
  <div style="background:#f8f9fa;padding:13px 20px;border-bottom:1px solid #e9ecef;
              display:flex;align-items:center;gap:10px">
    <div style="width:4px;height:32px;background:#c0392b;border-radius:3px;flex-shrink:0"></div>
    <div>
      <div style="font-size:15px;font-weight:800;color:#1a1a2e">{co}</div>
      <span style="display:inline-block;background:{bg};color:{tc};
                   font-size:11px;font-weight:700;padding:2px 10px;border-radius:4px;margin-top:3px">{sig}</span>
    </div>
  </div>
  <!-- 시그널 요약 -->
  <div style="padding:10px 20px;font-size:12.5px;color:#6c757d;border-bottom:1px solid #f0f0f0">
    {summ}
  </div>
  <!-- 경영층 초안 -->
  <div style="padding:14px 20px;border-bottom:1px solid #f0f0f0">
    <div style="font-size:10px;font-weight:700;color:#e67e22;letter-spacing:.8px;
                text-transform:uppercase;margin-bottom:8px">📤 경영층 문자 초안 (카카오톡/SMS)</div>
    <div style="background:#fff8f0;border:1.5px solid #f0c080;border-radius:8px;
                padding:12px 16px;font-size:13px;line-height:1.9;color:#2c3e50">
      {me_html}
    </div>
  </div>
  <!-- 포트폴리오사 문의 초안 -->
  <div style="padding:14px 20px">
    <div style="font-size:10px;font-weight:700;color:#2980b9;letter-spacing:.8px;
                text-transform:uppercase;margin-bottom:8px">💼 포트폴리오사 문의 초안</div>
    <div style="background:#f0f8ff;border:1.5px solid #a8d4f5;border-radius:8px;
                padding:12px 16px;font-size:13px;line-height:1.9;color:#2c3e50">
      {mp_html}
    </div>
    <div style="font-size:10px;color:#adb5bd;text-align:right;margin-top:6px">
      ※ AI 생성 초안 — 발송 전 반드시 검토·수정 후 사용하세요.
    </div>
  </div>
</div>"""

        drafts_section = f"""
  <!-- ━━━━ Section 2: 커뮤니케이션 초안 ━━━━ -->
  <div style="margin-top:32px"></div>

  <!-- 섹션 구분 탭 헤더 -->
  <table width="100%" cellspacing="0" cellpadding="0" style="margin-bottom:0;border-collapse:collapse">
    <tr>
      <td style="background:#f0f4f8;border-radius:10px 10px 0 0;padding:12px 22px 10px;
                 border-top:3px solid #8e44ad;border-left:1px solid #dee2e6;border-right:1px solid #dee2e6;
                 width:auto">
        <span style="font-size:14px;font-weight:800;color:#6c3483;letter-spacing:-.2px">
          💬 커뮤니케이션 초안
        </span>
        <span style="font-size:11px;color:#9b59b6;margin-left:8px">
          Communication Drafts — 🔴 즉시검토 {len(drafts_data)}건
        </span>
      </td>
    </tr>
  </table>
  <div style="background:#fff;border:1px solid #dee2e6;border-top:none;border-radius:0 0 12px 12px;
              padding:20px 20px 8px">
    <div style="background:#fff3cd;border-left:4px solid #f39c12;border-radius:0 8px 8px 0;
                padding:10px 16px;margin-bottom:18px;font-size:12.5px;color:#7d4e00">
      💡 <b>사용 방법:</b> 아래 초안을 복사하여 카카오톡·이메일에 붙여넣기 후 내용을 검토·수정하세요.
    </div>
    {draft_cards_html}
  </div>"""

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
{_HTML_STYLE.replace("{{", "{").replace("}}", "}")}
</head>
<body>
<div class="wrapper">

  <!-- ── 헤더 -->
  <div class="header">
    <div class="header-badge">Portfolio Intelligence</div>
    <h1>Daily Briefing</h1>
    <div class="sub">{date_str} &nbsp;·&nbsp; {total}건 분류 &nbsp;·&nbsp; {companies}개사 모니터링</div>
  </div>
  <div class="header-strip">
    <span class="strip-item hi">사업개발팀 내부용</span>
    <span class="strip-sep"></span>
    <span class="strip-item">CONFIDENTIAL</span>
    <span class="strip-sep"></span>
    <span class="strip-item">AI-Generated · 투자 판단 시 직접 검토 필요</span>
  </div>

  <!-- ── 지표 카드 (table 레이아웃 — 균등 너비 보장) -->
  <table width="100%" cellspacing="0" cellpadding="0" style="margin:14px 0 16px;border-collapse:separate;border-spacing:8px 0">
    <tr>
      <td width="33%" style="background:#fff;border-radius:10px;padding:16px 8px 14px;
          text-align:center;border-top:3px solid #e74c3c;
          box-shadow:0 2px 8px rgba(0,0,0,.06)">
        <div style="font-size:34px;font-weight:800;line-height:1;color:#c0392b">{len(reds)}</div>
        <div style="font-size:10.5px;color:#adb5bd;margin-top:5px;line-height:1.5;font-weight:500">즉시검토<br>Urgent Review</div>
      </td>
      <td width="33%" style="background:#fff;border-radius:10px;padding:16px 8px 14px;
          text-align:center;border-top:3px solid #f39c12;
          box-shadow:0 2px 8px rgba(0,0,0,.06)">
        <div style="font-size:34px;font-weight:800;line-height:1;color:#d68910">{len(yellows)}</div>
        <div style="font-size:10.5px;color:#adb5bd;margin-top:5px;line-height:1.5;font-weight:500">모니터링<br>To Watch</div>
      </td>
      <td width="34%" style="background:#fff;border-radius:10px;padding:16px 8px 14px;
          text-align:center;border-top:3px solid #adb5bd;
          box-shadow:0 2px 8px rgba(0,0,0,.06)">
        <div style="font-size:34px;font-weight:800;line-height:1;color:#868e96">{len(whites)}</div>
        <div style="font-size:10.5px;color:#adb5bd;margin-top:5px;line-height:1.5;font-weight:500">참고<br>Reference</div>
      </td>
    </tr>
  </table>

  <!-- ── Executive Summary -->
  <div class="exec-box" style="background:{exec_bg};border-color:{exec_border};">
    <span class="e-label" style="color:{exec_label_color};">Executive Summary</span>
    <div class="e-main" style="color:#1a1a2e;line-height:2;white-space:pre-line">{exec_msg}</div>
    <div class="e-sub" style="color:#868e96;line-height:1.9;white-space:pre-line">{exec_en}</div>
  </div>

  <!-- ━━━━ Section 1: 포트폴리오 현황 탭 헤더 ━━━━ -->
  <table width="100%" cellspacing="0" cellpadding="0" style="margin-bottom:0;border-collapse:collapse;margin-top:10px">
    <tr>
      <td style="background:#f0f4f8;border-radius:10px 10px 0 0;padding:12px 22px 10px;
                 border-top:3px solid #c0392b;border-left:1px solid #dee2e6;border-right:1px solid #dee2e6">
        <span style="font-size:14px;font-weight:800;color:#c0392b;letter-spacing:-.2px">
          📅 Daily 시그널 현황
        </span>
        <span style="font-size:11px;color:#868e96;margin-left:8px">
          Monitoring &amp; Actions
        </span>
      </td>
    </tr>
  </table>
  <div style="background:#fff;border:1px solid #dee2e6;border-top:none;border-radius:0 0 12px 12px;
              padding:18px 18px 8px">
    {cards}
    {white_note}
  </div>

  {drafts_section}

  <!-- ── 푸터 -->
  <div class="footer">
    <div class="conf-tag">Confidential · Internal Use Only</div><br>
    <b>Portfolio Intelligence Agent</b> &nbsp;·&nbsp; 사업개발팀<br>
    본 리포트는 AI 자동 분류 결과입니다. 최종 투자 판단은 반드시 직접 검토 후 결정하시기 바랍니다.<br>
    This report is AI-generated. All investment decisions require independent validation.
  </div>

</div>
</body></html>"""


# =============================================================================
# ⚪ 참고 항목 CSV
# =============================================================================

def build_white_csv(whites: list[ClassifiedSignal]) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["회사", "신호유형", "감성", "요약(KO)", "요약(EN)", "출처", "URL", "수집일"])
    for s in whites:
        writer.writerow([s.portfolio_name, s.signal_type, s.sentiment,
                         s.summary_ko, s.summary_en, s.source,
                         s.url, s.published_at[:10]])
    return buf.getvalue().encode("utf-8-sig")


# =============================================================================
# PDF 리포트 생성 (reportlab)
# 설치: pip install reportlab
# =============================================================================

def build_daily_pdf(signals: list[ClassifiedSignal], date_str: str) -> bytes:
    """경영층 보고용 PDF 리포트 — 한국어 + 영어 듀얼."""
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import mm
        from reportlab.platypus import (
            SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
            HRFlowable, KeepTogether
        )
        from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    except ImportError:
        logger.error("[PDF] reportlab 미설치 — pip install reportlab")
        return b""

    # 한글 폰트 등록 (TTFont — Malgun Gothic 직접 임베딩)
    from reportlab.pdfbase.ttfonts import TTFont
    KO_FONT = "Helvetica"
    for font_path in [
        "C:/Windows/Fonts/malgun.ttf",
        "C:/Windows/Fonts/malgunbd.ttf",
        "C:/Windows/Fonts/gulim.ttc",
    ]:
        try:
            pdfmetrics.registerFont(TTFont("KoreanFont", font_path))
            KO_FONT = "KoreanFont"
            break
        except Exception:
            continue
    if KO_FONT == "Helvetica":
        logger.warning("[PDF] 한글 폰트를 찾지 못했습니다. 한글이 깨질 수 있습니다.")

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=18*mm, rightMargin=18*mm,
        topMargin=16*mm, bottomMargin=16*mm,
    )

    # 색상
    NAVY   = colors.HexColor("#1a1a2e")
    GOLD   = colors.HexColor("#e8d5b7")
    RED    = colors.HexColor("#c0392b")
    ORANGE = colors.HexColor("#e67e22")
    LIGHT  = colors.HexColor("#f8f9fa")
    GRAY   = colors.HexColor("#6c757d")
    RED_BG   = colors.HexColor("#fff0f0")
    YEL_BG   = colors.HexColor("#fffbf0")

    styles = getSampleStyleSheet()

    def style(name, **kw):
        kw.setdefault("fontName", KO_FONT)
        return ParagraphStyle(name, **kw)

    def esc(text: str) -> str:
        """reportlab Paragraph XML 이스케이프 (& → &amp; 등)."""
        return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    s_title  = style("T", fontSize=16, textColor=GOLD,
                     spaceAfter=2, leading=20)
    s_sub    = style("S", fontSize=10, textColor=GRAY,
                     spaceAfter=0)
    s_h2     = style("H2", fontSize=12, textColor=NAVY,
                     spaceBefore=10, spaceAfter=4, fontName=KO_FONT)
    s_body   = style("B", fontSize=9, textColor=colors.black,
                     leading=13, spaceAfter=2)
    s_en     = style("E", fontSize=8.5, textColor=GRAY,
                     leading=12, spaceAfter=4)
    s_center = style("C", fontSize=9, alignment=TA_CENTER)
    s_small  = style("SM", fontSize=8, textColor=GRAY)

    elements = []

    # ── 헤더 박스
    header_data = [[
        Paragraph(f"Portfolio Intelligence Report", s_title),
        Paragraph(f"CONFIDENTIAL", style("CF", fontSize=9,
                  textColor=RED, alignment=TA_RIGHT)),
    ],[
        Paragraph(f"Daily Briefing | {date_str}", s_sub),
        Paragraph(f"사업개발팀 내부용", style("CF2", fontSize=8,
                  textColor=GRAY, alignment=TA_RIGHT)),
    ]]
    header_tbl = Table(header_data, colWidths=["70%", "30%"])
    header_tbl.setStyle(TableStyle([
        ("BACKGROUND",  (0,0), (-1,-1), NAVY),
        ("LEFTPADDING",  (0,0), (-1,-1), 12),
        ("RIGHTPADDING", (0,0), (-1,-1), 12),
        ("TOPPADDING",   (0,0), (-1,-1), 10),
        ("BOTTOMPADDING",(0,0), (-1,-1), 10),
        ("VALIGN", (0,0), (-1,-1), "MIDDLE"),
    ]))
    elements.append(header_tbl)
    elements.append(Spacer(1, 8*mm))

    # ── 시그널 집계 요약
    reds    = [s for s in signals if s.action_flag == "red"]
    yellows = [s for s in signals if s.action_flag == "yellow"]
    whites  = [s for s in signals if s.action_flag == "white"]
    companies = sorted(set(s.portfolio_name for s in signals))

    s_th = style("TH", fontSize=9, textColor=colors.white, alignment=TA_CENTER)
    summary_data = [
        [Paragraph("구분", s_th),
         Paragraph("🔴 즉시검토", style("RC", fontSize=9, textColor=colors.HexColor("#ff9999"), alignment=TA_CENTER)),
         Paragraph("🟡 모니터링", style("YC", fontSize=9, textColor=colors.HexColor("#ffd700"), alignment=TA_CENTER)),
         Paragraph("⚪ 참고", s_th),
         Paragraph("합계", s_th)],
    ]
    for co in companies:
        co_sigs = [s for s in signals if s.portfolio_name == co]
        r = sum(1 for s in co_sigs if s.action_flag == "red")
        y = sum(1 for s in co_sigs if s.action_flag == "yellow")
        w = sum(1 for s in co_sigs if s.action_flag == "white")
        summary_data.append([
            Paragraph(esc(co), s_body),
            Paragraph(str(r) if r else "-", style("RN", fontSize=9, textColor=RED if r else GRAY, alignment=TA_CENTER)),
            Paragraph(str(y) if y else "-", style("YN", fontSize=9, textColor=ORANGE if y else GRAY, alignment=TA_CENTER)),
            Paragraph(str(w) if w else "-", s_center),
            Paragraph(str(r+y+w), s_center),
        ])
    summary_data.append([
        Paragraph("<b>합계 Total</b>", s_body),
        Paragraph(f"<b>{len(reds)}</b>", style("RB", fontSize=9, textColor=RED, alignment=TA_CENTER)),
        Paragraph(f"<b>{len(yellows)}</b>", style("YB", fontSize=9, textColor=ORANGE, alignment=TA_CENTER)),
        Paragraph(f"<b>{len(whites)}</b>", s_center),
        Paragraph(f"<b>{len(signals)}</b>", s_center),
    ])

    col_w = [60*mm, 28*mm, 28*mm, 28*mm, 28*mm]
    sum_tbl = Table(summary_data, colWidths=col_w)
    sum_tbl.setStyle(TableStyle([
        ("BACKGROUND",  (0,0), (-1,0), NAVY),
        ("TEXTCOLOR",   (0,0), (-1,0), colors.white),
        ("BACKGROUND",  (0,-1), (-1,-1), LIGHT),
        ("GRID", (0,0), (-1,-1), 0.4, colors.lightgrey),
        ("ROWBACKGROUNDS", (0,1), (-1,-2), [colors.white, colors.HexColor("#f8f9fa")]),
        ("VALIGN", (0,0), (-1,-1), "MIDDLE"),
        ("TOPPADDING",    (0,0), (-1,-1), 5),
        ("BOTTOMPADDING", (0,0), (-1,-1), 5),
        ("LEFTPADDING",   (0,0), (-1,-1), 6),
    ]))
    elements.append(Paragraph("1. 시그널 현황 요약 | Signal Overview", s_h2))
    elements.append(sum_tbl)
    elements.append(Spacer(1, 6*mm))

    # ── 🔴🟡 상세 테이블
    if reds or yellows:
        detail_header = [
            Paragraph("회사 / Company", s_th),
            Paragraph("요약 (한국어)", s_th),
            Paragraph("Summary (English)", s_th),
            Paragraph("신호유형", s_th),
            Paragraph("투자 시사점", s_th),
        ]
        detail_data = [detail_header]

        def implication(s: ClassifiedSignal) -> str:
            """신호 유형별 투자 시사점 템플릿."""
            templates = {
                "펀딩·밸류에이션": "밸류에이션 변동에 따른 지분가치 재평가 필요",
                "경영진 변동":     "경영 안정성 및 전략 방향 변화 모니터링",
                "파트너십·협업":   "시너지 효과 및 사업 확장 가능성 평가",
                "제품·기술 출시":  "시장 경쟁력 및 매출 영향 점검",
                "규제·법률 리스크":"법적 리스크 노출 수준 및 재무 영향 검토",
                "재무·실적":       "실적 트렌드 분석 및 투자 회수 시점 재검토",
                "M&A·Exit":        "Exit 기회 또는 희석 위험 즉각 검토 요망",
                "평판·ESG":        "브랜드 가치 영향 및 ESG 리스크 평가",
                "기타":            "추가 정보 수집 후 판단 필요",
            }
            return templates.get(s.signal_type, "추가 모니터링 필요")

        for s in (reds + yellows):
            flag_color = RED_BG if s.action_flag == "red" else YEL_BG
            detail_data.append([
                Paragraph(f"{s.flag_emoji} <b>{esc(s.portfolio_name)}</b>", s_body),
                Paragraph(esc(s.summary_ko or "-"), s_body),
                Paragraph(esc(s.summary_en or "-"), s_en),
                Paragraph(esc(s.signal_type), s_small),
                Paragraph(esc(implication(s)), s_small),
            ])

        detail_tbl = Table(
            detail_data,
            colWidths=[32*mm, 45*mm, 40*mm, 26*mm, 30*mm],
        )
        detail_tbl.setStyle(TableStyle([
            ("BACKGROUND",   (0,0), (-1,0), NAVY),
            ("TEXTCOLOR",    (0,0), (-1,0), colors.white),
            ("GRID",         (0,0), (-1,-1), 0.4, colors.lightgrey),
            ("VALIGN",       (0,0), (-1,-1), "TOP"),
            ("ROWBACKGROUNDS",(0,1), (-1,-1), [colors.white, LIGHT]),
            ("TOPPADDING",    (0,0), (-1,-1), 5),
            ("BOTTOMPADDING", (0,0), (-1,-1), 5),
            ("LEFTPADDING",   (0,0), (-1,-1), 5),
            ("FONTSIZE",      (0,0), (-1,-1), 8.5),
        ]))
        elements.append(Paragraph("2. 즉시검토 · 모니터링 항목 상세 | Urgent & Watch Items", s_h2))
        elements.append(detail_tbl)
        elements.append(Spacer(1, 4*mm))

    # ── ⚪ 참고 항목 (간략)
    if whites:
        white_rows = [[
            Paragraph("회사", s_center),
            Paragraph("요약 (KO)", s_center),
            Paragraph("신호유형", s_center),
        ]]
        for s in whites:
            white_rows.append([
                Paragraph(esc(s.portfolio_name), s_small),
                Paragraph(esc((s.summary_ko or "")[:80]), s_small),
                Paragraph(esc(s.signal_type), s_small),
            ])
        w_tbl = Table(white_rows, colWidths=[35*mm, 95*mm, 40*mm])
        w_tbl.setStyle(TableStyle([
            ("BACKGROUND",   (0,0), (-1,0), colors.HexColor("#ced4da")),
            ("GRID",         (0,0), (-1,-1), 0.3, colors.lightgrey),
            ("ROWBACKGROUNDS",(0,1), (-1,-1), [colors.white, LIGHT]),
            ("TOPPADDING",    (0,0), (-1,-1), 4),
            ("BOTTOMPADDING", (0,0), (-1,-1), 4),
            ("LEFTPADDING",   (0,0), (-1,-1), 5),
            ("FONTSIZE",      (0,0), (-1,-1), 8),
            ("VALIGN",        (0,0), (-1,-1), "TOP"),
        ]))
        elements.append(Paragraph("3. 참고 항목 | Reference Items", s_h2))
        elements.append(w_tbl)

    # ── 면책 문구
    elements.append(Spacer(1, 6*mm))
    elements.append(HRFlowable(width="100%", thickness=0.5, color=GRAY))
    elements.append(Spacer(1, 3*mm))
    elements.append(Paragraph(
        "본 리포트는 AI 기반 자동 분류 결과이며, 최종 투자 판단은 반드시 직접 검토 후 결정하시기 바랍니다. "
        "This report is generated by an AI classification system. All investment decisions must be validated independently.",
        style("D", fontSize=7.5, textColor=GRAY)
    ))

    doc.build(elements)
    return buf.getvalue()



# =============================================================================
# Claude Anthropic API — Weekly·Monthly 심층 분석 전용
# Daily는 Groq 유지, Weekly/Monthly는 Claude Sonnet 사용
# =============================================================================

def _call_claude(prompt: str,
                 model: str = "gpt-4o",
                 max_tokens: int = 1200) -> str:
    """
    Anthropic Claude API 호출 (Weekly·Monthly 심층 분석 전용).
    실패 시 빈 문자열 반환 → 호출부에서 폴백 처리.
    """
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        logger.warning("[Claude] ANTHROPIC_API_KEY 없음 — 폴백")
        return ""
    try:
        resp = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=40,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        logger.warning(f"[Claude] API 호출 실패: {e}")
        return ""


_WEEKLY_INSIGHT_PROMPT = """당신은 SK네트웍스 사업개발팀의 시니어 포트폴리오 담당자입니다.
아래는 이번 주 ({date_range}) 포트폴리오사에서 수집된 주요 시그널 목록입니다.

{signals_json}

아래 형식을 반드시 지켜 한국어로 작성하세요. 줄글 금지, 보고서 항목 형식으로 작성.

[핵심 이슈]
▪ {{기업명}}: {{이슈 핵심을 명사형으로, 같은 기업의 여러 이슈는 ' · '로 구분해 한 줄에 작성}}
(이슈가 있는 기업만, 기업당 반드시 1줄, 최대 3개 기업. 같은 기업명을 두 줄 이상 절대 쓰지 말 것)

[팔로업 사항]
① {{구체적 확인·검토 액션, 담당자/대상 명시}}
② {{구체적 확인·검토 액션}}
(2~3개, 특정 팀이 아닌 포트폴리오 관리 관점으로. 같은 기업 관련 액션은 한 항목으로 통합)

[다음 주 모니터링 포인트]
▸ {{리스크 또는 주시 사항 1~2개, 간결하게}}

공통 규칙: 모든 항목은 반드시 주어(회사명 등 주체)로 시작할 것. 주어 없는 문구 금지.
(좋은 예: "업스테이지, 규제 리스크 법률 변화 주시" / 나쁜 예: "규제 리스크 법률 변화 주시")

JSON 없이 위 포맷 그대로 출력하세요."""


_MONTHLY_INSIGHT_PROMPT = """당신은 SK네트웍스 사업개발팀의 시니어 포트폴리오 담당자입니다.
아래는 이번 달 ({date_range}) 포트폴리오사에서 수집된 시그널 목록입니다.

이달 수집 통계: {stats}

{signals_json}

아래 JSON 스키마로만 출력하세요. 코드블록·설명 없이 JSON만.
{{
  "diagnosis": "이달의 진단 1문장 (70자 이내) — 위 수집 통계의 정량 수치(즉시검토 건수 등)와 정성 판단을 함께 담고, 집중 관리 대상 기업은 반드시 회사명으로 명시할 것 (예: 업스테이지·컬리 집중 관리 필요)",
  "top3": [
    {{"company": "회사명", "status": "핵심 현황, 15~20자 명사형", "impact": "당사 지분가치·Exit·평판 관점 영향, 15~20자", "action": "권고 대응, 15~20자"}}
  ],
  "exec_decision": "경영층 판단 필요 사안 — 'N건 — 회사명: [구체적 의사결정 사안]' 형식. 각 건마다 홀드·추가투자·Exit·손상인식·관계정리 등 액션 키워드를 반드시 포함할 것. 없으면 '없음 — 정기 모니터링 유지'"
}}

규칙:
- top3는 정확히 3개, 중요도 순, 같은 회사 중복 금지
- 모든 문구는 주체가 명확해야 하며 말줄임(...) 금지, 완결된 명사형 어구로 작성
- status/impact/action은 각각 20자를 넘기지 말 것"""


def _generate_weekly_insight(signals: list) -> str:
    """Claude Sonnet으로 주간 종합 인사이트 생성. 실패 시 기본 문구 반환."""
    from signal_db import SignalDB
    w_start, w_end = SignalDB.weekly_range()
    date_range = f"{w_start.strftime('%Y.%m.%d')}~{w_end.strftime('%m.%d')}"

    reds    = [s for s in signals if s.action_flag == "red"]
    yellows = [s for s in signals if s.action_flag == "yellow"]
    key_signals = sorted(reds + yellows,
                         key=lambda x: x.source_tier)[:8]
    if not key_signals:
        return f"이번 주({date_range}) 주요 이슈 없음 — 정기 모니터링 유지."

    signals_json = json.dumps([{
        "company":     s.portfolio_name,
        "signal_type": s.signal_type,
        "flag":        s.action_flag,
        "summary_ko":  s.summary_ko,
        "sentiment":   s.sentiment,
    } for s in key_signals], ensure_ascii=False, indent=2)

    result = _call_claude(
        _WEEKLY_INSIGHT_PROMPT.format(signals_json=signals_json, date_range=date_range),
        max_tokens=600,
    )
    if result:
        logger.info("[Claude] 주간 인사이트 생성 완료")
        return result

    # Groq 폴백
    logger.info("[Claude→Groq] 주간 인사이트 폴백")
    try:
        from classifier_groq import _call
        return _call(_WEEKLY_INSIGHT_PROMPT.format(signals_json=signals_json, date_range=date_range)) or                "주간 인사이트 생성 실패 — 수동 검토 필요."
    except Exception:
        return "주간 인사이트 생성 실패 — 수동 검토 필요."


def _generate_monthly_insight(signals: list, year: int, month: int) -> str:
    """Claude Sonnet으로 월간 심층 분석 생성. 실패 시 기본 문구 반환."""
    from signal_db import SignalDB
    m_start, m_end = SignalDB.monthly_range()
    date_range = f"{m_start.strftime('%Y.%m.%d')}~{m_end.strftime('%m.%d')}"

    key_signals = sorted(signals,
        key=lambda x: ({"red":0,"yellow":1,"white":2}[x.action_flag],
                       x.source_tier))[:12]
    if not key_signals:
        return f"{year}년 {month}월({date_range}) 주요 포트폴리오 이슈 없음."

    signals_json = json.dumps([{
        "company":     s.portfolio_name,
        "signal_type": s.signal_type,
        "flag":        s.action_flag,
        "summary_ko":  s.summary_ko,
        "summary_en":  s.summary_en,
        "sentiment":   s.sentiment,
        "source":      s.source,
    } for s in key_signals], ensure_ascii=False, indent=2)

    _n_red    = sum(1 for s in signals if s.action_flag == "red")
    _n_yellow = sum(1 for s in signals if s.action_flag == "yellow")
    _n_co     = len({s.portfolio_name for s in signals})
    stats = (f"총 {len(signals)}건 / 즉시검토 {_n_red}건 / "
             f"동향주시 {_n_yellow}건 / {_n_co}개사")

    result = _call_claude(
        _MONTHLY_INSIGHT_PROMPT.format(signals_json=signals_json, date_range=date_range, stats=stats),
        model="gpt-4o",
        max_tokens=1200,
    )
    if result:
        logger.info("[Claude] 월간 심층 분석 생성 완료")
        return result

    # Groq 폴백
    logger.info("[Claude→Groq] 월간 분석 폴백")
    try:
        from classifier_groq import _call
        return _call(_MONTHLY_INSIGHT_PROMPT.format(signals_json=signals_json, date_range=date_range, stats=stats)) or                "월간 분석 생성 실패 — 수동 검토 필요."
    except Exception:
        return "월간 분석 생성 실패 — 수동 검토 필요."


def _monthly_insight_as_text(raw: str) -> str:
    """월간 인사이트 JSON 응답을 이메일용 텍스트 포맷으로 변환. JSON 아니면 그대로 반환."""
    try:
        i, j = raw.find("{"), raw.rfind("}")
        data = json.loads(raw[i:j + 1]) if i != -1 and j > i else None
        if not data:
            return raw
        lines = ["[이달의 진단]", data.get("diagnosis", ""), "", "[Top 3 핵심 이슈]"]
        for it in (data.get("top3") or [])[:3]:
            lines.append(f"▪ {it.get('company','')}: {it.get('status','')}"
                         f" → 영향: {it.get('impact','')} → 대응: {it.get('action','')}")
        lines += ["", "[경영층 판단 필요]", data.get("exec_decision", "")]
        return "\n".join(lines)
    except Exception:
        return raw


# =============================================================================
# Weekly HTML — 팀장/임원용 (4섹션 구조)
# =============================================================================

def build_weekly_html(signals: list[ClassifiedSignal], week_range: str,
                      ai_insight: str = "") -> str:
    """Weekly 리포트 — Executive Summary + 히트맵 + 모니터링 포인트 + 투자 시사점 + 권고 액션.
    ai_insight: Claude Sonnet이 생성한 주간 종합 인사이트 (없으면 빈 문자열)."""
    by_company: dict[str, list[ClassifiedSignal]] = defaultdict(list)
    for s in signals:
        by_company[s.portfolio_name].append(s)

    total_red    = sum(1 for s in signals if s.action_flag == "red")
    total_yellow = sum(1 for s in signals if s.action_flag == "yellow")

    # ── Executive Summary
    top3_signals = sorted(signals,
                          key=lambda x: ({"red":0,"yellow":1,"white":2}[x.action_flag],
                                         x.source_tier))[:3]
    top3_items = "".join(
        f"<li style='margin:5px 0;font-size:13px'>"
        f"{s.flag_emoji} <b>{s.portfolio_name}</b> [{s.signal_type}] — {s.summary_ko}</li>"
        for s in top3_signals
    )

    # ── 히트맵
    signal_types = [
        "펀딩·밸류에이션", "경영진 변동", "파트너십·협업", "제품·기술 출시",
        "규제·법률 리스크", "재무·실적", "M&A·Exit", "평판·ESG",
    ]
    type_headers = "".join(
        f"<th style='font-size:9px;padding:6px 3px;writing-mode:vertical-rl;"
        f"transform:rotate(180deg);color:white'>{t}</th>"
        for t in signal_types
    )
    _DISPLAY_PRIORITY = {"업스테이지": 1, "비엠스마일": 2, "컬리": 3, "에버온": 4}
    def _weekly_sort(item):
        co, arts = item
        flag_score = min({"red":0,"yellow":1,"white":2}.get(a.action_flag,2) for a in arts)
        return (flag_score, _DISPLAY_PRIORITY.get(co, 99), co)
    heatmap_rows = ""
    for company, arts in sorted(by_company.items(), key=_weekly_sort):
        counts = {st: sum(1 for a in arts if a.signal_type == st) for st in signal_types}
        cells = "".join(
            f"<td style='text-align:center;padding:6px;"
            f"background:{'#e74c3c' if c >= 2 else '#f39c12' if c == 1 else '#f8f9fa'};"
            f"color:{'white' if c >= 1 else '#ccc'};font-size:13px'>"
            f"{'●' if c else '·'}</td>"
            for c in counts.values()
        )
        r = sum(1 for a in arts if a.action_flag == "red")
        y = sum(1 for a in arts if a.action_flag == "yellow")
        heatmap_rows += (
            f"<tr><td style='padding:6px 10px;font-weight:600;white-space:nowrap'>{company}"
            f"<span style='font-size:11px;margin-left:6px;color:#c0392b'>{'🔴' if r else ''}</span>"
            f"</td>{cells}</tr>"
        )

    # ── 모니터링 포인트 + 투자 시사점 + 권고 액션 (회사별)
    company_sections = ""
    for company, arts in sorted(by_company.items(), key=_weekly_sort):
        reds_co    = [a for a in arts if a.action_flag == "red"]
        yellows_co = [a for a in arts if a.action_flag == "yellow"]
        top_arts   = sorted(arts, key=lambda x: ({"red":0,"yellow":1,"white":2}[x.action_flag],
                                                  x.source_tier))[:3]
        border_color = "#e74c3c" if reds_co else "#f39c12" if yellows_co else "#ced4da"

        monitoring_items = "".join(
            f"<li style='margin:8px 0;font-size:13px;line-height:1.55'>"
            f"{a.flag_emoji} <b>[{a.signal_type}]</b> {a.summary_ko}<br>"
            f"<span style='color:#888;font-style:italic;font-size:12px'>{a.summary_en}</span>"
            f"<a href='{a.url}' style='font-size:11px;color:#3498db;margin-left:8px'>원문 →</a>"
            f"</li>"
            for a in top_arts
        )

        # 이번 주 대표 시그널 기준 투자 시사점
        top_sig = top_arts[0] if top_arts else None
        implication = _implication_template(top_sig.signal_type) if top_sig else "해당 없음"
        action = (
            _action_template(top_sig.signal_type, top_sig.action_flag)
            if top_sig else "정기 모니터링 유지"
        )

        company_sections += f"""
        <div style='background:#fff;border-radius:10px;padding:16px;margin-bottom:14px;
                    border-left:4px solid {border_color};box-shadow:0 1px 4px rgba(0,0,0,.06)'>
          <div style='display:flex;justify-content:space-between;align-items:center;margin-bottom:10px'>
            <h3 style='color:#1a1a2e;font-size:15px;font-weight:700;margin:0'>{company}</h3>
            <span style='font-size:12px'>
              {"🔴 " + str(len(reds_co)) + "건" if reds_co else ""}
              {"&nbsp;🟡 " + str(len(yellows_co)) + "건" if yellows_co else ""}
            </span>
          </div>
          <ul style='padding-left:18px;margin-bottom:10px'>{monitoring_items}</ul>
          <div style='background:#f8f9fa;border-radius:6px;padding:8px 12px;margin-top:6px;font-size:12px'>
            💡 <b>투자 시사점:</b> {implication}
          </div>
          <div style='background:#e8f4fd;border-radius:6px;padding:8px 12px;margin-top:4px;font-size:12px'>
            ⚡ <b>권고 액션 (이번 주):</b> {action}
          </div>
        </div>"""

    return f"""<!DOCTYPE html>
<html lang="ko">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
{_HTML_STYLE.replace("{{", "{").replace("}}", "}")}
</head>
<body>
<div class="wrapper" style="max-width:700px">

  <!-- 헤더 -->
  <div class="header">
    <h1>📈 Portfolio Intelligence · Weekly Report</h1>
    <div class="sub">{week_range} &nbsp;|&nbsp; {len(signals)}건 시그널 &nbsp;|&nbsp; {len(by_company)}개사</div>
  </div>

  <!-- 지표 (table 레이아웃) -->
  <table width="100%" cellspacing="0" cellpadding="0" style="margin:14px 0 16px;border-collapse:separate;border-spacing:8px 0">
    <tr>
      <td width="33%" style="background:#fff;border-radius:10px;padding:16px 8px 14px;
          text-align:center;border-top:3px solid #e74c3c;box-shadow:0 2px 8px rgba(0,0,0,.06)">
        <div style="font-size:34px;font-weight:800;line-height:1;color:#c0392b">{total_red}</div>
        <div style="font-size:10.5px;color:#adb5bd;margin-top:5px;line-height:1.5">🔴 즉시검토<br>Urgent</div>
      </td>
      <td width="33%" style="background:#fff;border-radius:10px;padding:16px 8px 14px;
          text-align:center;border-top:3px solid #f39c12;box-shadow:0 2px 8px rgba(0,0,0,.06)">
        <div style="font-size:34px;font-weight:800;line-height:1;color:#d68910">{total_yellow}</div>
        <div style="font-size:10.5px;color:#adb5bd;margin-top:5px;line-height:1.5">🟡 모니터링<br>To Watch</div>
      </td>
      <td width="34%" style="background:#fff;border-radius:10px;padding:16px 8px 14px;
          text-align:center;border-top:3px solid #adb5bd;box-shadow:0 2px 8px rgba(0,0,0,.06)">
        <div style="font-size:34px;font-weight:800;line-height:1;color:#868e96">{len(by_company)}</div>
        <div style="font-size:10.5px;color:#adb5bd;margin-top:5px;line-height:1.5">🏢 모니터링<br>Companies</div>
      </td>
    </tr>
  </table>

  <!-- Executive Summary -->
  <div style="background:#f0f4ff;border-left:4px solid #3498db;border-radius:8px;
              padding:14px 16px;margin-bottom:16px">
    <div style="font-size:11px;font-weight:700;color:#888;margin-bottom:6px;
                text-transform:uppercase;letter-spacing:0.5px">Weekly Executive Summary</div>
    <p style="font-size:13px;color:#1a1a2e;margin-bottom:8px">
      이번 주 포트폴리오 {len(by_company)}개사에서 총 {len(signals)}건의 시그널이 수집되었습니다.
      {"즉시검토 필요 이슈 " + str(total_red) + "건 포함." if total_red else "즉시검토 이슈는 없으며 일반 모니터링 수준입니다."}
    </p>
    <div style="font-size:12px;color:#555">이번 주 주요 동향 Top 3:</div>
    <ul style="padding-left:18px;margin-top:4px;color:#2c3e50">{top3_items}</ul>
  </div>

  <!-- 히트맵 -->
  <div class="section-header"><span>🔥 신호 유형 히트맵 | Signal Heatmap</span></div>
  <div style="overflow-x:auto;margin-bottom:16px">
    <table style="border-collapse:collapse;font-size:12px;width:100%;background:#fff;
                  border-radius:8px;overflow:hidden;box-shadow:0 1px 4px rgba(0,0,0,.06)">
      <tr style="background:#1a1a2e">
        <th style="padding:8px 10px;text-align:left;color:white">회사</th>
        {type_headers}
      </tr>
      {heatmap_rows}
    </table>
  </div>

  <!-- 모니터링 포인트 + 투자 시사점 + 권고 액션 -->
  <div class="section-header"><span>📋 포트폴리오사별 모니터링 &amp; 액션</span></div>
  {company_sections}

  <div class="footer">
    <b>Portfolio Intelligence Agent</b> · 사업개발팀<br>
    본 리포트는 AI 분류 결과이며 투자 판단은 반드시 직접 검토 후 결정하십시오.
  </div>
</div>
</body></html>"""


# =============================================================================
# Monthly HTML — CEO/이사회용 (4섹션 구조)
# =============================================================================

def build_monthly_html(signals: list[ClassifiedSignal], year: int, month: int,
                       ai_insight: str = "") -> str:
    """Monthly 리포트 — CEO/이사회용. Executive Summary + 리스크 현황 + 투자 시사점 + 권고 액션.
    ai_insight: Claude Sonnet이 생성한 월간 심층 분석 (없으면 빈 문자열)."""
    by_company: dict[str, list[ClassifiedSignal]] = defaultdict(list)
    for s in signals:
        by_company[s.portfolio_name].append(s)

    total_red    = sum(1 for s in signals if s.action_flag == "red")
    total_yellow = sum(1 for s in signals if s.action_flag == "yellow")

    # ── Executive Summary — 포트폴리오 건전성 지수
    health_pct = max(0, 100 - total_red * 20 - total_yellow * 5)
    if health_pct >= 80:
        health_label = "양호 (Green)"; health_color = "#27ae60"
    elif health_pct >= 60:
        health_label = "주의 (Yellow)"; health_color = "#f39c12"
    else:
        health_label = "위험 (Red)"; health_color = "#e74c3c"

    # ── TOP 5 시그널
    top5 = sorted(signals,
                  key=lambda x: ({"red":0,"yellow":1,"white":2}[x.action_flag],
                                 x.source_tier))[:5]
    top5_rows = "".join(f"""
    <tr style='background:{"#fff0f0" if s.action_flag=="red" else "#fffbf0" if s.action_flag=="yellow" else "#fff"}'>
      <td style='padding:10px;font-weight:700;white-space:nowrap'>{s.flag_emoji} {s.portfolio_name}</td>
      <td style='padding:10px;font-size:12px'>{s.signal_type}</td>
      <td style='padding:10px;font-size:13px'>{s.summary_ko}</td>
      <td style='padding:10px;font-size:12px;color:#888;font-style:italic'>{s.summary_en}</td>
    </tr>""" for s in top5)

    # ── 포트폴리오사별 월간 현황 + 투자 시사점
    _DISPLAY_PRIORITY = {"업스테이지": 1, "비엠스마일": 2, "컬리": 3, "에버온": 4}
    def _monthly_sort(item):
        co, arts = item
        flag_score = min({"red":0,"yellow":1,"white":2}.get(a.action_flag,2) for a in arts)
        return (flag_score, _DISPLAY_PRIORITY.get(co, 99), co)
    company_rows = ""
    for co, arts in sorted(by_company.items(), key=_monthly_sort):
        r = sum(1 for s in arts if s.action_flag == "red")
        y = sum(1 for s in arts if s.action_flag == "yellow")
        w = sum(1 for s in arts if s.action_flag == "white")
        top_sig = sorted(arts, key=lambda x: ({"red":0,"yellow":1,"white":2}[x.action_flag],
                                               x.source_tier))[0]
        impl = _implication_template(top_sig.signal_type)
        risk_bg = "#fff0f0" if r else "#fffbf0" if y else "#fff"
        company_rows += f"""
        <tr style='background:{risk_bg};border-bottom:1px solid #eee'>
          <td style='padding:10px;font-weight:700'>{co}</td>
          <td style='padding:10px;text-align:center;color:#c0392b;font-weight:700'>{r if r else "-"}</td>
          <td style='padding:10px;text-align:center;color:#e67e22'>{y if y else "-"}</td>
          <td style='padding:10px;text-align:center;color:#888'>{w}</td>
          <td style='padding:10px;font-size:12px;color:#555'>{impl}</td>
        </tr>"""

    # ── 권고 액션 (이사회 수준)
    board_actions = []
    for co, arts in sorted(by_company.items(), key=_monthly_sort):
        reds_co = [a for a in arts if a.action_flag == "red"]
        if reds_co:
            sig_types = ", ".join(set(a.signal_type for a in reds_co))
            board_actions.append(
                f"<li style='margin:8px 0'><b>{co}</b> — {sig_types} 관련 이사회 보고 및 대응 방안 의결 필요</li>"
            )
    if not board_actions:
        board_actions = ["<li style='margin:8px 0'>이번 달 즉시검토 이슈 없음 — 정기 포트폴리오 리뷰 진행</li>"]

    next_focus_types = list(set(s.signal_type for s in signals
                                if s.action_flag in ("red","yellow")))[:3]
    next_focus_items = "".join(
        f"<li style='margin:6px 0'>📌 {t} 동향 집중 모니터링</li>"
        for t in next_focus_types
    ) or "<li style='margin:6px 0'>📌 전반적 정기 모니터링 유지</li>"

    return f"""<!DOCTYPE html>
<html lang="ko">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
{_HTML_STYLE.replace("{{", "{").replace("}}", "}")}
</head>
<body>
<div class="wrapper" style="max-width:750px">

  <!-- 헤더 -->
  <div class="header">
    <h1>📋 Portfolio Intelligence · Monthly Report</h1>
    <div class="sub">{year}년 {month}월 &nbsp;|&nbsp; {len(signals)}건 시그널 &nbsp;|&nbsp; {len(by_company)}개사 &nbsp;|&nbsp; CEO/이사회용</div>
  </div>

  <!-- Executive Summary — 포트폴리오 건전성 -->
  <div style="background:#fff;border-radius:12px;padding:20px;margin-bottom:16px;
              box-shadow:0 1px 6px rgba(0,0,0,.08);border-top:4px solid {health_color}">
    <div style="font-size:11px;font-weight:700;color:#888;margin-bottom:10px;
                text-transform:uppercase;letter-spacing:0.5px">Monthly Executive Summary</div>
    <div style="display:flex;align-items:center;gap:16px;margin-bottom:12px">
      <div style="text-align:center;background:{health_color}15;border-radius:8px;padding:12px 20px">
        <div style="font-size:28px;font-weight:800;color:{health_color}">{health_pct}</div>
        <div style="font-size:11px;color:{health_color};font-weight:700">건전성 지수</div>
      </div>
      <div style="flex:1">
        <div style="font-size:15px;font-weight:700;color:{health_color};margin-bottom:4px">
          포트폴리오 건전성: {health_label}
        </div>
        <div style="font-size:13px;color:#555;line-height:1.6">
          {month}월 중 {len(by_company)}개 포트폴리오사 모니터링 결과,
          🔴 즉시검토 {total_red}건 · 🟡 동향주시 {total_yellow}건이 확인되었습니다.
        </div>
      </div>
    </div>
  </div>

  <!-- 지표 카드 (table 레이아웃) -->
  <table width="100%" cellspacing="0" cellpadding="0" style="margin:14px 0 16px;border-collapse:separate;border-spacing:8px 0">
    <tr>
      <td width="33%" style="background:#fff;border-radius:10px;padding:16px 8px 14px;
          text-align:center;border-top:3px solid #e74c3c;box-shadow:0 2px 8px rgba(0,0,0,.06)">
        <div style="font-size:34px;font-weight:800;line-height:1;color:#c0392b">{total_red}</div>
        <div style="font-size:10.5px;color:#adb5bd;margin-top:5px;line-height:1.5">🔴 즉시검토<br>Urgent</div>
      </td>
      <td width="33%" style="background:#fff;border-radius:10px;padding:16px 8px 14px;
          text-align:center;border-top:3px solid #f39c12;box-shadow:0 2px 8px rgba(0,0,0,.06)">
        <div style="font-size:34px;font-weight:800;line-height:1;color:#d68910">{total_yellow}</div>
        <div style="font-size:10.5px;color:#adb5bd;margin-top:5px;line-height:1.5">🟡 모니터링<br>To Watch</div>
      </td>
      <td width="34%" style="background:#fff;border-radius:10px;padding:16px 8px 14px;
          text-align:center;border-top:3px solid #adb5bd;box-shadow:0 2px 8px rgba(0,0,0,.06)">
        <div style="font-size:34px;font-weight:800;line-height:1;color:#868e96">{len(by_company)}</div>
        <div style="font-size:10.5px;color:#adb5bd;margin-top:5px;line-height:1.5">🏢 포트폴리오<br>Companies</div>
      </td>
    </tr>
  </table>

  <!-- 모니터링 포인트 — TOP 5 시그널 -->
  <div class="section-header"><span>📌 이달의 주요 시그널 TOP 5 | Key Signals</span></div>
  <div style="overflow-x:auto;margin-bottom:16px">
    <table style="width:100%;border-collapse:collapse;font-size:13px;background:#fff;
                  border-radius:8px;overflow:hidden;box-shadow:0 1px 4px rgba(0,0,0,.06)">
      <tr style="background:#1a1a2e;color:white">
        <th style="padding:10px;text-align:left">포트폴리오사</th>
        <th style="padding:10px;text-align:left">신호유형</th>
        <th style="padding:10px;text-align:left">주요 내용 (KO)</th>
        <th style="padding:10px;text-align:left">Summary (EN)</th>
      </tr>
      {top5_rows}
    </table>
  </div>

  <!-- 투자 시사점 — 포트폴리오사별 현황 -->
  <div class="section-header"><span>💡 포트폴리오사별 월간 현황 &amp; 투자 시사점</span></div>
  <div style="overflow-x:auto;margin-bottom:16px">
    <table style="width:100%;border-collapse:collapse;font-size:13px;background:#fff;
                  border-radius:8px;overflow:hidden;box-shadow:0 1px 4px rgba(0,0,0,.06)">
      <tr style="background:#1a1a2e;color:white">
        <th style="padding:10px;text-align:left">포트폴리오사</th>
        <th style="padding:10px;color:#ff9999">🔴</th>
        <th style="padding:10px;color:#ffd700">🟡</th>
        <th style="padding:10px">⚪</th>
        <th style="padding:10px;text-align:left">투자 시사점</th>
      </tr>
      {company_rows}
    </table>
  </div>

  <!-- 권고 액션 — 이사회 수준 -->
  <div style="background:#fff;border-radius:10px;padding:16px;margin-bottom:12px;
              border-left:4px solid #e74c3c;box-shadow:0 1px 4px rgba(0,0,0,.06)">
    <div style="font-size:13px;font-weight:700;color:#1a1a2e;margin-bottom:8px">
      ⚡ 이사회 권고 액션 | Board-Level Actions
    </div>
    <ul style="font-size:13px;color:#2c3e50;padding-left:18px;line-height:1.7">
      {"".join(board_actions)}
    </ul>
  </div>

  <!-- 다음 달 모니터링 중점 -->
  <div style="background:#f0f4ff;border-radius:10px;padding:16px;margin-bottom:16px;
              border-left:4px solid #3498db">
    <div style="font-size:13px;font-weight:700;color:#1a1a2e;margin-bottom:8px">
      📅 다음 달 모니터링 중점 사항 | Next Month Focus
    </div>
    <ul style="font-size:13px;color:#2c3e50;padding-left:18px;line-height:1.7">
      {next_focus_items}
      <li style='margin:6px 0'>📊 분기 실적 발표 일정 사전 파악 및 IR 미팅 준비</li>
      <li style='margin:6px 0'>🌐 규제·시장 환경 변화에 따른 포트폴리오 리스크 재평가</li>
    </ul>
  </div>

  <div class="footer">
    <b>Portfolio Intelligence Agent</b> · 사업개발팀 (CEO/이사회 배포용)<br>
    본 리포트는 AI 분류 결과이며 투자 판단은 반드시 직접 검토 후 결정하십시오.<br>
    This report is AI-generated. All investment decisions require independent validation.
  </div>
</div>
</body></html>"""


# =============================================================================
# 경영층 보고 초안 생성 (Groq LLM)
# =============================================================================

_EXEC_DRAFT_PROMPT = """\
당신은 벤처캐피털(VC) 사업개발팀장의 보고서 작성을 지원하는 전문 AI 어시스턴트입니다.
아래 포트폴리오 모니터링 시스템이 감지한 즉시검토(🔴) 시그널 목록을 바탕으로,
CEO/임원에게 보고할 수 있는 수준의 공식 이메일 본문 초안을 작성하십시오.

## 시그널 목록
{signals_json}

## 작성 지침
1. 수신자는 CEO 또는 C-레벨 임원이므로 격식체(합쇼체)로 작성
2. 본문은 3~5문단, 각 문단은 2~3문장
3. 각 시그널의 투자 관점 의미(지분가치 영향, 리스크, 기회)를 1문장씩 포함
4. 마지막에 권고사항(bullet 3개, 각 1문장)을 JSON 배열로 별도 제공
5. 영문 요약(2~3문장)도 별도 작성 — 해외 파트너 공유용

## 출력 형식 (JSON)
{{
  "body_ko": "한국어 보고 본문 (전문, \\n으로 문단 구분)",
  "body_en": "English summary (2-3 sentences)",
  "recommendations": ["권고사항 1", "권고사항 2", "권고사항 3"]
}}

JSON만 출력. 코드블록 없이."""


def _finish_summary(text: str, limit: int = 80) -> str:
    """요약문을 '…' 없이 깔끔하게 마무리 — limit 초과 시 문장/단어 경계에서 절단."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    best = -1
    for p in (". ", "다.", "요.", "함.", "음.", "됨.", "임."):
        idx = cut.rfind(p)
        if idx + len(p) > best:
            best = idx + len(p)
    if best >= int(limit * 0.4):
        return cut[:best].strip()
    sp = cut.rfind(" ")
    if sp >= int(limit * 0.5):
        cut = cut[:sp]
    return cut.strip()


_CONTACT_DRAFT_PROMPT = """\

당신은 벤처캐피털 사업개발팀을 지원하는 시니어 커뮤니케이션 전문가입니다.
아래 포트폴리오사 이슈를 바탕으로 두 가지 메시지 초안을 작성하십시오.

## 시그널 정보
회사명: {company}
신호 유형: {signal_type}
기사 제목: {title}
주요 내용 (한국어): {summary_ko}
주요 내용 (English): {summary_en}
출처: {source} (Tier {source_tier})
감성: {sentiment}

## 시그널 유형별 커뮤니케이션 핵심
- M&A·Exit: 딜 진행 사실 여부, 당사 지분/Exit 일정 영향 파악이 우선
- 펀딩·밸류에이션: 라운드 조건·밸류에이션 변화, 기존 지분 희석 여부 확인
- 경영진 변동: 후임 선임 일정, 경영 공백 리스크, 당사 관계 유지 방안 확인
- IPO·상장: 주관사 선정 현황, 공모가 밴드, 보호예수 조건 확인
- 법적 리스크·소송: 소송 규모·진행 상황, 재무적 영향 범위 파악
- 재무·유동성 위기: 자금 조달 계획, 당사 지원 가능 여부 논의

## 작성 요청

### 1. 경영층 보고 문자 (카카오톡/SMS용)
- 수신: 투자담당 이사 또는 대표
- 분량: 4줄 이내 (모바일 1화면 내)
- 톤: 간결·격식체. "보고드립니다" 투로 마무리
- 구성: ① [{signal_type}] 레이블로 시작 ② {company} 관련 이슈 1줄 요약 ③ 구체적 우려 포인트 1줄 ④ 검토·지시 요청

### 2. 포트폴리오사 담당자 문의 메시지 (이메일 또는 카톡용)
- 발신: SK networks 사업개발팀 (투자사 측)
- 수신: {company} 대표 또는 CFO
- 분량: 5~7줄
- 톤: 투자 파트너십 기반, 우호적이되 사실 확인 목적 명확히
- 발신자 소개는 반드시 "SK networks 사업개발팀"으로 표기할 것. [Your Company Name], [회사명] 같은 placeholder 절대 사용 금지
- 구성: ① 인사·발신자 소개("안녕하세요, SK networks 사업개발팀입니다.") ② 언론 보도 내용 구체적 언급 ③ 사실 관계 확인 요청 (투자사 관점에서 중요한 1~2가지 포인트 명시) ④ 가능하다면 미팅/콜 제안 ⑤ 지원 의사 표명

## 출력 형식 (JSON)
{{"msg_exec": "경영층 문자 초안 (줄바꿈 \\n 사용)", "msg_portfolio": "포트폴리오사 문의 초안 (줄바꿈 \\n 사용)"}}

JSON만 출력. 코드블록 없이."""


def _draft_contact_messages(signal: "ClassifiedSignal") -> tuple[str, str]:
    """
    Groq LLM으로 🔴 시그널에 대한 두 가지 메시지 초안 생성.
    반환: (msg_exec, msg_portfolio)
    """
    try:
        from classifier_groq import _call
        prompt = _CONTACT_DRAFT_PROMPT.format(
            company     = signal.portfolio_name,
            signal_type = signal.signal_type,
            title       = signal.title or "",
            summary_ko  = signal.summary_ko or "",
            summary_en  = signal.summary_en or "",
            source      = signal.source or "",
            source_tier = signal.source_tier or "",
            sentiment   = signal.sentiment or "",
        )
        raw = _call(prompt)
        if raw:
            text = raw.strip()
            if text.startswith("```"):
                text = text.split("```")[1]
                if text.startswith("json"):
                    text = text[4:]
            result = json.loads(text.strip())
            msg_exec      = result.get("msg_exec", "")
            msg_portfolio = result.get("msg_portfolio", "")
            for _ph in ("[Your Company Name]", "[Your Company]", "[회사명]", "[투자사명]", "[당사명]"):
                msg_exec = msg_exec.replace(_ph, "SK networks")
                msg_portfolio = msg_portfolio.replace(_ph, "SK networks")
            if msg_exec:
                logger.info(f"[커뮤니케이션초안] {signal.portfolio_name} 초안 생성 완료")
                return msg_exec, msg_portfolio
    except Exception as e:
        logger.warning(f"[커뮤니케이션초안] 생성 실패, 기본 템플릿 사용: {e}")

    # ── 폴백 템플릿 (Groq 호출 실패 시)
    title_short = (signal.title or "")[:40]
    msg_exec = (
        f"[{signal.signal_type}] {signal.portfolio_name} 관련 이슈 보고드립니다.\n"
        f"({signal.source}) {title_short}\n"
        f"지분 가치 및 Exit 일정 영향 여부 검토가 필요합니다.\n"
        f"보고 일정 및 대응 방향 지시 부탁드립니다."
    )
    msg_portfolio = (
        f"안녕하세요 대표님, SK networks 사업개발팀입니다.\n\n"
        f"최근 {signal.source} 보도({title_short})와 관련하여 연락드립니다.\n"
        f"{signal.summary_ko or ''}\n\n"
        f"저희 투자사 입장에서 아래 사항을 확인드리고자 합니다:\n"
        f"1) 보도 내용의 사실 여부 및 현황\n"
        f"2) 향후 일정 및 당사에 공유 가능한 내용\n\n"
        f"가능하시면 이번 주 중 짧게 콜 한 번 부탁드려도 될까요?\n"
        f"필요한 사항이 있으시면 언제든지 연락 주시기 바랍니다."
    )
    return msg_exec, msg_portfolio


_IR_DRAFT_PROMPT = """\
당신은 벤처캐피털 IR(투자자 관계) 전문가입니다.
아래 포트폴리오사 이슈를 바탕으로 LP(기관투자자)에게 보낼 수 있는 간결한 IR 커뮤니케이션 초안을 작성하십시오.

## 이슈 정보
회사명: {company}
신호 유형: {signal_type}
주요 내용 (KO): {summary_ko}
English Summary: {summary_en}

## 작성 지침
1. 수신자: LP 또는 관계 투자자 (기관 수준 예우)
2. 분량: 3~4문단, 각 2~3문장
3. 톤: 전문적·객관적, 불필요한 우려 자제, 당사 모니터링 역량 어필
4. 구성: ① 현황 요약 → ② 당사 포지션·지분 영향 검토 → ③ 향후 모니터링 계획
5. 마지막 문장: "추가 문의사항은 사업개발팀으로 연락 주시기 바랍니다."

## 출력
한국어 본문만. 제목·서명 제외. 문단 구분은 빈 줄로."""

# IR 초안 트리거 신호 유형
_IR_TRIGGER_TYPES = {"M&A·Exit", "펀딩·밸류에이션"}


def _draft_ir_memo(signal: ClassifiedSignal) -> str:
    """
    M&A·Exit / 펀딩·밸류에이션 / IPO 언급 시그널에 대해
    Groq LLM으로 LP 대상 IR 커뮤니케이션 초안 생성.
    해당 없으면 빈 문자열 반환.
    """
    is_ir_type = signal.signal_type in _IR_TRIGGER_TYPES
    has_ipo    = ("IPO" in (signal.summary_ko or "") or
                  "IPO" in (signal.summary_en or "") or
                  "상장" in (signal.summary_ko or ""))
    if not (is_ir_type or has_ipo):
        return ""

    try:
        from classifier_groq import _call
        prompt = _IR_DRAFT_PROMPT.format(
            company     = signal.portfolio_name,
            signal_type = signal.signal_type,
            summary_ko  = signal.summary_ko or "",
            summary_en  = signal.summary_en or "",
        )
        result = _call(prompt)
        logger.info(f"[IR초안] {signal.portfolio_name} IR 초안 생성 완료")
        return result or ""
    except Exception as e:
        logger.warning(f"[IR초안] 생성 실패: {e}")
        return ""


def _draft_executive_body(
    reds: list[ClassifiedSignal],
) -> tuple[str, str, list[str]]:
    """
    Groq LLM으로 경영층 보고 본문 초안 생성.
    반환: (body_ko, body_en, recommendations)
    LLM 호출 실패 시 기본 템플릿으로 폴백.
    """
    try:
        from classifier_groq import _call, _parse

        signals_json = json.dumps([
            {
                "portfolio": s.portfolio_name,
                "signal_type": s.signal_type,
                "sentiment": s.sentiment,
                "summary_ko": s.summary_ko,
                "summary_en": s.summary_en,
                "source": s.source,
            }
            for s in reds
        ], ensure_ascii=False, indent=2)

        prompt = _EXEC_DRAFT_PROMPT.format(signals_json=signals_json)
        raw = _call(prompt)

        if raw:
            # JSON 파싱
            text = raw.strip()
            if text.startswith("```"):
                text = text.split("```")[1]
                if text.startswith("json"):
                    text = text[4:]
            text = text.strip()
            result = json.loads(text)
            body_ko = result.get("body_ko", "")
            body_en = result.get("body_en", "")
            recs    = result.get("recommendations", [])
            if body_ko:
                logger.info("[경영층알림] Groq LLM 초안 생성 완료")
                return body_ko, body_en, recs

    except Exception as e:
        logger.warning(f"[경영층알림] LLM 초안 생성 실패, 기본 템플릿 사용: {e}")

    # ── 폴백: 기본 템플릿
    date_str  = datetime.now().strftime("%Y년 %m월 %d일")
    companies = ", ".join(sorted(set(s.portfolio_name for s in reds)))
    lines = [
        f"안녕하세요.",
        f"",
        f"금일({date_str}) 포트폴리오 모니터링 시스템을 통해 즉시 검토가 필요한 이슈 {len(reds)}건이 확인되어 보고드립니다.",
        f"대상 포트폴리오사는 {companies}이며, 상세 내역은 아래 표를 참고해 주시기 바랍니다.",
        f"",
        f"빠른 검토 부탁드리며, 추가 협의가 필요하신 경우 사업개발팀으로 연락 주시기 바랍니다.",
    ]
    body_ko = "\n".join(lines)
    body_en = (
        f"This is an urgent portfolio monitoring alert for {companies}. "
        f"{len(reds)} issue(s) require your immediate attention. "
        f"Please refer to the table below for details."
    )
    recs = [
        "오늘 중 담당 심사역 확인 및 이슈 경위 파악",
        "필요 시 포트폴리오사 대표이사 직접 컨택",
        "상세 내용은 첨부 Daily 리포트 참고 후 투자위원회 보고 여부 결정",
    ]
    return body_ko, body_en, recs


# =============================================================================
# 배포 오케스트레이터
# =============================================================================

class Dispatcher:
    def __init__(self, config_path: str = "config.yaml"):
        self.cfg = load_config(config_path)
        self.email = EmailSender(self.cfg)

        bot_token = os.path.expandvars(
            self.cfg["dispatch"]["telegram"]["bot_token"])
        chat_id = os.path.expandvars(
            self.cfg["dispatch"]["telegram"]["chat_id"])

        self.telegram = TelegramSender(bot_token=bot_token, chat_id=chat_id)

        # 봇 명령어 핸들러 (SignalStore 공유)
        self.store           = SignalStore()
        self.cmd_handler     = TelegramCommandHandler(bot_token, chat_id, self.store)

    def poll_commands(self) -> None:
        """텔레그램 봇 명령어 폴링 — 스케줄러에서 30초마다 호출."""
        self.cmd_handler.poll()

    # ── 텔레그램 즉시 알림
    def send_telegram_alerts(self, signals: list[ClassifiedSignal]):
        if not self.cfg["dispatch"]["telegram"]["enabled"]:
            return
        self.store.update(signals)          # /status, /report 명령어용 캐시 갱신

        reds    = [s for s in signals if s.action_flag == "red"]
        yellows = [s for s in signals if s.action_flag == "yellow"]
        whites  = [s for s in signals if s.action_flag == "white"]
        date_str = datetime.now().strftime("%Y-%m-%d")

        # ── 기업별 묶음, 플래그 심각도 순 정렬
        from collections import defaultdict as _dd
        by_co = _dd(list)
        for s in signals:
            by_co[s.portfolio_name].append(s)

        lines = []
        for co, co_sigs in sorted(by_co.items(),
                key=lambda x: (0 if any(s.action_flag=="red" for s in x[1])
                                else 1 if any(s.action_flag=="yellow" for s in x[1]) else 2,
                                x[0])):
            top = sorted(co_sigs, key=lambda s: 0 if s.action_flag=="red" else 1 if s.action_flag=="yellow" else 2)[0]
            emoji = "🔴" if top.action_flag == "red" else "🟡" if top.action_flag == "yellow" else "⚪"
            summary = (top.summary_ko or top.title or "")[:50]
            url = (getattr(top, "url", "") or "").strip()
            _senti = {"Positive": "📈 긍정", "Negative": "📉 부정",
                      "Neutral": "➡️ 중립", "Mixed": "➡️ 중립"}.get(getattr(top, "sentiment", ""), "")
            _senti_part = f" {_senti}" if _senti else ""
            line = f"{emoji} <b>{co}</b> [{top.signal_type}]{_senti_part}\n   {summary}…"
            if url:
                line += f'\n   🔗 <a href="{url}">기사 원문</a>'
            lines.append(line)

        body = "\n\n".join(lines) if lines else "오늘 특이사항 없음"

        msg = (
            f"📊 <b>포트폴리오 모니터링 — {date_str}</b>\n"
            f"🔴 {len(reds)}건 · 🟡 {len(yellows)}건 · ⚪ {len(whites)}건\n\n"
            f"{body}"
        )
        try:
            self.telegram.send(msg)
            print(f"[Telegram] 일일 요약 발송 완료 ({len(by_co)}개사)", flush=True)
        except Exception as e:
            logger.warning(f"[Telegram] 일일 요약 발송 실패: {e}")

    def send_telegram_realtime(self, signals: list):
        """수시 알림: 모든 시그널 포함, 건수 헤더 없이 발송."""
        if not self.cfg["dispatch"]["telegram"]["enabled"]:
            return
        targets = [s for s in signals if s.action_flag in ("red", "yellow")]
        if not targets:
            print("[Telegram] 신규 시그널 없음 → 발송 생략", flush=True)
            return
        lines = []
        for s in targets:
            emoji = "🔴" if s.action_flag == "red" else "🟡"
            summary = (s.summary_ko or s.title or "")[:60]
            url = (getattr(s, "url", "") or "").strip()
            parts = [
                f"{emoji} <b>{s.portfolio_name}</b> [{s.signal_type}]",
                f"   {summary}",
            ]
            if url:
                parts.append(f'   🔗 <a href="{url}">원문 보기</a>')
            lines.append("\n".join(parts))
        msg = "\n\n".join(lines)
        try:
            self.telegram.send(msg)
            print(f"[Telegram] realtime {len(targets)}건 발송", flush=True)
        except Exception as e:
            logger.warning(f"[Telegram] realtime 발송 실패: {e}")

    # ── 텔레그램 "기사 없음" 알림
    def send_telegram_no_news(self):
        """오늘 수집된 기사가 없을 때 텔레그램으로 알림."""
        if not self.cfg["dispatch"]["telegram"]["enabled"]:
            return
        date_str = datetime.now().strftime("%Y-%m-%d")
        msg = (
            f"📭 <b>포트폴리오 모니터링 — {date_str}</b>\n\n"
            f"오늘 수집된 포트폴리오사 관련 기사가 없습니다."
        )
        try:
            self.telegram.send(msg)
            logger.info("[Telegram] 기사 없음 알림 발송 완료")
        except Exception as e:
            logger.warning(f"[Telegram] 기사 없음 알림 실패: {e}")

    # ── Daily 이메일 (HTML 대시보드 + PDF 첨부)
    def send_daily_email(self, signals: list[ClassifiedSignal],
                          weekly_signals: Optional[list] = None,
                          monthly_signals: Optional[list] = None):
        if not self.cfg["dispatch"]["email_daily"]["enabled"]:
            return
        signals  = deduplicate_signals(signals)
        dcfg     = self.cfg["dispatch"]["email_daily"]
        date_str = datetime.now().strftime("%Y-%m-%d")
        generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        reds    = [s for s in signals if s.action_flag == "red"]
        yellows = [s for s in signals if s.action_flag == "yellow"]
        whites  = [s for s in signals if s.action_flag == "white"]

        _weekly  = weekly_signals  if weekly_signals  else signals
        _monthly = monthly_signals if monthly_signals else signals

        subject = dcfg["subject_template"].format(
            date=date_str,
            red_count=len(reds),
            yellow_count=len(yellows),
        )

        # ── 브라우저 Daily 탭과 동일한 렌더러로 이메일 HTML 생성
        from datetime import timezone as _tz, timedelta as _td
        _KST = _tz(_td(hours=9))
        generated_at = datetime.now(_KST).strftime("%Y-%m-%d %H:%M KST")
        daily_content = _build_daily_overview_section(signals, generated_at)
        html = f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
{_HTML_STYLE.replace("{{", "{").replace("}}", "}")}
<style>
  body {{ background:#edf0f4 }}
  .wrapper {{ max-width:900px }}
  .topbar {{
    background:linear-gradient(150deg,#0d1b2a,#1a2744);
    border-radius:12px 12px 0 0;
    padding:22px 28px;
  }}
  .topbar h1 {{ color:#fff;font-size:20px;font-weight:700 }}
  .topbar .meta {{ color:rgba(255,255,255,.4);font-size:11px;margin-top:4px }}
  .tab-bar {{ display:flex;gap:5px;background:#edf0f4;padding:14px 20px 0;border-bottom:none; }}
  .tab-btn-active {{
    padding:13px 30px;font-size:13.5px;font-weight:700;
    color:#1a1a2e;background:#ffffff;border:1.5px solid #c8d0da;
    border-bottom:none;border-radius:10px 10px 0 0;
    box-shadow:0 -3px 10px rgba(0,0,0,.07);position:relative;letter-spacing:.1px;
  }}
  .tab-btn-active::before {{
    content:'';position:absolute;top:0;left:8px;right:8px;
    height:3px;background:#c0392b;border-radius:3px 3px 0 0;
  }}
  .tab-content-panel {{
    background:#fff;border:1.5px solid #c8d0da;border-top:none;
    border-radius:0 0 12px 12px;padding:20px;
  }}
  .upd {{ text-align:center;font-size:11px;color:#adb5bd;margin-top:24px;border-top:1px solid #e9ecef;padding-top:14px; }}
</style>
</head>
<body>
<div class="wrapper">

  <!-- 탑바 -->
  <div class="topbar">
    <h1>📊 Portfolio Intelligence Dashboard</h1>
    <div class="meta">CONFIDENTIAL &nbsp;·&nbsp; {generated_at} 기준</div>
  </div>

  <!-- 탭 바 (Daily 활성, 이메일이므로 정적) -->
  <div class="tab-bar">
    <div class="tab-btn-active">📅 Daily</div>
  </div>

  <!-- Daily 탭 콘텐츠 -->
  <div class="tab-content-panel">
    {daily_content}
  </div>

  <div class="upd">
    Portfolio Intelligence Agent &nbsp;·&nbsp; 사업개발팀 내부용<br>
    AI 자동 생성 리포트 — 최종 투자 판단은 반드시 직접 검토 후 결정하시기 바랍니다.
  </div>

</div>
</body>
</html>"""

        # dashboard.html 저장 (weekly/monthly 포함)
        save_dashboard(signals, weekly_signals=_weekly, monthly_signals=_monthly)

        self.email.send(dcfg["recipients"], subject, html, None)

    # ── 경영층 즉시 보고 (🔴 발생 시 Groq LLM으로 초안 작성 후 자동 발송)
    def send_executive_alert(self, signals: list[ClassifiedSignal]):
        """
        🔴 시그널 발생 시 경영층(CEO/임원)에게 보고용 이메일 자동 발송.
        Groq LLM이 경영층 수준의 보고 본문을 실제로 작성함.
        config.yaml의 dispatch.executive_alert 섹션 필요.
        """
        ecfg = self.cfg.get("dispatch", {}).get("executive_alert", {})
        if not ecfg.get("enabled", False):
            return
        reds = [s for s in signals if s.action_flag == "red"]
        if not reds:
            logger.info("[경영층알림] 🔴 항목 없음 — 발송 생략")
            return

        date_str     = datetime.now().strftime("%Y년 %m월 %d일")
        date_short   = datetime.now().strftime("%Y-%m-%d")
        companies    = ", ".join(sorted(set(s.portfolio_name for s in reds)))

        # ── Step 1: Groq LLM으로 경영층 보고 본문 생성
        body_ko, body_en, recommendations = _draft_executive_body(reds)

        # ── Step 2: 시그널 테이블 HTML
        items_html = ""
        for s in reds:
            items_html += f"""
            <tr style="border-bottom:1px solid #eee">
              <td style="padding:10px 12px;font-weight:700;white-space:nowrap;color:#c0392b">{s.portfolio_name}</td>
              <td style="padding:10px 12px;font-size:12px;color:#555">{s.signal_type}</td>
              <td style="padding:10px 12px;font-size:13px;line-height:1.55">{s.summary_ko}</td>
              <td style="padding:10px 12px;font-size:12px;color:#888;font-style:italic;line-height:1.5">{s.summary_en}</td>
            </tr>"""

        # ── Step 3: HTML 구성
        rec_items = "".join(
            f"<li style='margin:6px 0;font-size:13px;line-height:1.6'>{r}</li>"
            for r in recommendations
        ) if recommendations else "<li style='margin:6px 0'>담당 심사역 즉시 검토 및 팀장 보고</li>"

        html = f"""<!DOCTYPE html>
<html lang="ko">
<head><meta charset="UTF-8">
<style>
  body {{ font-family:'Malgun Gothic',-apple-system,'Segoe UI',sans-serif;color:#1a1a2e;
          max-width:700px;margin:0 auto;padding:28px 24px;background:#fff }}
  table {{ border-collapse:collapse;width:100% }}
</style>
</head>
<body>

  <!-- 긴급 헤더 -->
  <div style="background:linear-gradient(135deg,#a93226,#c0392b);color:white;
              padding:18px 22px;border-radius:10px;margin-bottom:22px">
    <div style="font-size:11px;opacity:0.8;letter-spacing:1.5px;
                text-transform:uppercase;margin-bottom:6px">🔴 즉시검토 — Immediate Review</div>
    <div style="font-size:19px;font-weight:700;letter-spacing:-0.3px">
      포트폴리오 긴급 이슈 보고
    </div>
    <div style="font-size:12px;margin-top:5px;opacity:0.85">
      {date_str} &nbsp;·&nbsp; 사업개발팀
    </div>
  </div>

  <!-- LLM 작성 보고 본문 -->
  <div style="font-size:14px;line-height:1.85;color:#2c3e50;margin-bottom:22px;
              border-left:3px solid #c0392b;padding-left:16px;white-space:pre-line">{body_ko}</div>

  <!-- 시그널 상세 테이블 -->
  <div style="font-size:11px;font-weight:700;color:#888;letter-spacing:1px;
              text-transform:uppercase;margin-bottom:8px">이슈 상세 내역</div>
  <table style="border:1px solid #e9ecef;border-radius:8px;overflow:hidden;
                font-size:13px;margin-bottom:20px">
    <tr style="background:#1a1a2e;color:white">
      <th style="padding:10px 12px;text-align:left;white-space:nowrap">포트폴리오사</th>
      <th style="padding:10px 12px;text-align:left">신호 유형</th>
      <th style="padding:10px 12px;text-align:left">주요 내용 (KO)</th>
      <th style="padding:10px 12px;text-align:left">Summary (EN)</th>
    </tr>
    {items_html}
  </table>

  <!-- LLM 권고사항 -->
  <div style="background:#fff8e1;border-left:4px solid #f39c12;padding:14px 18px;
              border-radius:0 8px 8px 0;margin-bottom:22px">
    <div style="font-size:12px;font-weight:700;color:#e67e22;margin-bottom:8px">
      ⚡ 권고 액션 | Recommended Actions
    </div>
    <ul style="padding-left:18px;margin:0;color:#2c3e50">
      {rec_items}
    </ul>
  </div>

  <!-- 영문 요약 (해외 파트너 공유용) -->
  <div style="background:#f0f4ff;border-radius:8px;padding:14px 18px;margin-bottom:22px;
              border:1px solid #dce7ff">
    <div style="font-size:11px;font-weight:700;color:#3498db;margin-bottom:6px;
                text-transform:uppercase;letter-spacing:0.5px">English Summary (for overseas partners)</div>
    <div style="font-size:13px;color:#34495e;line-height:1.7;font-style:italic">{body_en}</div>
  </div>

  <!-- 면책 + 메타 -->
  <div style="font-size:11px;color:#adb5bd;border-top:1px solid #f1f3f5;
              padding-top:14px;line-height:1.8">
    본 메일은 Portfolio Intelligence Agent가 Groq AI를 활용해 자동 작성한 보고 초안입니다.<br>
    최종 투자 판단은 반드시 직접 검토 후 결정하시기 바랍니다.<br>
    <span style="color:#dee2e6">This alert is AI-generated (Groq/Llama-3.3-70b).
    All investment decisions require independent validation.</span>
  </div>

</body></html>"""

        # ── Step 4: IR 초안 생성 (M&A·Exit / 펀딩 / IPO 해당 시)
        ir_section = ""
        for s in reds:
            ir_text = _draft_ir_memo(s)
            if ir_text:
                ir_section += f"""
  <div style="background:#f8f9fa;border-radius:10px;padding:16px 18px;
              margin-bottom:14px;border-left:4px solid #8e44ad">
    <div style="font-size:11px;font-weight:700;color:#8e44ad;margin-bottom:4px;
                text-transform:uppercase;letter-spacing:0.5px">
      📄 IR 커뮤니케이션 초안 — {s.portfolio_name} [{s.signal_type}]
    </div>
    <div style="font-size:13px;color:#2c3e50;line-height:1.85;
                white-space:pre-line;margin-top:6px">{ir_text}</div>
    <div style="font-size:10px;color:#adb5bd;margin-top:8px">
      ※ AI 생성 초안입니다. 발송 전 반드시 검토·수정 후 사용하시기 바랍니다.
    </div>
  </div>"""

        if ir_section:
            ir_block = f"""
  <!-- IR 초안 섹션 -->
  <div style="font-size:11px;font-weight:700;color:#888;letter-spacing:1px;
              text-transform:uppercase;margin:20px 0 8px">
    📄 LP/투자자 IR 커뮤니케이션 초안 | IR Draft
  </div>
  {ir_section}"""
            # html 의 면책 문구 바로 앞에 삽입
            html = html.replace("  <!-- 면책 + 메타 -->", ir_block + "\n  <!-- 면책 + 메타 -->")

        subject = (
            f"[긴급] 포트폴리오 즉시검토 {len(reds)}건 — "
            f"{companies} ({date_short})"
        )
        recipients = ecfg.get("recipients", [])
        if recipients:
            self.email.send(recipients, subject, html)
            logger.info(f"[경영층알림] 🔴 {len(reds)}건 → {recipients} 발송 완료")
        else:
            logger.warning("[경영층알림] recipients 설정 없음 — config.yaml 확인 필요")

    # ── Weekly 이메일
    def send_weekly_email(self, signals: list[ClassifiedSignal]):
        if not self.cfg["dispatch"]["email_weekly"]["enabled"]:
            return
        wcfg = self.cfg["dispatch"]["email_weekly"]
        now  = datetime.now()
        week_range = f"{now.strftime('%Y-%m-%d')} 주간"
        subject = wcfg["subject_template"].format(week_range=week_range)
        # ── Claude Sonnet으로 주간 인사이트 생성
        logger.info("[Weekly] Claude Sonnet 주간 인사이트 생성 중...")
        ai_insight = _generate_weekly_insight(signals)
        html = build_weekly_html(signals, week_range, ai_insight=ai_insight)
        self.email.send(wcfg["recipients"], subject, html)

    # ── Monthly 이메일
    def send_monthly_email(self, signals: list[ClassifiedSignal]):
        if not self.cfg["dispatch"]["email_monthly"]["enabled"]:
            return
        mcfg = self.cfg["dispatch"]["email_monthly"]
        now  = datetime.now()
        # ── Claude Sonnet으로 월간 심층 분석 생성
        logger.info("[Monthly] Claude Sonnet 월간 심층 분석 생성 중...")
        ai_insight = _monthly_insight_as_text(
            _generate_monthly_insight(signals, now.year, now.month))
        html = build_monthly_html(signals, now.year, now.month,
                                  ai_insight=ai_insight)
        subject = mcfg["subject_template"].format(
            year=now.year, month=now.month)
        self.email.send(mcfg["recipients"], subject, html)


# =============================================================================
# 텔레그램 봇 명령어 핸들러 (/status, /report, /help)
# =============================================================================

class TelegramCommandHandler:
    """
    텔레그램 봇 long-polling 방식으로 사용자 명령어를 수신·처리.

    지원 명령어:
      /status   — 최근 시그널 현황 요약 즉시 전송
      /report   — 최근 수집 결과 간략 보고서 전송
      /help     — 사용 가능한 명령어 목록 표시

    사용법:
      handler = TelegramCommandHandler(bot_token, chat_id, signal_store)
      handler.poll()    ← 스케줄러 루프에서 주기적으로 호출 (30초마다)
    """
    UPDATES_URL  = "https://api.telegram.org/bot{token}/getUpdates"
    MESSAGE_URL  = "https://api.telegram.org/bot{token}/sendMessage"

    def __init__(self, bot_token: str, chat_id: str,
                 signal_store: "SignalStore"):
        self.token        = bot_token
        self.chat_id      = str(chat_id)
        self.store        = signal_store
        self._offset: int = 0

    def _get_updates(self) -> list[dict]:
        try:
            resp = requests.get(
                self.UPDATES_URL.format(token=self.token),
                params={"offset": self._offset, "timeout": 5},
                timeout=10,
            )
            resp.raise_for_status()
            return resp.json().get("result", [])
        except Exception as e:
            logger.debug(f"[TgCmd] getUpdates 오류: {e}")
            return []

    def _send(self, text: str, parse_mode: str = "HTML") -> None:
        try:
            requests.post(
                self.MESSAGE_URL.format(token=self.token),
                json={"chat_id": self.chat_id, "text": text,
                      "parse_mode": parse_mode,
                      "disable_web_page_preview": True},
                timeout=10,
            )
        except Exception as e:
            logger.warning(f"[TgCmd] 메시지 전송 실패: {e}")

    def poll(self) -> None:
        """새 업데이트 확인 및 명령어 처리 (스케줄러에서 30초마다 호출)."""
        updates = self._get_updates()
        for upd in updates:
            self._offset = upd["update_id"] + 1
            msg = upd.get("message") or upd.get("channel_post")
            if not msg:
                continue
            text = msg.get("text", "").strip()
            # 허용된 채팅에서만 응답
            if str(msg.get("chat", {}).get("id", "")) != self.chat_id:
                continue
            if text.startswith("/status"):
                self._handle_status()
            elif text.startswith("/report"):
                self._handle_report()
            elif text.startswith("/help"):
                self._handle_help()

    # ── /help
    def _handle_help(self) -> None:
        self._send(
            "📊 <b>Portfolio Intelligence Bot</b>\n"
            "──────────────────────────\n"
            "/status  — 최근 시그널 현황 요약\n"
            "/report  — 회사별 상세 보고\n"
            "/help    — 이 도움말\n\n"
            "<i>🔴 긴급 이슈는 자동으로 Push 됩니다.</i>"
        )

    # ── /status
    def _handle_status(self) -> None:
        signals = self.store.get_recent()
        if not signals:
            self._send("📭 최근 24시간 내 수집된 시그널이 없습니다.")
            return

        reds    = [s for s in signals if s.action_flag == "red"]
        yellows = [s for s in signals if s.action_flag == "yellow"]
        whites  = [s for s in signals if s.action_flag == "white"]
        companies = len(set(s.portfolio_name for s in signals))
        updated_at = datetime.now().strftime("%m/%d %H:%M")

        lines = [
            f"📊 <b>포트폴리오 현황</b> ({updated_at} 기준)",
            "──────────────────────────",
            f"🔴 즉시검토: <b>{len(reds)}건</b>",
            f"🟡 모니터링: <b>{len(yellows)}건</b>",
            f"⚪ 참고:    <b>{len(whites)}건</b>",
            f"🏢 모니터링 기업: {companies}개사",
        ]

        if reds:
            lines.append("\n🚨 <b>즉시검토 이슈:</b>")
            for s in reds[:3]:
                lines.append(f"  • <b>{s.portfolio_name}</b> [{s.signal_type}]")
                lines.append(f"    {s.summary_ko[:60]}…")

        lines.append("\n<i>/report 로 상세 내용 조회</i>")
        self._send("\n".join(lines))

    # ── /report
    def _handle_report(self) -> None:
        signals = self.store.get_recent()
        if not signals:
            self._send("📭 최근 24시간 내 수집된 시그널이 없습니다.")
            return

        from collections import defaultdict
        by_co: dict[str, list] = defaultdict(list)
        for s in signals:
            by_co[s.portfolio_name].append(s)

        updated_at = datetime.now().strftime("%m/%d %H:%M")
        lines = [
            f"📋 <b>포트폴리오 상세 리포트</b> ({updated_at})",
            "──────────────────────────",
        ]

        for co, arts in sorted(by_co.items()):
            r = sum(1 for a in arts if a.action_flag == "red")
            y = sum(1 for a in arts if a.action_flag == "yellow")
            top = sorted(arts, key=lambda x: ({"red":0,"yellow":1,"white":2}[x.action_flag],
                                               x.source_tier))[0]
            emoji = "🔴" if r else "🟡" if y else "⚪"
            lines.append(f"\n{emoji} <b>{co}</b>  [🔴{r} 🟡{y}]")
            lines.append(f"  └ {top.signal_type}: {top.summary_ko[:70]}…")

        lines.append("\n<i>상세 리포트는 이메일을 확인하세요.</i>")
        self._send("\n".join(lines))


# =============================================================================
# 시그널 저장소 (봇 명령어용 — 최근 24시간)
# =============================================================================

class SignalStore:
    """
    최근 수집된 시그널을 메모리에 보관.
    TelegramCommandHandler가 /status, /report 응답에 사용.
    """
    def __init__(self):
        self._signals: list[ClassifiedSignal] = []
        self._updated_at: Optional[datetime]  = None

    def update(self, signals: list[ClassifiedSignal]) -> None:
        self._signals    = list(signals)
        self._updated_at = datetime.now(timezone.utc)
        logger.debug(f"[SignalStore] 업데이트 완료: {len(signals)}건")

    def get_recent(self) -> list[ClassifiedSignal]:
        """저장된 시그널 반환 (24시간 이내)."""
        if not self._updated_at:
            return []
        age_h = (datetime.now(timezone.utc) - self._updated_at).total_seconds() / 3600
        if age_h > 24:
            return []
        return self._signals


# =============================================================================
# 정적 HTML 대시보드 생성
# =============================================================================

def build_dashboard_html(signals: list[ClassifiedSignal], generated_at: str,
                         drafts_data: Optional[list[dict]] = None) -> str:
    """탭 구조 대시보드 — Tab1: 이메일과 동일 구조(Executive Summary + 시그널 카드) / Tab2: 커뮤니케이션 초안"""
    by_company: dict[str, list[ClassifiedSignal]] = defaultdict(list)
    for s in signals:
        by_company[s.portfolio_name].append(s)

    reds    = [s for s in signals if s.action_flag == "red"]
    yellows = [s for s in signals if s.action_flag == "yellow"]
    whites  = [s for s in signals if s.action_flag == "white"]
    total   = len(signals)
    health  = max(0, 100 - len(reds)*20 - len(yellows)*5)
    hcolor  = "#27ae60" if health >= 80 else "#f39c12" if health >= 60 else "#e74c3c"
    # 전체 포트폴리오 수 (portfolio.yaml 기준)
    try:
        from collector import load_portfolios
        total_portfolio = len(load_portfolios("portfolio.yaml"))
    except Exception:
        total_portfolio = 14

    # ── Executive Summary (이메일과 동일 로직, 줄바꿈+마크 형식)
    if reds:
        red_lines   = [f"🔴 {s.portfolio_name} — {_exec_text(s)}" for s in reds[:3]]
        yellow_lines= [f"🟡 {s.portfolio_name} — {_exec_text(s)}" for s in yellows[:2]]
        exec_msg  = "\n".join(red_lines + yellow_lines)
        red_en    = [f"· {s.portfolio_name}: {_exec_text_en(s)}" for s in reds[:3]]
        exec_en   = "\n".join(red_en)
        exec_bg   = "#fdf5f5"; exec_border = "#e74c3c"; exec_label_color = "#c0392b"
    elif yellows:
        yellow_lines= [f"🟡 {s.portfolio_name} — {_exec_text(s)}" for s in yellows[:4]]
        exec_msg  = "\n".join(yellow_lines)
        exec_en   = "\n".join([f"· {s.portfolio_name}: watch & report" for s in yellows[:4]])
        exec_bg   = "#fdfbf0"; exec_border = "#f39c12"; exec_label_color = "#d68910"
    else:
        exec_msg  = "⚪ 오늘 포트폴리오 전반 특이사항 없음 — 정기모니터링 유지"
        exec_en   = "· No significant signals today. Routine monitoring continues."
        exec_bg   = "#f0fdf4"; exec_border = "#27ae60"; exec_label_color = "#27ae60"

    # ── 시그널 카드 — 플래그 섹션 헤더 + 회사별 묶음
    _DISPLAY_PRIORITY = {"업스테이지": 1, "비엠스마일": 2, "컬리": 3, "에버온": 4}
    def _flag_section_header(flag: str, count: int) -> str:
        cfg = {
            "red":    ("red",    "🔴 즉시검토", "Immediate Review"),
            "yellow": ("yellow", "🟡 동향주시", "Watch & Report"),
            "white":  ("white",  "⚪ 정기모니터링", "Routine Track"),
        }.get(flag, ("white", "⚪", ""))
        cls, label, sub = cfg
        return (
            f'<div class="flag-banner {cls}">'
            f'<div><span class="fb-label">{label}</span>'
            f'<span class="fb-sub">{sub}</span></div>'
            f'<span class="fb-pill">{count}건</span></div>'
        )

    red_co: dict[str, list] = defaultdict(list)
    yellow_co: dict[str, list] = defaultdict(list)
    for s in signals:
        if s.action_flag == "red":
            red_co[s.portfolio_name].append(s)
        elif s.action_flag == "yellow":
            yellow_co[s.portfolio_name].append(s)

    def _sort_co(d):
        return sorted(d.items(), key=lambda kv: (_DISPLAY_PRIORITY.get(kv[0], 99), kv[0]))

    cards_parts = []
    if red_co:
        cards_parts.append(_flag_section_header("red", len(reds)))
        for co, sigs in _sort_co(red_co):
            cards_parts.append(_company_section_html(co, sorted(sigs, key=lambda x: 0 if x.action_flag=="red" else 1)))
    if yellow_co:
        cards_parts.append(_flag_section_header("yellow", len(yellows)))
        for co, sigs in _sort_co(yellow_co):
            cards_parts.append(_company_section_html(co, sigs))
    cards = "\n".join(cards_parts)
    if not cards:
        cards = '<div style="text-align:center;padding:24px;color:#adb5bd;font-size:13px">오늘 검토 항목 없음 · No items today</div>'

    white_note = (
        f'<div class="white-note">⚪ 정기모니터링 {len(whites)}건</div>'
    ) if whites else ""

    # ── Tab2: 커뮤니케이션 초안
    draft_tab_content = ""
    if drafts_data:
        draft_rows = ""
        for i, d in enumerate(drafts_data):
            co   = d.get("portfolio_name", "")
            sig  = d.get("signal_type", "")
            summ = _finish_summary(d.get("summary_ko") or "", 55)
            me   = json.dumps((d.get("msg_exec") or ""), ensure_ascii=False)
            mp   = json.dumps((d.get("msg_portfolio") or ""), ensure_ascii=False)
            me_html = (d.get("msg_exec") or "").replace("\n", "<br>")
            mp_html = (d.get("msg_portfolio") or "").replace("\n", "<br>")
            draft_rows += f"""
              <tr class="sig-row" onclick="toggleDraft({i})" id="drow-{i}">
                <td style="padding:18px 14px 18px 22px">
                  <span style="font-size:14px;font-weight:700;color:#1a1a2e">{co}</span>
                </td>
                <td style="padding:18px 14px">
                  <span style="background:#ede7f6;color:#6c3483;font-size:12px;font-weight:600;
                             padding:5px 13px;border-radius:20px">{sig}</span>
                </td>
                <td style="padding:18px 14px;font-size:13px;color:#495057">{summ}</td>
                <td style="text-align:center;color:#8e44ad;font-size:17px;width:44px;padding:18px 14px">
                  <span id="darr-{i}">▶</span>
                </td>
              </tr>
              <tr id="dpanel-{i}" style="display:none">
                <td colspan="4" style="padding:0;background:#faf8ff;border-bottom:1px solid #e9ecef">
                  <div style="padding:14px 20px;display:flex;flex-direction:column;gap:12px">
                    <div style="border:1.5px solid #e67e22;border-radius:8px;overflow:hidden;position:relative">
                      <div style="background:#fff8f0;padding:8px 14px;font-size:10px;font-weight:700;
                                  color:#e67e22;letter-spacing:.8px;text-transform:uppercase">
                        📤 경영층 문자 초안 (카카오톡/SMS)
                      </div>
                      <div style="padding:12px 14px;font-size:13px;line-height:1.8;
                                  color:#2c3e50;background:#fff">{me_html}</div>
                      <button onclick="copyDraft({me},this)"
                        style="position:absolute;top:6px;right:10px;background:#fff;
                               border:1px solid #dee2e6;border-radius:6px;padding:4px 14px;
                               font-size:11px;font-weight:600;color:#555;cursor:pointer">복사</button>
                    </div>
                    <div style="border:1.5px solid #2980b9;border-radius:8px;overflow:hidden;position:relative">
                      <div style="background:#f0f8ff;padding:8px 14px;font-size:10px;font-weight:700;
                                  color:#2980b9;letter-spacing:.8px;text-transform:uppercase">
                        💼 포트폴리오사 문의 초안
                      </div>
                      <div style="padding:12px 14px;font-size:13px;line-height:1.8;
                                  color:#2c3e50;background:#fff">{mp_html}</div>
                      <button onclick="copyDraft({mp},this)"
                        style="position:absolute;top:6px;right:10px;background:#fff;
                               border:1px solid #dee2e6;border-radius:6px;padding:4px 14px;
                               font-size:11px;font-weight:600;color:#555;cursor:pointer">복사</button>
                    </div>
                    <div style="font-size:10px;color:#adb5bd;text-align:right">
                      ※ AI 생성 초안 — 발송 전 반드시 검토·수정 후 사용하세요.
                    </div>
                  </div>
                </td>
              </tr>"""

        draft_tab_content = f"""
        <div style="background:#fff3cd;border-left:4px solid #f39c12;border-radius:0 8px 8px 0;
                    padding:10px 16px;margin-bottom:16px;font-size:13px;color:#7d4e00">
          💡 <b>사용 방법:</b> 각 행을 클릭하면 초안이 펼쳐집니다.
          <b>복사</b> 버튼으로 카카오톡·이메일에 바로 붙여넣기 하세요.
        </div>
        <table style="width:100%;border-collapse:collapse;background:#fff;border-radius:12px;
                      overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,.06)">
          <thead>
            <tr style="background:#1a1a2e">
              <th style="padding:14px 16px;text-align:left;font-size:12px;color:rgba(255,255,255,.7);
                         font-weight:600;letter-spacing:.5px">포트폴리오사</th>
              <th style="padding:14px 16px;text-align:left;font-size:12px;color:rgba(255,255,255,.7);
                         font-weight:600;letter-spacing:.5px">신호 유형</th>
              <th style="padding:14px 16px;text-align:left;font-size:12px;color:rgba(255,255,255,.7);
                         font-weight:600;letter-spacing:.5px">주요 내용</th>
              <th style="width:40px"></th>
            </tr>
          </thead>
          <tbody>{draft_rows}</tbody>
        </table>"""
    else:
        draft_tab_content = """
        <div style="text-align:center;padding:56px;color:#adb5bd;font-size:14px">
          🔴 즉시검토 시그널 없음 — 커뮤니케이션 초안이 생성되지 않았습니다.
        </div>"""

    n_drafts = len([d for d in (drafts_data or [])
                    if (d.get("msg_exec") or "").strip() or (d.get("msg_portfolio") or "").strip()])
    drafts_badge = (f'<span id="drafts-badge" style="background:#c0392b;color:#fff;font-size:11px;'
                    f'border-radius:20px;padding:2px 10px;margin-left:8px;vertical-align:middle">'
                    f'{n_drafts}건</span>') if n_drafts > 0 else ""

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Portfolio Intelligence Dashboard</title>
{_HTML_STYLE.replace("{{", "{").replace("}}", "}")}
<style>
  /* ── 대시보드 전용 오버라이드 */
  body {{ background:#edf0f4 }}
  .wrapper {{ max-width:900px }}

  /* ── 탑바 */
  .topbar {{
    background:linear-gradient(150deg,#0d1b2a,#1a2744);
    border-radius:12px 12px 0 0;
    padding:22px 28px;
  }}
  .topbar h1 {{ color:#fff;font-size:20px;font-weight:700 }}
  .topbar .meta {{ color:rgba(255,255,255,.4);font-size:11px;margin-top:4px }}
  .hbadge {{
    background:rgba(255,255,255,.1);border-radius:10px;
    padding:12px 20px;text-align:center;min-width:90px
  }}
  .hbadge .n {{ font-size:28px;font-weight:800;color:{hcolor} }}
  .hbadge .l {{ font-size:9px;color:rgba(255,255,255,.45);text-transform:uppercase;
                letter-spacing:1px;margin-top:2px }}

  /* ── 탭 바 — 브라우저 카드 탭 스타일 */
  .tab-bar {{
    display:flex; gap:5px;
    background:#edf0f4;
    padding:14px 20px 0;
    border-bottom:none;
  }}
  .tab-btn {{
    padding:13px 30px;
    font-size:13.5px; font-weight:600;
    cursor:pointer;
    color:#6c757d;
    border:1.5px solid #d1d8e0;
    border-bottom:none;
    background:#dde3ec;
    border-radius:10px 10px 0 0;
    transition:all .18s;
    letter-spacing:.1px;
    position:relative;
  }}
  .tab-btn:hover {{ color:#343a40; background:#eef1f5; }}
  .tab-btn.active {{
    color:#1a1a2e;
    background:#ffffff;
    border-color:#c8d0da;
    font-weight:700;
    box-shadow:0 -3px 10px rgba(0,0,0,.07);
  }}
  .tab-btn.active::before {{
    content:'';
    position:absolute;
    top:0; left:8px; right:8px;
    height:3px;
    background:#c0392b;
    border-radius:3px 3px 0 0;
  }}

  /* ── 탭 콘텐츠 */
  .tab-content {{ display:none; padding-top:20px }}
  .tab-content.active {{ display:block }}

  /* ── 복사 버튼 */
  .copy-ok {{ background:#27ae60!important;color:#fff!important;border-color:#27ae60!important }}

  /* ── 하단 업데이트 표시 */
  .upd {{
    text-align:center; font-size:11px; color:#adb5bd;
    margin-top:24px; border-top:1px solid #e9ecef; padding-top:14px;
  }}
</style>
</head>
<body>
<div class="wrapper">

  <!-- ── 탑바 -->
  <div class="topbar">
    <div>
      <h1>📊 Portfolio Intelligence Dashboard</h1>
      <div class="meta">CONFIDENTIAL · {generated_at} 기준</div>
    </div>
  </div>

  <!-- ── 탭 바 (헤더 아래, 흰 배경으로 명확한 대비) -->
  <div class="tab-bar">
    <button class="tab-btn active" onclick="showTab('overview',this)">
      📅 Daily
    </button>
    <button class="tab-btn" onclick="showTab('drafts',this)">
      📱 Comm. Draft{drafts_badge}
    </button>
  </div>

  <!-- ═══ Tab1: 포트폴리오 현황 (이메일과 동일 구조) ═══ -->
  <div id="tab-overview" class="tab-content active">

    <!-- 지표 카드: 3개 플래그 -->
    <table width="100%" cellspacing="0" cellpadding="0"
           style="margin:14px 0 10px;border-collapse:separate;border-spacing:8px 0">
      <tr>
        <td style="background:#fff;border-radius:10px;padding:16px 8px 14px;
            text-align:center;border-top:3px solid #e74c3c;
            box-shadow:0 2px 8px rgba(0,0,0,.06)">
          <div style="font-size:34px;font-weight:800;line-height:1;color:#c0392b">{len(reds)}</div>
          <div style="font-size:10.5px;color:#adb5bd;margin-top:5px;line-height:1.5;font-weight:500">🔴 즉시검토<br><span style="font-size:9px;color:#e0aaaa">Immediate Review</span></div>
        </td>
        <td style="background:#fff;border-radius:10px;padding:16px 8px 14px;
            text-align:center;border-top:3px solid #f39c12;
            box-shadow:0 2px 8px rgba(0,0,0,.06)">
          <div style="font-size:34px;font-weight:800;line-height:1;color:#d68910">{len(yellows)}</div>
          <div style="font-size:10.5px;color:#adb5bd;margin-top:5px;line-height:1.5;font-weight:500">🟡 동향주시<br><span style="font-size:9px;color:#d4b96a">Watch & Report</span></div>
        </td>
        <td style="background:#fff;border-radius:10px;padding:16px 8px 14px;
            text-align:center;border-top:3px solid #adb5bd;
            box-shadow:0 2px 8px rgba(0,0,0,.06)">
          <div style="font-size:34px;font-weight:800;line-height:1;color:#868e96">{len(whites)}</div>
          <div style="font-size:10.5px;color:#adb5bd;margin-top:5px;line-height:1.5;font-weight:500">⚪ 정기모니터링<br><span style="font-size:9px">PDF 첨부 참조</span></div>
        </td>
      </tr>
    </table>

    <!-- 모니터링 기업 — 별도 라인 -->
    <div style="background:#f0f4f8;border-radius:10px;padding:10px 16px;margin-bottom:16px;
                display:flex;align-items:center;justify-content:space-between;
                border:1px solid #dde3ea">
      <div style="font-size:11px;color:#6c757d;font-weight:600;letter-spacing:.3px">
        📡 오늘 시그널 감지 기업
      </div>
      <div style="display:flex;align-items:baseline;gap:4px">
        <span style="font-size:22px;font-weight:800;color:#1a1a2e;line-height:1">{len(by_company)}</span>
        <span style="font-size:12px;color:#adb5bd;font-weight:500">/ {total_portfolio}개사</span>
      </div>
    </div>

    <!-- Executive Summary -->
    <div class="exec-box" style="background:{exec_bg};border-color:{exec_border};">
      <span class="e-label" style="color:{exec_label_color};">Executive Summary</span>
      <div class="e-main" style="color:#1a1a2e;line-height:2;white-space:pre-line">{exec_msg}</div>
      <div class="e-sub" style="color:#868e96;line-height:1.9;white-space:pre-line">{exec_en}</div>
    </div>

    <!-- 모니터링 포인트 & 권고 액션 -->
    <div class="section-header">
      <span>📋 모니터링 포인트 &amp; 권고 액션 &nbsp;|&nbsp; Monitoring &amp; Actions</span>
    </div>
    {cards}
    {white_note}

  </div>

  <!-- ═══ Tab2: 커뮤니케이션 초안 ═══ -->
  <div id="tab-drafts" class="tab-content">
    {draft_tab_content}
  </div>

  <div class="upd">
    마지막 업데이트: {generated_at} &nbsp;·&nbsp; Portfolio Intelligence Agent &nbsp;·&nbsp;
    본 대시보드는 AI 자동 생성 결과이며 투자 판단 시 직접 검토가 필요합니다.
  </div>
</div>

<script>
  function showTab(id, btn) {{
    document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    document.getElementById('tab-' + id).classList.add('active');
    btn.classList.add('active');
  }}
  function toggleDraft(i) {{
    const p = document.getElementById('dpanel-' + i);
    const a = document.getElementById('darr-' + i);
    const r = document.getElementById('drow-' + i);
    const open = p.style.display !== 'none';
    p.style.display    = open ? 'none' : 'table-row';
    a.textContent      = open ? '▶' : '▼';
    r.style.background = open ? '' : '#f5f0ff';
  }}
  function copyDraft(text, btn) {{
    navigator.clipboard.writeText(text).then(() => {{
      btn.textContent = '✓ 복사됨';
      btn.classList.add('copy-ok');
      setTimeout(() => {{ btn.textContent = '복사'; btn.classList.remove('copy-ok'); }}, 2000);
    }});
  }}
</script>
</body></html>"""


def _inject_section(html: str, start_marker: str, end_marker: str, new_content: str) -> str:
    """마커 사이 콘텐츠 교체 헬퍼."""
    if start_marker in html and end_marker in html:
        before = html[:html.index(start_marker) + len(start_marker)]
        after  = html[html.index(end_marker):]
        return before + "\n" + new_content + "\n  " + after
    return html


def save_dashboard(signals: list[ClassifiedSignal],
                   drafts_data: Optional[list[dict]] = None,
                   weekly_signals: Optional[list] = None,
                   monthly_signals: Optional[list] = None,
                   path: str = "dashboard.html") -> str:
    """
    기존 dashboard.html 템플릿을 유지하면서
    각 AUTO 마커 사이 콘텐츠를 교체.
    weekly_signals: 과거 7일치 시그널 (없으면 오늘 signals 사용)
    monthly_signals: 과거 30일치 시그널 (없으면 오늘 signals 사용)
    """
    import re
    from datetime import timezone as _tz, timedelta as _td
    _KST = _tz(_td(hours=9))
    generated_at = datetime.now(_KST).strftime("%Y-%m-%d %H:%M KST")

    try:
        with open(path, "r", encoding="utf-8") as f:
            html = f.read()
    except FileNotFoundError:
        html = ""

    if "<!-- DAILY-AUTO-START -->" not in html:
        logger.warning("[Dashboard] 마커 없음 — 전체 재생성")
        html = build_dashboard_html(signals, generated_at, drafts_data=drafts_data)
        with open(path, "w", encoding="utf-8") as f:
            f.write(html)
        return path

    # ── 1. Daily 섹션
    html = _inject_section(html,
        "<!-- DAILY-AUTO-START -->", "<!-- DAILY-AUTO-END -->",
        _build_daily_overview_section(signals, generated_at))

    # ── 2. Weekly 섹션 — 매 실행마다 재생성 (히트맵 모달 최신 반영)
    from signal_db import SignalDB as _SDB
    _w_start, _w_end = _SDB.weekly_range()
    _weekly = weekly_signals if weekly_signals else signals
    if "<!-- WEEKLY-AUTO-START -->" in html:
        html = _inject_section(html,
            "<!-- WEEKLY-AUTO-START -->", "<!-- WEEKLY-AUTO-END -->",
            _build_weekly_section(_weekly, generated_at))

    # ── 3. Monthly 섹션 — 전월(1일~말일) 기준, 동일 월이면 재생성 생략 (월 1회 갱신)
    _m_start, _m_end = _SDB.monthly_range()
    _m_token = f"<!-- MONTHLY-PERIOD:{_m_start.strftime('%Y-%m')} -->"
    _monthly = monthly_signals if monthly_signals else signals
    if "<!-- MONTHLY-AUTO-START -->" in html:
        if _m_token in html:
            logger.info(f"[Dashboard] Monthly 동일 기간 — 갱신 생략 {_m_token}")
        else:
            html = _inject_section(html,
                "<!-- MONTHLY-AUTO-START -->", "<!-- MONTHLY-AUTO-END -->",
                _m_token + _build_monthly_section(_monthly, generated_at))

    # ── 4. 커뮤니케이션 초안 섹션
    if "<!-- DRAFTS-AUTO-START -->" in html:
        html = _inject_section(html,
            "<!-- DRAFTS-AUTO-START -->", "<!-- DRAFTS-AUTO-END -->",
            _build_drafts_section(signals))

    # ── 날짜 메타 업데이트
    html = re.sub(r'<!-- META-DATE -->.*?<!-- /META-DATE -->',
                  f'<!-- META-DATE -->{generated_at}<!-- /META-DATE -->', html)

    # ── 커뮤니케이션 초안 배지 숫자 동적 업데이트 (탭 버튼 영역)
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    logger.info(f"[Dashboard] 업데이트 완료 → {path} ({generated_at})")
    return path


def _build_weekly_modal(cells: list) -> str:
    """히트맵 셀 클릭 시 표시되는 기사 상세 모달 HTML+JS."""
    import json as _json_wm
    cells_json = _json_wm.dumps(
        {str(i): c for i, c in enumerate(cells)},
        ensure_ascii=False
    ).replace("</", "<\/")
    return (
        "<div id='wk-overlay' onclick='wkClose(event)' "
        "style='display:none;position:fixed;inset:0;background:rgba(15,23,42,.55);"
        "z-index:9999;align-items:center;justify-content:center'>"
        "<div id='wk-modal' style='background:#fff;border-radius:16px;width:540px;"
        "max-width:94vw;max-height:80vh;display:flex;flex-direction:column;"
        "box-shadow:0 24px 60px rgba(15,23,42,.22)'>"
        "<div style='padding:18px 20px 14px;border-bottom:1px solid #f1f5f9;position:relative'>"
        "<div style='font-size:15px;font-weight:900;color:#0f172a;margin-bottom:4px' id='wk-title'></div>"
        "<div style='font-size:12px;color:#64748b' id='wk-sub'></div>"
        "<button onclick=\"document.getElementById('wk-overlay').style.display='none'\" "
        "style='position:absolute;right:16px;top:50%;transform:translateY(-50%);"
        "width:28px;height:28px;border-radius:50%;background:#f1f5f9;border:none;"
        "cursor:pointer;font-size:15px;color:#64748b;line-height:1'>✕</button>"
        "</div>"
        "<div id='wk-body' style='overflow-y:auto;padding:14px 20px 18px;flex:1'></div>"
        "</div></div>"
        "<script>(function(){"
        f"var WK={cells_json};"
        "var FL={red:{label:'즉시검토',bg:'#fde8e8',color:'#A32D2D'},"
        "yellow:{label:'동향주시',bg:'#fef3cd',color:'#854F0B'},"
        "white:{label:'정기모니터링',bg:'#dcfce7',color:'#166534'}};"
        "function esc(s){return(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;')"
        ".replace(/>/g,'&gt;').replace(/\"/g,'&quot;');}"
        "window.wkModal=function(el){"
        "var idx=el.getAttribute('data-wk');var d=WK[idx];if(!d)return;"
        "document.getElementById('wk-title').textContent=d.co+' · '+d.day;"
        "document.getElementById('wk-sub').textContent='총 '+d.articles.length+'건 · 제목 또는 원문 보기 클릭으로 이동';"
        "var html='';d.articles.forEach(function(a){"
        "var f=FL[a.flag]||FL.white;"
        "var url=a.url&&a.url!='#'?a.url:'';"
        "html+='<div style=\"padding:12px 0;border-bottom:1px solid #f8fafc\">' ;"
        "html+='<div style=\"display:flex;align-items:center;gap:8px;margin-bottom:6px;flex-wrap:wrap\">' ;"
        "html+='<span style=\"background:'+f.bg+';color:'+f.color+';border:1px solid '+f.color+';border-radius:4px;padding:2px 8px;font-size:10px;font-weight:900\">'+f.label+'</span>' ;"
        "html+='<span style=\"background:#f1f5f9;color:#475569;border-radius:4px;padding:2px 8px;font-size:10px;font-weight:700\">'+esc(a.type)+'</span>' ;"
        "html+='</div>' ;"
        "if(url){html+='<div style=\"font-size:13px;font-weight:600;color:#0f172a;line-height:1.5;margin-bottom:4px\"><a href=\"'+esc(url)+'\" target=\"_blank\" style=\"color:#0f172a;text-decoration:none\">'+esc(a.title)+'</a></div>';}"
        "else{html+='<div style=\"font-size:13px;font-weight:600;color:#0f172a;line-height:1.5;margin-bottom:4px\">'+esc(a.title)+'</div>';}"
        "html+='<div style=\"font-size:11px;color:#94a3b8;margin-bottom:6px\">'+esc(a.source)+' · '+esc(a.date)+'</div>' ;"
        "if(url){html+='<a href=\"'+esc(url)+'\" target=\"_blank\" style=\"display:inline-flex;align-items:center;gap:5px;font-size:11px;color:#2563eb;text-decoration:none;padding:4px 10px;border:1px solid #bfdbfe;border-radius:5px;background:#eff6ff\">🔗 원문 보기</a>';}"
        "html+='</div>';});"
        "document.getElementById('wk-body').innerHTML=html;"
        "var ov=document.getElementById('wk-overlay');"
        "ov.style.display='flex';};"
        "window.wkClose=function(e){if(e.target===document.getElementById('wk-overlay'))"
        "document.getElementById('wk-overlay').style.display='none';};"
        "})();</script>"
    )


def _build_weekly_section(signals: list[ClassifiedSignal], generated_at: str) -> str:
    """Weekly 탭: 요일별 히트맵 + 수렴 리스크 + 다음 주 모니터링."""
    from collections import defaultdict as _dd
    from datetime import datetime as _dt, timedelta as _td
    from html import escape as _esc
    FLAG_RANK  = {"red": 0, "yellow": 1, "white": 2}
    FLAG_SCORE = {"red": 100, "yellow": 45, "white": 5}
    TYPE_WEIGHT = {
        "경영진 변동": 25, "M&A·Exit": 25, "M&A&Exit": 25,
        "자금조달": 20, "IPO·상장": 20, "IPO": 20, "재무": 18,
        "제품·기술": 15, "규제·법무": 15, "파트너십": 10,
    }
    # 시그널 유형별 고유 색상 (텍스트색, 배경색)
    TYPE_COLORS = {
        "M&A·Exit":   ("#1d4ed8", "#dbeafe"),
        "M&A&Exit":   ("#1d4ed8", "#dbeafe"),
        "자금조달":   ("#15803d", "#dcfce7"),
        "IPO·상장":   ("#0e7490", "#cffafe"),
        "IPO":        ("#0e7490", "#cffafe"),
        "재무":       ("#c2410c", "#ffedd5"),
        "경영진 변동":("#9f1239", "#ffe4e6"),
        "규제·법무":  ("#7c3aed", "#ede9fe"),
        "파트너십":   ("#0f766e", "#ccfbf1"),
        "제품·기술":  ("#1e40af", "#e0e7ff"),
    }
    _TYPE_COLOR_DEFAULT = ("#475569", "#f1f5f9")
    FLAG_TEXT  = {"red": "즉시검토", "yellow": "동향주시", "white": "정기모니터링"}
    WEEKDAY_KR = ["월", "화", "수", "목", "금", "토", "일"]

    def _fmt_md(d):     return f"{d.month}/{d.day}"
    def _fmt_kr_md(d):  return f"{d.month}월 {d.day}일"
    def _flag(s):       return (getattr(s, "action_flag", "white") or "white").lower()
    def _co(s):         return str(getattr(s, "portfolio_name", "미분류") or "미분류")
    def _title(s):      return str(getattr(s, "title", "") or getattr(s, "summary_ko", "") or "")
    def _summary(s):    return str(getattr(s, "summary_ko", "") or getattr(s, "title", "") or "")
    def _truncate(text, limit=72):
        text = " ".join(str(text or "").split())
        return text if len(text) <= limit else text[:limit].rstrip() + "…"
    def _parse_date(s):
        for fn in ("published_at", "classified_at", "created_at"):
            raw = getattr(s, fn, None)
            if not raw:
                continue
            try:
                return _dt.fromisoformat(str(raw).replace("Z", "+00:00")).date()
            except Exception:
                continue
        return None
    def _risk_score(items):
        score, types_seen, red_cnt = 0, set(), 0
        for item in items:
            f = _flag(item)
            t = str(getattr(item, "signal_type", "기타") or "기타")
            score += FLAG_SCORE.get(f, 5) + TYPE_WEIGHT.get(t, 8)
            types_seen.add(t); red_cnt += (f == "red")
        if len(items) >= 2:  score += min(35, (len(items)-1)*8)
        if len(types_seen) >= 2: score += 35
        if red_cnt >= 2:     score += 25
        return score

    from signal_db import SignalDB as _SDB
    _w_start, _w_end = _SDB.weekly_range()
    w_start_d, w_end_d = _w_start.date(), _w_end.date()
    show_days    = [w_start_d + _td(days=i) for i in range(5)]  # 대상 주 월~금
    week_label   = f"{w_start_d.year} {_fmt_kr_md(w_start_d)}~{_fmt_kr_md(w_end_d)}"
    next_monday  = w_end_d + _td(days=1)
    next_friday  = next_monday + _td(days=4)

    by_co     = _dd(list)
    by_co_day = _dd(lambda: _dd(list))
    for s in signals:
        co = _co(s)
        by_co[co].append(s)
        parsed = _parse_date(s)
        if parsed:
            by_co_day[co][parsed].append(s)

    def _company_sort_key(co):
        items = by_co[co]
        best_flag = min(FLAG_RANK.get(_flag(x), 2) for x in items)
        return (best_flag, -_risk_score(items), co)
    sorted_cos    = sorted(by_co.keys(), key=_company_sort_key)
    reds_total    = sum(1 for s in signals if _flag(s) == "red")
    yellows_total = sum(1 for s in signals if _flag(s) == "yellow")

    # AI 주간 총평
    try:
        ai_insight = _generate_weekly_insight(signals)
    except Exception:
        ai_insight = "주간 인사이트 생성 실패 — 주요 리스크 항목을 수동으로 확인해 주세요."
    import re as _re
    def _strip_md(t):
        t = _re.sub(r'\#{1,6}\s+', '', t)
        t = _re.sub(r'\*{2,3}(.+?)\*{2,3}', r'\1', t)
        t = _re.sub(r'\*(.+?)\*', r'\1', t)
        t = _re.sub(r'`(.+?)`', r'\1', t)
        return t.strip()
    insight_lines = [
        _strip_md(l.strip())
        for l in (ai_insight or "").split("\n")
        if l.strip() and not l.strip().startswith("---")
    ]
    # Weekly 총평 — Monthly M1 스타일로 섹션 구조화 렌더링
    _W_SEC = {
        "핵심 이슈":               ("🎯", "#6ee7b7"),
        "팔로업 사항":             ("📋", "#93c5fd"),
        "다음 주 모니터링 포인트": ("📌", "#fcd34d"),
    }
    def _weekly_section_html(lines_):
        from html import escape as _esc2
        secs = []
        cur_key, cur_items = None, []
        for ln in lines_:
            if ln.startswith("[") and "]" in ln:
                if cur_key is not None:
                    secs.append((cur_key, cur_items))
                cur_key = ln[1:ln.index("]")]
                cur_items = []
            elif cur_key is not None and ln:
                cur_items.append(ln)
        if cur_key is not None:
            secs.append((cur_key, cur_items))
        if not secs:
            # 섹션 헤더 없는 fallback 텍스트 — 그대로 렌더링
            from html import escape as _esc2
            fallback = "".join(
                f"<div style='margin:3px 0;font-size:12.5px;line-height:1.65;"
                f"color:rgba(255,255,255,.92)'>{_esc2(ln)}</div>"
                for ln in lines_ if ln
            )
            return fallback or "<div style='font-size:13px;color:rgba(255,255,255,.78)'>이번 주 특이 총평 없음</div>"
        parts = []
        for key, items in secs:
            emoji, color = _W_SEC.get(key, ("▸", "#e2e8f0"))
            parts.append(
                f"<div style='font-size:11px;font-weight:900;letter-spacing:1.2px;"
                f"color:{color};margin:14px 0 7px'>{emoji} {key.upper()}</div>"
            )
            rows = "".join(
                f"<div style='font-size:12.5px;line-height:1.7;color:rgba(255,255,255,.92);"
                f"padding:3px 0'>{_esc2(it)}</div>"
                for it in items if it
            )
            if rows:
                parts.append(
                    f"<div style='background:rgba(255,255,255,.06);border-radius:8px;"
                    f"padding:10px 14px;border-left:2px solid {color}'>{rows}</div>"
                )
        return "".join(parts)
    insight_html = _weekly_section_html(insight_lines) or         "<div style='font-size:13px;color:rgba(255,255,255,.78)'>이번 주 특이 총평 없음</div>"

    # ── 히트맵 헤더
    day_headers = "".join(
        "<th style='text-align:center;padding:10px 8px;font-size:12px;color:#dbe4ff;"
        f"font-weight:800;white-space:nowrap'>{WEEKDAY_KR[d.weekday()]} {_fmt_md(d)}</th>"
        for d in show_days
    )

    # ── 히트맵 행
    heatmap_rows = ""
    js_wk_cells = []   # 모달용 기사 데이터 (index -> {co, day, articles})
    for co in sorted_cos:
        items = by_co[co]
        best_flag = min(FLAG_RANK.get(_flag(x), 2) for x in items)
        flag_key  = "red" if best_flag == 0 else "yellow" if best_flag == 1 else "white"
        co_color  = "#ef4444" if flag_key == "red" else "#f59e0b" if flag_key == "yellow" else "#64748b"
        flag_em   = "🔴" if flag_key == "red" else "🟡" if flag_key == "yellow" else "⚪"

        row_cells = ""
        week_count = 0   # 히트맵 표시 기간(5영업일) 내 실제 기사 합계
        for day in show_days:
            day_items = by_co_day[co].get(day, [])
            n = len(day_items)
            week_count += n
            if not n:
                row_cells += "<td style='text-align:center;padding:11px 8px;color:#cbd5e1;font-size:13px'>—</td>"
                continue
            best_d = min(FLAG_RANK.get(_flag(x), 2) for x in day_items)
            if best_d == 0:
                bg, fg, border = "#fee2e2", "#dc2626", "#fecaca"
            elif best_d == 1:
                bg, fg, border = "#fef3c7", "#d97706", "#fde68a"
            else:
                bg, fg, border = "#dcfce7", "#16a34a", "#bbf7d0"
            cell_idx = len(js_wk_cells)
            day_label = f"{WEEKDAY_KR[day.weekday()]} {_fmt_md(day)}"
            js_wk_cells.append({
                "co": co, "day": day_label,
                "articles": [
                    {"title": _title(x),
                     "url": str(getattr(x, "url", "") or ""),
                     "flag": _flag(x),
                     "type": str(getattr(x, "signal_type", "기타") or "기타"),
                     "summary": _summary(x),
                     "date": str(getattr(x, "published_at", "") or "")[:10],
                     "source": str(getattr(x, "source", "") or "")}
                    for x in day_items
                ],
            })
            row_cells += (
                f"<td style='text-align:center;padding:8px'>"
                f"<span data-wk='{cell_idx}' onclick='wkModal(this)' "
                "style='display:inline-flex;align-items:center;justify-content:center;"
                f"min-width:30px;height:28px;background:{bg};color:{fg};border:1px solid {border};"
                f"border-radius:8px;font-size:13px;font-weight:900;cursor:pointer'>{n}</span></td>"
            )

        heatmap_rows += (
            "<tr style='border-bottom:1px solid #eef2f7'>"
            f"<td style='padding:12px 14px;font-weight:900;font-size:13px;color:#0f172a;white-space:nowrap'>"
            f"<span style='margin-right:7px'>{flag_em}</span>{_esc(co)}</td>"
            f"{row_cells}"
            f"<td style='text-align:center;padding:10px 8px;font-weight:900;font-size:13px;"
            f"color:{co_color};white-space:nowrap'>{week_count}건</td>"
            "</tr>"
        )
    if not heatmap_rows:
        heatmap_rows = ("<tr><td colspan='7' style='text-align:center;padding:32px;color:#94a3b8'>"
                        "이번 주 집계된 시그널이 없습니다.</td></tr>")

    # ── 히트맵 색상 범례
    heatmap_legend = (
        "<div style='display:flex;gap:14px;flex-wrap:wrap;padding:8px 4px 0;"
        "font-size:11px;color:#64748b'>"
        "<span style='font-weight:700;color:#475569'>셀 색상 기준:</span>"
        "<span><span style='display:inline-block;width:12px;height:12px;border-radius:3px;"
        "background:#fee2e2;border:1px solid #fecaca;vertical-align:middle;margin-right:4px'></span>"
        "🔴 즉시검토 (빨강)</span>"
        "<span><span style='display:inline-block;width:12px;height:12px;border-radius:3px;"
        "background:#fef3c7;border:1px solid #fde68a;vertical-align:middle;margin-right:4px'></span>"
        "🟡 동향주시 (노랑)</span>"
        "<span><span style='display:inline-block;width:12px;height:12px;border-radius:3px;"
        "background:#dcfce7;border:1px solid #bbf7d0;vertical-align:middle;margin-right:4px'></span>"
        "⚪ 정기모니터링 (초록)</span>"
        "<span style='color:#94a3b8'>— = 해당일 기사 없음 · 숫자 = 기사 건수</span>"
        "</div>"
    )

    # ── 수렴 시그널 분석 (최대 3건)
    convergence_candidates = []
    for co, items in by_co.items():
        type_counts = _dd(int)
        flag_counts = _dd(int)
        for x in items:
            type_counts[str(getattr(x, "signal_type", "기타") or "기타")] += 1
            flag_counts[_flag(x)] += 1
        if len(items) < 2:
            continue
        if len(type_counts) >= 2 or flag_counts["red"] >= 2 or max(type_counts.values() or [0]) >= 3:
            convergence_candidates.append((co, items, type_counts, flag_counts, _risk_score(items)))
    convergence_candidates.sort(key=lambda x: (-x[4], x[0]))

    convergence_cards = ""
    for co, items, type_counts, flag_counts, score in convergence_candidates[:3]:
        n_types = len(type_counts)
        if n_types >= 2:
            label, lc, lb = "HIGH CONVERGENCE", "#7c3aed", "#f5f3ff"
            headline = f"이번 주 {len(items)}건 · {n_types}개 리스크 카테고리 수렴"
        else:
            label, lc, lb = "CONCENTRATED", "#ef4444", "#fff1f2"
            top_type = max(type_counts, key=type_counts.get)
            headline = f"{top_type} 단일 이슈가 {type_counts[top_type]}건 반복 노출"

        # 유형별 개별 색상 pill
        pills = "".join(
            "<span style='display:inline-block;"
            f"background:{TYPE_COLORS.get(t, _TYPE_COLOR_DEFAULT)[1]};"
            f"color:{TYPE_COLORS.get(t, _TYPE_COLOR_DEFAULT)[0]};"
            f"border:1px solid {TYPE_COLORS.get(t, _TYPE_COLOR_DEFAULT)[0]};"
            "border-radius:5px;padding:3px 9px;font-size:10px;font-weight:900;"
            f"margin:2px 4px 2px 0'>{_esc(t)} ×{c}</span>"
            for t, c in sorted(type_counts.items(), key=lambda x: -x[1])[:4]
        )
        top_item = sorted(
            items,
            key=lambda x: (FLAG_RANK.get(_flag(x), 2),
                           -TYPE_WEIGHT.get(str(getattr(x, "signal_type", "기타") or "기타"), 8))
        )[0]
        desc = (_summary(top_item) or "").strip()  # 전문 표시 (잘림 금지)

        convergence_cards += (
            f"<div style='border:1.5px solid {lc};border-radius:12px;margin-bottom:12px;"
            "overflow:hidden;background:#fff;box-shadow:0 6px 18px rgba(15,23,42,.05)'>"
            f"<div style='background:{lb};padding:12px 14px;display:flex;align-items:center;gap:10px;flex-wrap:wrap'>"
            f"<span style='background:{lc};color:#fff;font-size:10px;font-weight:900;"
            f"padding:4px 9px;border-radius:6px;letter-spacing:.4px'>{label}</span>"
            f"<span style='font-weight:900;font-size:14px;color:#111827'>{_esc(co)}</span>"
            "</div>"
            f"<div style='padding:10px 14px 4px'>{pills}</div>"
            "<div style='padding:3px 14px 13px;font-size:12px;color:#475569;line-height:1.65'>"
            f"<b style='color:#111827'>{_esc(headline)}</b><br>{_esc(desc)}"
            "</div></div>"
        )
    if not convergence_cards:
        convergence_cards = (
            "<div style='background:#fff;border:1px dashed #cbd5e1;border-radius:12px;"
            "color:#94a3b8;font-size:12px;padding:18px;text-align:center'>"
            "이번 주 복수 리스크 수렴 시그널 없음</div>"
        )

    # ── 다음 주 모니터링 (상위 5개사, 중복 없이 핵심 1건)
    monitoring_items = ""
    for idx, co in enumerate(sorted_cos[:5], start=1):
        items = by_co[co]
        top_item = sorted(
            items,
            key=lambda x: (FLAG_RANK.get(_flag(x), 2),
                           -TYPE_WEIGHT.get(str(getattr(x, "signal_type", "기타") or "기타"), 8))
        )[0]
        f = _flag(top_item)
        fc = "#dc2626" if f == "red" else "#d97706" if f == "yellow" else "#64748b"
        em = "🔴" if f == "red" else "🟡" if f == "yellow" else "⚪"
        snippet = (_summary(top_item) or "").strip()  # 전문 표시 (잘림 금지)
        monitoring_items += (
            f"<div style='padding:9px 0;border-bottom:1px solid #f1f5f9;font-size:12.5px;line-height:1.6'>"
            f"<span style='color:{fc};font-weight:900'>{em} {_esc(co)}</span>"
            f"<span style='color:#475569'> — {_esc(snippet)}</span>"
            f"</div>"
        )
    if not monitoring_items:
        monitoring_items = (
            "<div style='color:#94a3b8;font-size:12px;text-align:center;padding:16px'>"
            "특이 모니터링 포인트 없음</div>"
        )

    return (
        "<div style='background:#eef2f7;padding:0 0 4px'>"
        # AI 총평 박스
        "<div style='background:linear-gradient(135deg,#1a2e7a 0%,#2d1b69 100%);"
        "border-radius:14px;padding:20px 22px;margin-bottom:18px;color:#fff;"
        "box-shadow:0 10px 28px rgba(30,41,59,.18)'>"
        "<div style='font-size:11px;color:rgba(255,255,255,.58);font-weight:800;"
        f"letter-spacing:.8px;margin-bottom:10px'>📊 AI 주간 포트폴리오 총평 — {week_label}</div>"
        "<div style='display:flex;gap:10px;flex-wrap:wrap;margin-bottom:12px'>"
        "<span style='background:rgba(255,255,255,.13);border:1px solid rgba(255,255,255,.18);"
        f"border-radius:999px;padding:5px 10px;font-size:12px;font-weight:800'>총 {len(signals)}건</span>"
        "<span style='background:rgba(239,68,68,.22);border:1px solid rgba(248,113,113,.35);"
        f"border-radius:999px;padding:5px 10px;font-size:12px;font-weight:800'>🔴 즉시검토 {reds_total}건</span>"
        "<span style='background:rgba(245,158,11,.22);border:1px solid rgba(251,191,36,.35);"
        f"border-radius:999px;padding:5px 10px;font-size:12px;font-weight:800'>🟡 동향주시 {yellows_total}건</span>"
        "<span style='background:rgba(255,255,255,.13);border:1px solid rgba(255,255,255,.18);"
        f"border-radius:999px;padding:5px 10px;font-size:12px;font-weight:800'>{len(by_co)}개사</span>"
        "</div>"
        f"{insight_html}"
        "</div>"
        # 히트맵 섹션 제목
        "<div style='font-size:13px;font-weight:900;color:#0f172a;"
        "padding:6px 0 10px 12px;border-left:4px solid #c0392b;margin-bottom:12px'>"
        "📅 요일별 시그널 히트맵"
        "<span style='font-size:11px;font-weight:600;color:#64748b;margin-left:8px'>"
        "중요 검토 필요 포트폴리오 우선 정렬 · 셀 색상은 해당일 최고 플래그</span></div>"
        # 히트맵 테이블
        "<div style='overflow-x:auto;border-radius:14px;box-shadow:0 8px 22px rgba(15,23,42,.06)'>"
        "<table style='border-collapse:collapse;width:100%;background:#fff;overflow:hidden'>"
        "<thead><tr style='background:#111827'>"
        "<th style='text-align:left;padding:12px 14px;font-size:12px;color:#fff;"
        "font-weight:900;min-width:150px'>포트폴리오사</th>"
        f"{day_headers}"
        "<th style='text-align:center;padding:12px 10px;font-size:12px;color:#fff;"
        "font-weight:900;white-space:nowrap'>주간계</th>"
        "</tr></thead>"
        f"<tbody>{heatmap_rows}</tbody>"
        "</table></div>"
        # 범례
        f"<div style='margin:6px 2px 18px'>{heatmap_legend}</div>"
        # 수렴 시그널 (풀너비)
        "<div style='margin-bottom:18px'>"
        "<div style='font-size:13px;font-weight:900;color:#0f172a;"
        "padding:4px 0 9px 12px;border-left:4px solid #8e44ad;margin-bottom:12px'>"
        "⚡ 수렴 시그널 분석"
        "<span style='font-size:11px;font-weight:600;color:#64748b;margin-left:6px'>"
        "동일 기업에서 복수 리스크 카테고리가 동시 발생한 경우</span></div>"
        f"{convergence_cards}"
        "</div>"
        # 다음 주 모니터링 (풀너비)
        "<div style='margin-bottom:8px'>"
        "<div style='font-size:13px;font-weight:900;color:#0f172a;"
        "padding:4px 0 9px 12px;border-left:4px solid #f39c12;margin-bottom:12px'>"
        f"📌 다음 주 모니터링 포인트 ({_fmt_md(next_monday)}~{_fmt_md(next_friday)})"
        "</div>"
        "<div style='background:#fffbea;border:1.5px solid #f39c12;border-radius:10px;padding:10px 16px'>"
        f"{monitoring_items}"
        "</div></div>"
        "</div>"  # outer end
        + _build_weekly_modal(js_wk_cells)
    )


def _build_monthly_section(signals: list[ClassifiedSignal], generated_at: str) -> str:
    """Monthly 탭 — M1(총평) M2(리스크 등급) M3(Exit 파이프라인) M4(팀 액션 로그) M5(다음 달 이벤트)."""
    from collections import defaultdict as _dd
    from html import escape as _esc
    from signal_db import SignalDB as _SDB
    _m_start, _m_end = _SDB.monthly_range()
    now = _m_start  # 리포트 기준월 = 전월 (라벨·비교월 계산 전용)

    # ── 공통 헬퍼
    FLAG_RANK  = {"red": 0, "yellow": 1, "white": 2}
    BADGE_STYLE = {
        "red":    ("즉시검토",      "#c0392b", "#fde8e8"),
        "yellow": ("동향주시",      "#856404", "#fef3cd"),
        "white":  ("정기모니터링",  "#27ae60", "#e8f8f0"),
    }

    def _badge(flag: str) -> str:
        label, color, bg = BADGE_STYLE.get(flag, BADGE_STYLE["white"])
        return (f"<span style='background:{bg};color:{color};border:1px solid {color};"
                f"border-radius:6px;padding:3px 9px;font-size:11px;font-weight:700;"
                f"white-space:nowrap'>{label}</span>")

    def _section_hd(icon: str, title: str, color: str) -> str:
        return (f"<div style='font-size:13px;font-weight:900;color:#0f172a;"
                f"padding:6px 0 10px 12px;border-left:4px solid {color};margin:0 0 14px'>"
                f"{icon} {title}</div>")

    reds    = [s for s in signals if s.action_flag == "red"]
    yellows = [s for s in signals if s.action_flag == "yellow"]

    # ── M1: AI 월간 총평
    try:
        ai_insight = _generate_monthly_insight(signals, now.year, now.month)
    except Exception:
        ai_insight = "월간 분석 생성 실패 — 수동 검토 필요."

    import re as _re_m
    def _strip_md_m(t):
        t = _re_m.sub(r'\#{1,6}\s+', '', t, flags=_re_m.MULTILINE)
        t = _re_m.sub(r'\*{2,3}(.+?)\*{2,3}', r'\1', t)
        t = _re_m.sub(r'\*(.+?)\*', r'\1', t)
        t = _re_m.sub(r'`(.+?)`', r'\1', t)
        return t.strip()
    ai_insight = _strip_md_m(ai_insight)

    insight_lines = [
        l.strip()
        for l in ai_insight.split("\n")
        if l.strip() and not l.strip().startswith("---")
    ]
    def _monthly_insight_line_html(line):
        is_header = line.startswith("[") and "]" in line
        if is_header:
            return (
                f"<div style='margin:13px 0 5px;font-size:13px;font-weight:900;"
                f"color:rgba(255,255,255,1.0);letter-spacing:.3px'>{_esc(line)}</div>"
            )
        return (
            f"<div style='margin:3px 0;font-size:12.5px;line-height:1.65;"
            f"color:rgba(255,255,255,.92)'>{_esc(line)}</div>"
        )
    insight_html = "".join(
        _monthly_insight_line_html(line)
        for line in insight_lines
        if line
    ) or "<div style='font-size:13px;color:rgba(255,255,255,.78)'>이번 달 분석 없음</div>"

    # ── M1 신규 디자인: JSON 응답이면 진단 박스 + Top3 카드(현황/영향/대응) + 경영층 콜아웃
    def _m1_tag(t, c):
        return (f"<span style='background:{c};color:#0f172a;border-radius:3px;"
                f"padding:1px 7px;font-size:9.5px;font-weight:900;margin-right:7px'>{t}</span>")

    def _m1_label(emoji, t, color="#6ee7b7"):
        return (f"<div style='font-size:11px;font-weight:900;letter-spacing:1.2px;"
                f"color:{color};margin-bottom:7px'>{emoji} {t}</div>")

    try:
        _js_s = ai_insight.find("{"); _js_e = ai_insight.rfind("}")
        _m1 = json.loads(ai_insight[_js_s:_js_e + 1]) if _js_s != -1 and _js_e > _js_s else {}
        _diag = (_m1.get("diagnosis") or "").strip()
        _top3 = _m1.get("top3") or []
        _exec = (_m1.get("exec_decision") or "").strip()
        _co_rank = {}
        for s in signals:
            r = FLAG_RANK.get(s.action_flag, 2)
            if r < _co_rank.get(s.portfolio_name, 2):
                _co_rank[s.portfolio_name] = r
        def _inline_row(tag, tag_color, text, last=False):
            mb = "" if last else "margin-bottom:7px;"
            return (
                "<div style='display:flex;align-items:baseline;gap:8px;" + mb + "'>"
                "<span style='background:" + tag_color + ";color:#0f172a;border-radius:3px;"
                "padding:1px 7px;font-size:9.5px;font-weight:900;white-space:nowrap;flex-shrink:0'>" + tag + "</span>"
                "<span style='font-size:12px;line-height:1.5;color:rgba(255,255,255,.88)'>" + _esc(text) + "</span>"
                "</div>"
            )
        _card_list = []
        for it in _top3[:3]:
            _co = (it.get("company") or "").strip()
            _fl_r = _co_rank.get(_co, 2)
            _fl = {0: "🔴", 1: "🟡", 2: "⚪"}.get(_fl_r, "⚪")
            _bc = "#e74c3c" if _fl_r == 0 else "#fbbf24" if _fl_r == 1 else "rgba(255,255,255,.2)"
            _card_list.append(
                "<div style='background:rgba(255,255,255,.07);border:1px solid rgba(255,255,255,.15);"
                "border-top:2px solid " + _bc + ";border-radius:10px;padding:14px 16px'>"
                "<div style='font-size:13px;font-weight:900;margin-bottom:12px'>" + _fl + " " + _esc(_co) + "</div>"
                + _inline_row("현황", "#94a3b8", (it.get("status") or "").strip())
                + _inline_row("영향", "#fbbf24", (it.get("impact") or "").strip())
                + _inline_row("대응", "#6ee7b7", (it.get("action") or "").strip(), last=True)
                + "</div>"
            )
        _cards = (
            "<div style='display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-bottom:8px'>"
            + "".join(_card_list)
            + "</div>"
        )
        if _diag and _cards:
            _struct = (
                _m1_label("🩺", "이달의 진단")
                + "<div style='display:flex;gap:10px;align-items:flex-start;background:rgba(110,231,183,.08);"
                  "border:1px solid rgba(110,231,183,.25);border-radius:10px;padding:11px 14px;margin-bottom:16px'>"
                  "<span style='font-size:17px;line-height:1.4'>📌</span>"
                  f"<div style='font-size:13.5px;line-height:1.7;font-weight:600'>{_esc(_diag)}</div></div>"
                + _m1_label("🎯", "TOP 3 핵심 이슈") + _cards
            )
            if _exec:
                _struct += (
                    "<div style='margin-top:13px;background:rgba(251,191,36,.1);border-left:3px solid #fbbf24;"
                    "border-radius:0 8px 8px 0;padding:10px 14px'>"
                    "<div style='font-size:11px;font-weight:900;letter-spacing:1.2px;color:#fbbf24;margin-bottom:7px'>⚖️ 경영층 판단 필요</div>"
                    f"<div style='font-size:13.5px;font-weight:600;line-height:1.7;margin-top:3px'>{_esc(_exec)}</div></div>"
                )
            insight_html = _struct
    except Exception:
        pass  # JSON 아니면 기존 라인 렌더링 유지

    # ── M2: 리스크 등급 변화 테이블
    by_co: dict = _dd(list)
    for s in signals:
        by_co[s.portfolio_name].append(s)

    def _co_flag(items):
        best = min(FLAG_RANK.get(s.action_flag, 2) for s in items)
        return "red" if best == 0 else "yellow" if best == 1 else "white"

    def _prev_flag(items, curr):
        """현재 플래그보다 한 단계 낮은 추정값 반환."""
        if curr == "red":
            return "yellow" if any(s.action_flag == "yellow" for s in items) else "white"
        elif curr == "yellow":
            return "white"
        return "white"

    def _arrow_html(prev, curr):
        p, c = FLAG_RANK.get(prev, 2), FLAG_RANK.get(curr, 2)
        if c < p:   return "<span style='color:#e74c3c;font-weight:900;font-size:16px'>↑</span>"
        elif c > p: return "<span style='color:#27ae60;font-weight:900;font-size:16px'>↓</span>"
        else:       return "<span style='color:#95a5a6;font-size:16px'>→</span>"

    sorted_cos = sorted(by_co.items(), key=lambda x: (FLAG_RANK.get(_co_flag(x[1]), 2), x[0]))
    risk_rows = ""
    for co, items in sorted_cos:
        curr  = _co_flag(items)
        prev  = _prev_flag(items, curr)
        types = list({s.signal_type for s in items if s.action_flag in ("red", "yellow")})[:2]
        reason = " · ".join(types) if types else "시그널 감지"
        risk_rows += (
            f"<tr style='border-bottom:1px solid #f0f2f5'>"
            f"<td style='padding:12px 14px;font-weight:700;font-size:13px;color:#0f172a'>{_esc(co)}</td>"
            f"<td style='padding:12px 10px;text-align:center'>{_badge(prev)}</td>"
            f"<td style='padding:12px 6px;text-align:center'>{_arrow_html(prev, curr)}</td>"
            f"<td style='padding:12px 10px;text-align:center'>{_badge(curr)}</td>"
            f"<td style='padding:12px 14px;font-size:12px;color:#6c757d'>{_esc(reason)}</td>"
            "</tr>"
        )
    if not risk_rows:
        risk_rows = "<tr><td colspan='5' style='text-align:center;padding:20px;color:#adb5bd;font-size:12px'>시그널 데이터 없음</td></tr>"

    # ── M3: Exit 파이프라인
    EXIT_TYPES = {"M&A·Exit", "M&A&Exit", "IPO·상장", "IPO"}
    exit_seen: set = set()
    exit_cards = ""
    for s in sorted(signals, key=lambda x: FLAG_RANK.get(x.action_flag, 2)):
        if s.signal_type not in EXIT_TYPES or s.portfolio_name in exit_seen:
            continue
        exit_seen.add(s.portfolio_name)
        content    = (s.summary_ko or s.title or "").lower()
        stype      = (s.signal_type or "")
        is_ipo     = ("ipo" in stype.lower() or "상장" in stype or
                      "ipo" in content or "상장" in content or "기업공개" in content)
        is_ma      = ("m&a" in stype.lower() or "인수" in content or "매각" in content)
        if is_ipo:
            exit_label = "IPO·상장"
            exit_color = "#0e7490"
            exit_bg    = "#cffafe"
        elif is_ma:
            exit_label = "M&A·인수"
            exit_color = "#dc2626"
            exit_bg    = "#fee2e2"
        else:
            exit_label = stype or "Exit"
            exit_color = "#7c3aed"
            exit_bg    = "#ede9fe"
        timing     = "모니터링 중"
        status_txt = _finish_summary(s.summary_ko, 30) if s.summary_ko else "모니터링 중"
        exit_cards += (
            f"<div style='background:#fff;border:1px solid #e2e8f0;border-radius:10px;"
            f"padding:14px 18px;margin-bottom:10px'>"
            f"<div style='display:flex;gap:16px;align-items:flex-start'>"
            f"<div style='min-width:130px'>"
            f"<div style='font-size:14px;font-weight:800;color:#0f172a'>{_esc(s.portfolio_name)}</div>"
            f"<div style='font-size:11px;color:#94a3b8;margin-top:2px'>포트폴리오사</div></div>"
            f"<div style='flex:1;display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px'>"
            f"<div><div style='font-size:10px;color:#94a3b8;font-weight:700;margin-bottom:5px'>EXIT 유형</div>"
            f"<span style='background:{exit_bg};color:{exit_color};border:1px solid {exit_color};"
            f"border-radius:4px;padding:2px 9px;font-size:11px;font-weight:700'>{exit_label}</span></div>"
            f"<div><div style='font-size:10px;color:#94a3b8;font-weight:700;margin-bottom:5px'>예상 시기</div>"
            f"<span style='font-size:13px;font-weight:700;color:{exit_color}'>{timing}</span></div>"
            f"<div><div style='font-size:10px;color:#94a3b8;font-weight:700;margin-bottom:5px'>준비 상태</div>"
            f"<span style='font-size:12px;color:#475569'>{_esc(status_txt)}</span></div>"
            "</div></div></div>"
        )
        if len(exit_seen) >= 4:
            break
    if not exit_cards:
        exit_cards = ("<div style='background:#f8fafc;border:1px dashed #cbd5e1;border-radius:10px;"
                      "padding:18px;text-align:center;color:#94a3b8;font-size:12px'>"
                      "이달 M&A · Exit · IPO 시그널 없음</div>")

    # ── M4: 팀 액션 로그 (텔레그램 /log 연동)
    import json as _json_m4
    from pathlib import Path as _Path_m4
    _log_file = _Path_m4("data/action_log.json")
    _log_entries = []
    if _log_file.exists():
        try:
            _all_entries = _json_m4.loads(_log_file.read_text(encoding="utf-8"))
            _month_prefix = _m_start.strftime("%Y-%m")
            _log_entries = [e for e in _all_entries if str(e.get("ts", "")).startswith(_month_prefix)]
        except Exception:
            _log_entries = []

    if _log_entries:
        from html import escape as _esc_log
        _log_rows = ""
        for _e in _log_entries:
            _ts = _e.get("ts", "")
            _author = _e.get("author", "")
            _content = _e.get("content", "")
            _log_rows += (
                "<div style='display:flex;gap:12px;padding:10px 0;"
                "border-bottom:1px solid #f1f5f9;align-items:flex-start'>"
                "<div style='min-width:90px;font-size:11px;color:#94a3b8;padding-top:2px'>"
                f"{_esc_log(_ts[:10])}<br>{_esc_log(_ts[11:16])}</div>"
                "<div style='flex:1'>"
                f"<div style='font-size:13px;color:#0f172a;line-height:1.6'>{_esc_log(_content)}</div>"
                + (f"<div style='font-size:11px;color:#94a3b8;margin-top:2px'>— {_esc_log(_author)}</div>" if _author else "")
                + "</div></div>"
            )
        action_log = (
            "<div style='background:#fff;border:1px solid #e2e8f0;border-radius:10px;"
            "padding:4px 16px 8px'>"
            + _log_rows
            + "<div style='font-size:11px;color:#94a3b8;padding:8px 0;text-align:right'>"
            f"총 {len(_log_entries)}건 · 텔레그램 /log 로 추가 가능</div>"
            "</div>"
        )
    else:
        action_log = (
            "<div style='background:#f8fafc;border:1.5px dashed #cbd5e1;border-radius:10px;"
            "padding:28px;text-align:center;color:#94a3b8'>"
            "<div style='font-size:13px;font-weight:600;margin-bottom:8px'>이번 달 액션 로그가 없습니다</div>"
            "<div style='font-size:12px;line-height:1.8;color:#64748b'>"
            "텔레그램 봇에 아래 형식으로 전송하면 자동 수집됩니다<br>"
            "<code style='background:#f1f5f9;padding:2px 8px;border-radius:4px;font-size:12px'>"
            "/log [내용]</code><br>"
            "<span style='font-size:11px;color:#94a3b8'>예시: /log 컬리 경영진 대응 미팅 완료</span>"
            "</div></div>"
        )

    # ── M5: 다음 달 주요 이벤트 — 기업별 1건(중복 제거), 이슈 → 다음 달 모니터링 포인트
    _watch_map = {
        "M&A·Exit":       "딜 진행 경과 및 당사 지분 가치·Exit 일정 영향 확인",
        "펀딩·밸류에이션": "라운드 조건·밸류에이션 확정 여부 및 지분 희석 영향 확인",
        "경영진 변동":     "후임 선임 일정 및 경영 공백 리스크 점검",
        "규제·법률 리스크": "규제·소송 진행 경과 및 사업 영향 범위 점검",
        "재무·실적":       "실적 추이 및 자금 조달 계획 모니터링",
        "파트너십·협업":   "협업 구체화 진척 및 사업 시너지 확인",
        "제품·기술 출시":  "시장 반응 및 매출 기여도 추적",
        "평판·ESG":       "여론 추이 및 회사 대응 현황 점검",
    }
    _seen_co = set()
    top_events = []
    for s in sorted(
        [s for s in signals if s.action_flag in ("red", "yellow", "white")],
        key=lambda x: (FLAG_RANK.get(x.action_flag, 2), x.portfolio_name)
    ):
        if s.portfolio_name in _seen_co:
            continue
        _seen_co.add(s.portfolio_name)
        top_events.append(s)
        if len(top_events) >= 4:
            break
    event_cards = ""
    for s in top_events:
        label, color, bg = BADGE_STYLE.get(s.action_flag, BADGE_STYLE["white"])
        summary = _finish_summary(s.summary_ko or s.title or "", 60)
        watch   = _watch_map.get(s.signal_type, "후속 보도 및 사업 영향 모니터링")
        event_cards += (
            f"<div style='background:#fff;border:1px solid #e2e8f0;"
            f"border-left:4px solid {color};border-radius:0 8px 8px 0;"
            f"padding:12px 16px;margin-bottom:10px'>"
            f"<div style='display:flex;align-items:center;gap:8px;margin-bottom:5px'>"
            f"<span style='background:{bg};color:{color};border-radius:4px;"
            f"padding:2px 9px;font-size:10px;font-weight:700'>{label}</span>"
            f"<span style='font-size:13px;font-weight:700;color:#0f172a'>{_esc(s.portfolio_name)}</span>"
            "</div>"
            f"<div style='font-size:12px;color:#475569;line-height:1.55'>{_esc(summary)}</div>"
            f"<div style='font-size:12px;color:#1a2744;font-weight:600;line-height:1.55;margin-top:4px'>"
            f"→ 다음 달: {watch}</div>"
            "</div>"
        )
    if not event_cards:
        event_cards = ("<div style='color:#94a3b8;font-size:12px;text-align:center;padding:16px'>"
                       "다음 달 주요 모니터링 항목 없음</div>")

    next_month      = now.month + 1 if now.month < 12 else 1
    next_year       = now.year if now.month < 12 else now.year + 1
    prev_month_name = f"{now.month - 1}월" if now.month > 1 else "12월"
    next_month_name = f"{next_year}년 {next_month}월"

    return (
        # ── M1
        "<div style='background:linear-gradient(135deg,#1a3a2e,#0d2137);"
        "border-radius:14px;padding:20px 22px;margin-bottom:20px;color:#fff;"
        "box-shadow:0 10px 28px rgba(15,23,42,.12)'>"
        "<div style='font-size:11px;color:rgba(255,255,255,.55);font-weight:800;"
        f"letter-spacing:.8px;margin-bottom:10px'>🗓 월간 포트폴리오 리뷰 — {now.year}년 {now.month}월</div>"
        "<div style='display:flex;gap:10px;flex-wrap:wrap;margin-bottom:12px'>"
        f"<span style='background:rgba(255,255,255,.13);border:1px solid rgba(255,255,255,.18);"
        f"border-radius:999px;padding:5px 10px;font-size:12px;font-weight:800'>총 {len(signals)}건</span>"
        f"<span style='background:rgba(239,68,68,.22);border:1px solid rgba(248,113,113,.35);"
        f"border-radius:999px;padding:5px 10px;font-size:12px;font-weight:800'>🔴 즉시검토 {len(reds)}건</span>"
        f"<span style='background:rgba(245,158,11,.22);border:1px solid rgba(251,191,36,.35);"
        f"border-radius:999px;padding:5px 10px;font-size:12px;font-weight:800'>🟡 동향주시 {len(yellows)}건</span>"
        "</div>"
        f"{insight_html}"
        "</div>"
        # ── M2
        + _section_hd("📊", f"포트폴리오 리스크 등급 변화 {prev_month_name} → {now.month}월", "#2980b9")
        + "<div style='overflow-x:auto;border-radius:12px;"
          "box-shadow:0 4px 12px rgba(15,23,42,.06);margin-bottom:20px'>"
          "<table style='border-collapse:collapse;width:100%;background:#fff'>"
          "<thead><tr style='background:#111827'>"
          "<th style='text-align:left;padding:12px 14px;font-size:11px;color:#fff;font-weight:700;min-width:120px'>포트폴리오사</th>"
          "<th style='text-align:center;padding:12px 10px;font-size:11px;color:#fff;font-weight:700'>이전</th>"
          "<th style='text-align:center;padding:12px 6px;font-size:11px;color:#fff;font-weight:700'>변화</th>"
          "<th style='text-align:center;padding:12px 10px;font-size:11px;color:#fff;font-weight:700'>현재</th>"
          "<th style='text-align:left;padding:12px 14px;font-size:11px;color:#fff;font-weight:700'>변화 원인</th>"
          "</tr></thead>"
        + f"<tbody>{risk_rows}</tbody></table></div>"
        # ── M4
        + _section_hd("📝", "이달 팀 액션 로그", "#7f8c8d")
        + f"<div style='margin-bottom:20px'>{action_log}</div>"
        # ── M5
        + _section_hd("📅", f"다음 달 주요 이벤트 ({next_month_name} 예상)", "#2980b9")
        + f"<div style='margin-bottom:8px'>{event_cards}</div>"
    )


def _build_drafts_section(signals: list[ClassifiedSignal]) -> str:
    """커뮤니케이션 초안 — Daily 탭 하단 섹션으로 표시."""
    reds    = [s for s in signals if s.action_flag == "red"][:6]
    yellows = [s for s in signals if s.action_flag == "yellow"][:4]

    # 🔴가 없으면 🟡 시그널로 초안 생성 (동향주시도 선제 커뮤니케이션 가능)
    target_signals = reds if reds else yellows
    flag_label = "🔴 즉시검토" if reds else "🟡 동향주시"

    if not target_signals:
        return (
            '<div style="text-align:center;padding:40px;color:#adb5bd;font-size:14px">'
            '오늘 즉시검토·동향주시 시그널이 없어 초안이 생성되지 않았습니다.<br>'
            '<span style="font-size:12px">내일 GitHub Actions 실행 후 자동 업데이트됩니다.</span></div>'
        )

    rows = ""
    for i, s in enumerate(target_signals):
        try:
            msg_exec, msg_portfolio = _draft_contact_messages(s)
        except Exception:
            msg_exec = f"[{s.signal_type}] {s.portfolio_name} 관련 이슈 보고드립니다.\n{s.summary_ko or ''}\n검토 부탁드립니다."
            msg_portfolio = f"안녕하세요 대표님,\n\n{s.portfolio_name} 관련 {s.signal_type} 이슈({(s.title or '')[:40]})로 연락드립니다.\n\n현황 공유 부탁드립니다."

        def _card(title, text, idx, kind):
            esc = text.replace("'", "\\'").replace("\n", "\\n")
            return f"""
        <div style="background:#fff;border-radius:8px;border:1px solid #e9ecef;margin-bottom:10px;overflow:hidden">
          <div style="background:#f8f9fa;padding:10px 14px;display:flex;justify-content:space-between;align-items:center;cursor:pointer"
               onclick="this.nextElementSibling.style.display=this.nextElementSibling.style.display==='none'?'block':'none'">
            <span style="font-size:12px;font-weight:700;color:#495057">{title}</span>
            <span style="font-size:11px;color:#6c757d">▼ 펼치기</span>
          </div>
          <div style="display:none;padding:12px 14px">
            <pre style="font-size:12px;color:#343a40;white-space:pre-wrap;line-height:1.6;margin-bottom:10px;font-family:'Malgun Gothic','맑은 고딕',sans-serif">{text}</pre>
            <button onclick="navigator.clipboard.writeText('{esc}').then(()=>{{this.textContent='✓ 복사됨';setTimeout(()=>this.textContent='복사',2000)}})"
                    style="font-size:11px;padding:5px 16px;background:#1a2744;color:#fff;border:none;border-radius:5px;cursor:pointer">복사</button>
          </div>
        </div>"""

        rows += f"""
      <div style="margin-bottom:20px">
        <div style="font-size:14px;font-weight:700;color:#1a1a2e;margin-bottom:8px;padding-bottom:6px;border-bottom:2px solid #e9ecef">
          <span style="background:#f3e5f5;color:#6c3483;border-radius:4px;padding:2px 8px;font-size:10px;margin-right:8px">{s.signal_type}</span>
          {s.portfolio_name}
        </div>
        <div style="font-size:12px;color:#495057;margin-bottom:8px">{_finish_summary(s.summary_ko or s.title or '', 80)}</div>
        {_card('📤 경영진 보고용 (내부)', msg_exec, i, 'exec')}
        {_card('📧 포트폴리오사 대표 연락용', msg_portfolio, i, 'portfolio')}
      </div>"""

    return f"""
    <div style="background:#fff3cd;border-left:4px solid #f39c12;border-radius:0 8px 8px 0;
                padding:10px 16px;margin-bottom:16px;font-size:13px;color:#7d4e00">
      💡 <b>사용 방법:</b> 각 항목을 클릭하면 초안이 펼쳐집니다. <b>복사</b> 버튼으로 카카오톡·이메일에 붙여넣기 하세요.
      <span style="float:right;font-size:11px;background:#fff;border-radius:4px;padding:2px 8px;color:#495057">{flag_label} 시그널 대상 · {len(target_signals)}건</span>
    </div>
    {rows}
"""


def _build_daily_overview_section(signals: list[ClassifiedSignal], generated_at: str) -> str:
    """Daily 탭 내부 콘텐츠만 생성 (메트릭 카드 + Exec Summary + 시그널 카드)."""
    reds    = [s for s in signals if s.action_flag == "red"]
    yellows = [s for s in signals if s.action_flag == "yellow"]
    whites  = [s for s in signals if s.action_flag == "white"]
    total   = len(signals)

    try:
        from collector import load_portfolios
        total_portfolio = len(load_portfolios("portfolio.yaml"))
    except Exception:
        total_portfolio = 14
    companies = len(set(s.portfolio_name for s in signals))

    # ── Executive Summary (회사당 최대 1건만 표시)
    if reds:
        _seen_r: set = set()
        exec_lines = []
        for s in reds:
            if s.portfolio_name not in _seen_r:
                _seen_r.add(s.portfolio_name)
                exec_lines.append(f"🔴 {s.portfolio_name} — {_exec_text(s)}")
            if len(exec_lines) >= 3:
                break
        _seen_y: set = set()
        for s in yellows:
            if s.portfolio_name not in _seen_y:
                _seen_y.add(s.portfolio_name)
                exec_lines.append(f"🟡 {s.portfolio_name} — {_exec_text(s)}")
            if len(_seen_y) >= 2:
                break
        exec_bg = "#fdf5f5"; exec_border = "#e74c3c"; exec_color = "#c0392b"
    elif yellows:
        _seen_y2: set = set()
        exec_lines = []
        for s in yellows:
            if s.portfolio_name not in _seen_y2:
                _seen_y2.add(s.portfolio_name)
                exec_lines.append(f"🟡 {s.portfolio_name} — {_exec_text(s)}")
            if len(exec_lines) >= 4:
                break
        exec_bg = "#fdfbf0"; exec_border = "#f39c12"; exec_color = "#d68910"
    else:
        exec_lines = ["⚪ 오늘 포트폴리오 전반 특이사항 없음 — 정기모니터링 유지"]
        exec_bg = "#f0fdf4"; exec_border = "#27ae60"; exec_color = "#27ae60"
    exec_html = "".join(
        f'<div style="margin-bottom:4px">{line}</div>' for line in exec_lines
    )

    # ── 시그널 카드 (flag별 배너 + 회사별 묶음)
    from collections import defaultdict as _dd
    _DISPLAY_PRIORITY = {"업스테이지": 1, "비엠스마일": 2, "컬리": 3, "에버온": 4}

    def _flag_banner(flag, label, sub, count):
        return (
            f'<div class="flag-banner {flag}">'
            f'<div><span class="fb-label">{label}</span>'
            f'<span class="fb-sub">{sub}</span></div>'
            f'<span class="fb-pill">{count}건</span></div>'
        )

    red_co: dict = _dd(list)
    yellow_co: dict = _dd(list)
    for s in signals:
        if s.action_flag == "red":   red_co[s.portfolio_name].append(s)
        elif s.action_flag == "yellow": yellow_co[s.portfolio_name].append(s)

    def _sort_co(d):
        return sorted(d.items(), key=lambda kv: (_DISPLAY_PRIORITY.get(kv[0], 99), kv[0]))

    cards_html = ""
    if red_co:
        _red_inner = _flag_banner("red", "🔴 즉시검토", "Immediate Review", len(reds))
        for co, sigs in _sort_co(red_co):
            _red_inner += _company_section_html(co, sorted(sigs, key=lambda x: 0 if x.action_flag == "red" else 1))
        cards_html += f'<div data-flag-group="red">{_red_inner}</div>'
    if yellow_co:
        _yellow_inner = _flag_banner("yellow", "🟡 동향주시", "Watch & Report", len(yellows))
        for co, sigs in _sort_co(yellow_co):
            _yellow_inner += _company_section_html(co, sigs)
        cards_html += f'<div data-flag-group="yellow">{_yellow_inner}</div>'
    white_co: dict = _dd(list)
    for s in signals:
        if s.action_flag == "white":
            white_co[s.portfolio_name].append(s)
    if white_co:
        _white_inner = _flag_banner("white", "⚪ 정기모니터링", "Routine Track", len(whites))
        for co, sigs in _sort_co(white_co):
            _white_inner += _company_section_html(co, sigs)
        cards_html += f'<div data-flag-group="white">{_white_inner}</div>'
    if not cards_html:
        cards_html = '<div style="text-align:center;padding:24px;color:#adb5bd;font-size:13px">오늘 검토 항목 없음 · No items today</div>'

    return f"""
    <!-- 지표 카드: 3개 플래그 -->
    <table width="100%" cellspacing="0" cellpadding="0"
           style="margin:14px 0 10px;border-collapse:separate;border-spacing:8px 0">
      <tr>
        <td onclick="filterFlag('red')" style="background:#fff;border-radius:10px;padding:16px 8px 14px;
            text-align:center;border-top:3px solid #e74c3c;
            box-shadow:0 2px 8px rgba(0,0,0,.06);cursor:pointer;transition:opacity .15s">
          <div style="font-size:34px;font-weight:800;line-height:1;color:#c0392b">{len(reds)}</div>
          <div style="font-size:10.5px;color:#adb5bd;margin-top:5px;line-height:1.5;font-weight:500">🔴 즉시검토<br><span style="font-size:9px;color:#e0aaaa">Immediate Review</span></div>
        </td>
        <td onclick="filterFlag('yellow')" style="background:#fff;border-radius:10px;padding:16px 8px 14px;
            text-align:center;border-top:3px solid #f39c12;
            box-shadow:0 2px 8px rgba(0,0,0,.06);cursor:pointer;transition:opacity .15s">
          <div style="font-size:34px;font-weight:800;line-height:1;color:#d68910">{len(yellows)}</div>
          <div style="font-size:10.5px;color:#adb5bd;margin-top:5px;line-height:1.5;font-weight:500">🟡 동향주시<br><span style="font-size:9px;color:#d4b96a">Watch & Report</span></div>
        </td>
        <td onclick="filterFlag('white')" style="background:#fff;border-radius:10px;padding:16px 8px 14px;
            text-align:center;border-top:3px solid #adb5bd;
            box-shadow:0 2px 8px rgba(0,0,0,.06);cursor:pointer;transition:opacity .15s">
          <div style="font-size:34px;font-weight:800;line-height:1;color:#868e96">{len(whites)}</div>
          <div style="font-size:10.5px;color:#adb5bd;margin-top:5px;line-height:1.5;font-weight:500">⚪ 정기모니터링<br><span style="font-size:9px">Reference</span></div>
        </td>
      </tr>
    </table>

    <!-- 모니터링 기업 -->
    <div style="background:#f0f4f8;border-radius:10px;padding:10px 16px;margin-bottom:16px;
                display:flex;align-items:center;justify-content:space-between;
                border:1px solid #dde3ea">
      <div style="font-size:11px;color:#6c757d;font-weight:600;letter-spacing:.3px">
        📡 오늘 시그널 감지 기업
      </div>
      <div style="display:flex;align-items:baseline;gap:4px">
        <span style="font-size:22px;font-weight:800;color:#1a1a2e;line-height:1">{companies}</span>
        <span style="font-size:12px;color:#adb5bd;font-weight:500">/ {total_portfolio}개사</span>
      </div>
    </div>

    <!-- Executive Summary -->
    <div class="exec-box" style="background:{exec_bg};border-color:{exec_border};">
      <span class="e-label" style="color:{exec_color};">Executive Summary</span>
      <div class="e-main" style="color:#1a1a2e;line-height:2">
        {exec_html}
      </div>
    </div>

    <!-- 모니터링 포인트 & 권고 액션 -->
    <div class="section-header">
      <span>📋 모니터링 포인트 &amp; 권고 액션 &nbsp;|&nbsp; Monitoring &amp; Actions</span>
    </div>

    {cards_html}
"""


# =============================================================================
# 인터랙티브 커뮤니케이션 초안 페이지 (drafts.html)
# =============================================================================

def build_drafts_html(drafts_data: list[dict], generated_at: str) -> str:
    """클릭 펼침 + 복사 버튼이 있는 커뮤니케이션 초안 인터랙티브 페이지."""
    rows = ""
    panels = ""
    for i, d in enumerate(drafts_data):
        co       = d.get("portfolio_name", "")
        sig      = d.get("signal_type", "")
        summ     = _finish_summary(d.get("summary_ko") or "", 60)
        msg_e    = (d.get("msg_exec") or "").replace("`", "\\`").replace("\\n", "\n")
        msg_p    = (d.get("msg_portfolio") or "").replace("`", "\\`").replace("\\n", "\n")
        msg_e_js = json.dumps(msg_e, ensure_ascii=False)
        msg_p_js = json.dumps(msg_p, ensure_ascii=False)

        rows += f"""
        <tr class="sig-row" onclick="toggle({i})" id="row-{i}">
          <td><span class="co-badge">{co}</span></td>
          <td><span class="type-tag">{sig}</span></td>
          <td class="summ-cell">{summ}</td>
          <td class="arrow-cell"><span id="arr-{i}">▶</span></td>
        </tr>
        <tr class="draft-panel" id="panel-{i}" style="display:none">
          <td colspan="4">
            <div class="dp-inner">
              <div class="msg-block exec-block">
                <div class="msg-label">📤 경영층 문자 초안 (카카오톡/SMS)</div>
                <div class="msg-body" id="exec-{i}">{(d.get("msg_exec") or "").replace(chr(10),"<br>")}</div>
                <button class="copy-btn" onclick="copyMsg({msg_e_js}, this)">복사</button>
              </div>
              <div class="msg-block port-block">
                <div class="msg-label">💼 포트폴리오사 문의 초안</div>
                <div class="msg-body" id="port-{i}">{(d.get("msg_portfolio") or "").replace(chr(10),"<br>")}</div>
                <button class="copy-btn" onclick="copyMsg({msg_p_js}, this)">복사</button>
              </div>
              <div class="draft-note">※ AI 생성 초안입니다. 발송 전 반드시 검토·수정 후 사용하세요.</div>
            </div>
          </td>
        </tr>"""

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>커뮤니케이션 초안 | Portfolio Intelligence</title>
<style>
  * {{ box-sizing:border-box; margin:0; padding:0; }}
  body {{ font-family:'Malgun Gothic','Segoe UI',-apple-system,sans-serif; background:#edf0f4; color:#1a1a2e; }}
  .wrap {{ max-width:900px; margin:0 auto; padding:24px 20px; }}

  .topbar {{ background:linear-gradient(150deg,#0d1b2a,#1a2744); border-radius:12px;
             padding:22px 28px; margin-bottom:20px; }}
  .topbar h1 {{ color:#fff; font-size:18px; font-weight:700; }}
  .topbar .meta {{ color:rgba(255,255,255,.4); font-size:11px; margin-top:4px; }}

  .guide {{ background:#fff8e1; border-left:4px solid #f39c12; border-radius:0 8px 8px 0;
            padding:10px 16px; margin-bottom:18px; font-size:12px; color:#7d4e00; }}

  table {{ width:100%; border-collapse:collapse; background:#fff;
           border-radius:12px; overflow:hidden; box-shadow:0 2px 8px rgba(0,0,0,.06); }}
  thead tr {{ background:#1a1a2e; }}
  thead th {{ padding:12px 14px; text-align:left; font-size:11px; font-weight:600;
              color:rgba(255,255,255,.7); letter-spacing:.5px; text-transform:uppercase; }}

  .sig-row {{ cursor:pointer; border-bottom:1px solid #f1f3f5; transition:background .15s; }}
  .sig-row:hover {{ background:#f8f9fa; }}
  .sig-row td {{ padding:12px 14px; vertical-align:middle; }}

  .co-badge {{ font-size:13px; font-weight:700; color:#1a1a2e; }}
  .type-tag {{ background:#f1f3f5; color:#6c3483; font-size:11px; font-weight:600;
               padding:3px 10px; border-radius:20px; white-space:nowrap; }}
  .summ-cell {{ font-size:12px; color:#555; }}
  .arrow-cell {{ text-align:center; color:#adb5bd; font-size:12px; width:40px; }}

  .draft-panel td {{ padding:0; background:#faf8ff; border-bottom:1px solid #e9ecef; }}
  .dp-inner {{ padding:14px 20px; display:flex; flex-direction:column; gap:12px; }}

  .msg-block {{ border-radius:8px; overflow:hidden; position:relative; }}
  .exec-block {{ border:1.5px solid #f39c12; }}
  .port-block {{ border:1.5px solid #2980b9; }}

  .msg-label {{ padding:8px 14px; font-size:10px; font-weight:700; letter-spacing:.8px;
                text-transform:uppercase; }}
  .exec-block .msg-label {{ background:#fff8f0; color:#e67e22; }}
  .port-block .msg-label {{ background:#f0f8ff; color:#2980b9; }}

  .msg-body {{ padding:12px 14px; font-size:13px; line-height:1.8; color:#2c3e50;
               background:#fff; min-height:60px; }}

  .copy-btn {{ position:absolute; top:8px; right:10px; background:#fff;
               border:1px solid #dee2e6; border-radius:6px; padding:3px 12px;
               font-size:11px; font-weight:600; color:#555; cursor:pointer;
               transition:all .15s; }}
  .copy-btn:hover {{ background:#f1f3f5; }}
  .copy-btn.copied {{ background:#27ae60; color:#fff; border-color:#27ae60; }}

  .draft-note {{ font-size:10px; color:#adb5bd; text-align:right; }}

  .upd {{ text-align:center; font-size:11px; color:#adb5bd; margin-top:20px;
          border-top:1px solid #e9ecef; padding-top:14px; }}
</style>
</head>
<body>
<div class="wrap">
  <div class="topbar">
    <h1>📱 커뮤니케이션 초안 | Communication Drafts</h1>
    <div class="meta">🔴 즉시검토 시그널 대상 · AI 생성 초안 · 발송 전 반드시 검토 필요</div>
  </div>

  <div class="guide">
    💡 <b>사용 방법:</b> 각 행을 클릭하면 초안이 펼쳐집니다. <b>복사</b> 버튼으로 카카오톡·이메일에 바로 붙여넣기 가능합니다.
  </div>

  <table>
    <thead>
      <tr>
        <th>포트폴리오사</th>
        <th>신호 유형</th>
        <th>주요 내용</th>
        <th></th>
      </tr>
    </thead>
    <tbody>
      {rows}
    </tbody>
  </table>

  <div class="upd">
    생성 시각: {generated_at} &nbsp;·&nbsp;
    Portfolio Intelligence Agent &nbsp;·&nbsp;
    본 초안은 AI가 자동 생성한 커뮤니케이션 템플릿입니다.
  </div>
</div>
<script>
  function toggle(i) {{
    const panel = document.getElementById('panel-' + i);
    const arr   = document.getElementById('arr-' + i);
    const row   = document.getElementById('row-' + i);
    const open  = panel.style.display !== 'none';
    panel.style.display = open ? 'none' : 'table-row';
    arr.textContent     = open ? '▶' : '▼';
    row.style.background = open ? '' : '#f5f0ff';
  }}
  function copyMsg(text, btn) {{
    navigator.clipboard.writeText(text).then(() => {{
      btn.textContent = '✓ 복사됨';
      btn.classList.add('copied');
      setTimeout(() => {{ btn.textContent = '복사'; btn.classList.remove('copied'); }}, 2000);
    }});
  }}
</script>
</body></html>"""


def save_drafts(drafts_data: list[dict],
                path: str = "drafts.html") -> str:
    """커뮤니케이션 초안 인터랙티브 HTML 저장."""
    from datetime import timezone as _tz, timedelta as _td
    _KST = _tz(_td(hours=9))
    generated_at = datetime.now(_KST).strftime("%Y-%m-%d %H:%M KST")
    html = build_drafts_html(drafts_data, generated_at)
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    logger.info(f"[Drafts] 커뮤니케이션 초안 페이지 저장 → {path}")
    return path


# =============================================================================
# 단독 실행 (테스트용)
# =============================================================================
if __name__ == "__main__":
    from collector import Collector
    from classifier_groq import Classifier

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")

    articles = Collector().run()
    signals  = Classifier().run(articles)
    disp     = Dispatcher()

    disp.send_telegram_alerts(signals)
    disp.send_daily_email(signals)
    print("배포 완료")
