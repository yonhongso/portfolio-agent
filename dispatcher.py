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
            "Neutral": "➡️ 중립", "Mixed": "↕️ 혼조",
        }
        sentiment_label = sentiment_map.get(signal.sentiment, signal.sentiment)
        relevance_star  = {"High": "★★★", "Medium": "★★☆", "Low": "★☆☆"}.get(
            signal.relevance, signal.relevance)

        # 국내(KR) → 한국어, 해외 → 영어만 표시
        summary_ko = (signal.summary_ko or "").strip()
        summary_en = (signal.summary_en or "").strip()
        if summary_ko and summary_en:
            summary_line = f"🇰🇷 {summary_ko}\n🇺🇸 <i>{summary_en}</i>"
        elif summary_ko:
            summary_line = f"🇰🇷 {summary_ko}"
        elif summary_en:
            summary_line = f"🇺🇸 <i>{summary_en}</i>"
        else:
            summary_line = signal.title[:60]

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
                smtp.sendmail(self.from_addr, to, msg.as_string())
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
  body { font-family: 'Segoe UI', -apple-system, BlinkMacSystemFont, Arial, sans-serif;
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

def _company_section_html(company: str,
                          signals: list["ClassifiedSignal"]) -> str:
    """L2: 회사 헤딩 (한 번만) + L3 기사 카드 목록. 회사명 카드 내 중복 없음."""
    cards_html = "\n".join(_signal_card_html(s) for s in signals)
    return f"""
    <div class="co-group">
      <div class="co-heading">
        <span class="co-name">{company}</span>
        <span class="co-cnt">{len(signals)}건</span>
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
    summary_ko = (s.summary_ko or "").strip()
    summary_en = (s.summary_en or "").strip()
    headline = summary_ko or summary_en or s.title[:60]

    # 출처 단축
    src_raw = s.source or ""
    src_short = (src_raw.replace("www.", "").split(".")[0][:16] + " →") if src_raw else "원문 →"

    return f"""
    <div class="art-card {flag}">
      <div class="art-top">
        <span class="sig-badge" style="background:{bg};color:{tc}">{s.signal_type}</span>
        <a href="{url}" target="_blank" class="art-src">{src_short}</a>
      </div>
      <div class="art-hl">{headline}</div>
      <div class="art-acts">
        <div class="act-impl">💡 {impl}</div>
        <div class="act-do">⚡ {action}</div>
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
        red_lines    = [f"🔴 {s.portfolio_name} — {s.summary_ko[:40]}…" for s in reds[:3]]
        yellow_lines = [f"🟡 {s.portfolio_name} — {s.summary_ko[:40]}…" for s in yellows[:2]]
        exec_msg  = "\n".join(red_lines + yellow_lines)
        exec_en   = "\n".join([f"· {s.portfolio_name}: {(s.summary_en or s.summary_ko)[:45]}…" for s in reds[:3]])
        exec_bg   = "#fdf5f5"; exec_border = "#e74c3c"; exec_label_color = "#c0392b"
    elif yellows:
        yellow_lines = [f"🟡 {s.portfolio_name} — {s.summary_ko[:40]}…" for s in yellows[:4]]
        exec_msg  = "\n".join(yellow_lines)
        exec_en   = "\n".join([f"· {s.portfolio_name}: watch & report" for s in yellows[:4]])
        exec_bg   = "#fdfbf0"; exec_border = "#f39c12"; exec_label_color = "#d68910"
    else:
        exec_msg  = "⚪ 오늘 포트폴리오 전반 특이사항 없음 — 정기모니터링 유지"
        exec_en   = "· No significant signals today. Routine monitoring continues."
        exec_bg   = "#f0fdf4"; exec_border = "#27ae60"; exec_label_color = "#27ae60"

    # ── 시그널 카드 — 회사별 묶음 렌더링
    from collections import defaultdict as _dd
    visible = [s for s in signals if s.action_flag in ("red", "yellow")]
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
        f'<div class="white-note">⚪ 정기모니터링 {len(whites)}건 — 첨부 PDF 참조 &nbsp;|&nbsp; '
        f'{len(whites)} routine item(s) included in the attached PDF report.</div>'
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
{_HTML_STYLE}
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
                 model: str = "claude-sonnet-4-6",
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
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": model,
                "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=40,
        )
        resp.raise_for_status()
        return resp.json()["content"][0]["text"].strip()
    except Exception as e:
        logger.warning(f"[Claude] API 호출 실패: {e}")
        return ""


_WEEKLY_INSIGHT_PROMPT = """당신은 벤처캐피탈 사업개발팀의 시니어 투자 분석가입니다.
아래는 지난 1주일간 포트폴리오사에서 수집된 주요 시그널 목록입니다.

{signals_json}

다음 형식으로 한국어 주간 종합 인사이트를 작성하세요 (총 300자 이내):
1. 이번 주 가장 중요한 포트폴리오 이슈 (1~2문장)
2. 투자팀이 이번 주 취해야 할 핵심 액션 (2~3개, 구체적으로)
3. 다음 주 주시해야 할 리스크 포인트 (1~2개)

JSON 없이 자연스러운 문단 형식으로 작성하세요."""


_MONTHLY_INSIGHT_PROMPT = """당신은 벤처캐피탈 사업개발팀의 수석 포트폴리오 분석가입니다.
아래는 지난 1개월간 포트폴리오사에서 수집된 시그널 목록입니다.

{signals_json}

다음 구조로 월간 심층 분석 보고서를 한국어로 작성하세요:

[이달의 포트폴리오 총평]
(3~4문장: 전반적 포트폴리오 건전성, 주요 이슈, 시장 맥락)

[Top 3 핵심 이슈 및 투자 시사점]
1. {회사명}: (이슈 요약 + 투자 관점 시사점)
2. {회사명}: (이슈 요약 + 투자 관점 시사점)
3. {회사명}: (이슈 요약 + 투자 관점 시사점)

[다음 달 중점 모니터링 과제]
(2~3개 항목)

[경영층 보고 핵심 메시지 (1문장)]

총 500자 이내. JSON 없이 작성."""


def _generate_weekly_insight(signals: list) -> str:
    """Claude Sonnet으로 주간 종합 인사이트 생성. 실패 시 기본 문구 반환."""
    reds    = [s for s in signals if s.action_flag == "red"]
    yellows = [s for s in signals if s.action_flag == "yellow"]
    key_signals = sorted(reds + yellows,
                         key=lambda x: x.source_tier)[:8]
    if not key_signals:
        return "이번 주 주요 이슈 없음 — 정기 모니터링 유지."

    signals_json = json.dumps([{
        "company":     s.portfolio_name,
        "signal_type": s.signal_type,
        "flag":        s.action_flag,
        "summary_ko":  s.summary_ko,
        "sentiment":   s.sentiment,
    } for s in key_signals], ensure_ascii=False, indent=2)

    result = _call_claude(
        _WEEKLY_INSIGHT_PROMPT.format(signals_json=signals_json),
        model="claude-sonnet-4-6",
        max_tokens=600,
    )
    if result:
        logger.info("[Claude] 주간 인사이트 생성 완료")
        return result

    # Groq 폴백
    logger.info("[Claude→Groq] 주간 인사이트 폴백")
    try:
        from classifier_groq import _call
        return _call(_WEEKLY_INSIGHT_PROMPT.format(signals_json=signals_json)) or                "주간 인사이트 생성 실패 — 수동 검토 필요."
    except Exception:
        return "주간 인사이트 생성 실패 — 수동 검토 필요."


def _generate_monthly_insight(signals: list, year: int, month: int) -> str:
    """Claude Sonnet으로 월간 심층 분석 생성. 실패 시 기본 문구 반환."""
    key_signals = sorted(signals,
        key=lambda x: ({"red":0,"yellow":1,"white":2}[x.action_flag],
                       x.source_tier))[:12]
    if not key_signals:
        return f"{year}년 {month}월 주요 포트폴리오 이슈 없음."

    signals_json = json.dumps([{
        "company":     s.portfolio_name,
        "signal_type": s.signal_type,
        "flag":        s.action_flag,
        "summary_ko":  s.summary_ko,
        "summary_en":  s.summary_en,
        "sentiment":   s.sentiment,
        "source":      s.source,
    } for s in key_signals], ensure_ascii=False, indent=2)

    result = _call_claude(
        _MONTHLY_INSIGHT_PROMPT.format(signals_json=signals_json),
        model="claude-sonnet-4-6",
        max_tokens=1200,
    )
    if result:
        logger.info("[Claude] 월간 심층 분석 생성 완료")
        return result

    # Groq 폴백
    logger.info("[Claude→Groq] 월간 분석 폴백")
    try:
        from classifier_groq import _call
        return _call(_MONTHLY_INSIGHT_PROMPT.format(signals_json=signals_json)) or                "월간 분석 생성 실패 — 수동 검토 필요."
    except Exception:
        return "월간 분석 생성 실패 — 수동 검토 필요."


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
{_HTML_STYLE}
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
{_HTML_STYLE}
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
- 발신: 사업개발팀 (투자사 측)
- 수신: {company} 대표 또는 CFO
- 분량: 5~7줄
- 톤: 투자 파트너십 기반, 우호적이되 사실 확인 목적 명확히
- 구성: ① 인사·발신자 소개 ② 언론 보도 내용 구체적 언급 ③ 사실 관계 확인 요청 (투자사 관점에서 중요한 1~2가지 포인트 명시) ④ 가능하다면 미팅/콜 제안 ⑤ 지원 의사 표명

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
        f"안녕하세요 대표님, 사업개발팀입니다.\n\n"
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
        reds = [s for s in signals if s.action_flag == "red"]
        logger.info(f"[Telegram] 🔴 즉시 알림 대상: {len(reds)}건")
        for s in reds:
            self.telegram.send_signal(s)

    # ── Daily 이메일 (HTML 대시보드 + PDF 첨부)
    def send_daily_email(self, signals: list[ClassifiedSignal]):
        if not self.cfg["dispatch"]["email_daily"]["enabled"]:
            return
        signals  = deduplicate_signals(signals)          # ← 반드시 제일 먼저
        dcfg     = self.cfg["dispatch"]["email_daily"]
        date_str = datetime.now().strftime("%Y-%m-%d")
        reds     = [s for s in signals if s.action_flag == "red"]
        yellows  = [s for s in signals if s.action_flag == "yellow"]
        whites   = [s for s in signals if s.action_flag == "white"]

        subject = dcfg["subject_template"].format(
            date=date_str,
            red_count=len(reds),
            yellow_count=len(yellows),
        )

        # 🔴 시그널별 커뮤니케이션 초안 생성 (Groq)
        drafts_data = []
        for s in reds:
            msg_exec, msg_portfolio = _draft_contact_messages(s)
            drafts_data.append({
                "portfolio_name": s.portfolio_name,
                "signal_type":    s.signal_type,
                "summary_ko":     s.summary_ko,
                "msg_exec":       msg_exec,
                "msg_portfolio":  msg_portfolio,
            })

        html = build_daily_html(signals, date_str, drafts_data=drafts_data or None)

        # dashboard.html에 커뮤니케이션 초안 탭 포함하여 저장
        save_dashboard(signals, drafts_data=drafts_data or None)

        attachments = []

        # PDF 첨부 (경영층 보고용)
        pdf_data = build_daily_pdf(signals, date_str)
        if pdf_data:
            attachments.append((
                f"Portfolio_Report_{date_str}.pdf",
                pdf_data,
                "application/pdf",
            ))

        # ⚪ CSV 첨부
        if whites:
            attachments.append((
                f"참고기사_{date_str}.csv",
                build_white_csv(whites),
                "text/csv",
            ))

        self.email.send(dcfg["recipients"], subject, html,
                        attachments if attachments else None)

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
  body {{ font-family:-apple-system,'Segoe UI',sans-serif;color:#1a1a2e;
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
        ai_insight = _generate_monthly_insight(signals, now.year, now.month)
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
        red_lines   = [f"🔴 {s.portfolio_name} — {s.summary_ko[:40]}…" for s in reds[:3]]
        yellow_lines= [f"🟡 {s.portfolio_name} — {s.summary_ko[:40]}…" for s in yellows[:2]]
        exec_msg  = "\n".join(red_lines + yellow_lines)
        red_en    = [f"· {s.portfolio_name}: {(s.summary_en or s.summary_ko)[:45]}…" for s in reds[:3]]
        exec_en   = "\n".join(red_en)
        exec_bg   = "#fdf5f5"; exec_border = "#e74c3c"; exec_label_color = "#c0392b"
    elif yellows:
        yellow_lines= [f"🟡 {s.portfolio_name} — {s.summary_ko[:40]}…" for s in yellows[:4]]
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
            f'<div class="flag-section {cls}">'
            f'{label} <span style="font-size:10px;font-weight:500;opacity:.7">{sub}</span>'
            f'<span class="count-pill">{count}건</span></div>'
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
        f'<div class="white-note">⚪ 정기모니터링 {len(whites)}건 — 첨부 PDF 참조 &nbsp;|&nbsp; '
        f'{len(whites)} routine item(s) included in the attached PDF report.</div>'
    ) if whites else ""

    # ── Tab2: 커뮤니케이션 초안
    draft_tab_content = ""
    if drafts_data:
        draft_rows = ""
        for i, d in enumerate(drafts_data):
            co   = d.get("portfolio_name", "")
            sig  = d.get("signal_type", "")
            summ = (d.get("summary_ko") or "")[:55] + "…"
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

    drafts_badge = (f'<span style="background:#c0392b;color:#fff;font-size:11px;'
                    f'border-radius:20px;padding:2px 10px;margin-left:8px;vertical-align:middle">'
                    f'{len(drafts_data)}건</span>') if drafts_data else ""

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Portfolio Intelligence Dashboard</title>
{_HTML_STYLE}
<style>
  /* ── 대시보드 전용 오버라이드 */
  body {{ background:#edf0f4 }}
  .wrapper {{ max-width:900px }}

  /* ── 탑바 */
  .topbar {{
    background:linear-gradient(150deg,#0d1b2a,#1a2744);
    border-radius:12px 12px 0 0;
    padding:22px 28px;
    display:flex; justify-content:space-between; align-items:center;
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
      📱 커뮤니케이션 초안{drafts_badge}
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


def save_dashboard(signals: list[ClassifiedSignal],
                   drafts_data: Optional[list[dict]] = None,
                   path: str = "dashboard.html") -> str:
    """대시보드 HTML 파일 저장 후 경로 반환."""
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M")
    html = build_dashboard_html(signals, generated_at, drafts_data=drafts_data)
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    logger.info(f"[Dashboard] 저장 완료 → {path}")
    return path


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
        summ     = (d.get("summary_ko") or "")[:60] + "…"
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
  body {{ font-family:'Segoe UI',-apple-system,sans-serif; background:#edf0f4; color:#1a1a2e; }}
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
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M")
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
