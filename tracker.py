# -*- coding: utf-8 -*-
"""
13F 트래커
- SEC EDGAR에서 등록 펀드들의 13F-HR 공시를 가져와 직전 분기 대비 변동을 계산하고
  텔레그램으로 발송한다.
- 신규 편입/청산뿐 아니라 보유 주식수 증감률, 포트폴리오 비중(%)과 비중 변화(pp),
  put/call 여부까지 반영한다.

사용법:
  python tracker.py --dry           # 전체 펀드 처리, 텔레그램 대신 stdout 출력
  python tracker.py --full          # 전체 펀드 처리 후 텔레그램 발송
  python tracker.py --check         # 새 공시가 있는 펀드만 발송 (cron용)
  python tracker.py --fund 1536411  # 특정 CIK만

환경변수:
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID  (발송 시 필수)
  GEMINI_API_KEY                        (있으면 시사점 코멘트 생성)
  GEMINI_MODEL                          (기본 gemini-2.5-flash)
"""
import argparse
import csv
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import requests

# Windows 콘솔(cp949)에서 이모지 출력 깨짐 방지
for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
STATE_FILE = DATA_DIR / "state.json"


def _load_env():
    """같은 폴더의 .env 파일을 환경변수로 로드 (이미 설정된 값은 유지)."""
    env = BASE_DIR / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env()

HEADERS = {"User-Agent": "Personal research soheeji77@gmail.com"}
EDGAR_SLEEP = 0.25  # SEC fair-use: 초당 요청 수 제한


# ---------------------------------------------------------------- EDGAR fetch

def edgar_get(url, **kw):
    time.sleep(EDGAR_SLEEP)
    r = requests.get(url, headers=HEADERS, timeout=60, **kw)
    r.raise_for_status()
    return r


def get_filings(cik):
    """해당 CIK의 13F-HR(및 정정) 목록을 [(period, filed, accession), ...] 최신순으로."""
    url = f"https://data.sec.gov/submissions/CIK{cik:010d}.json"
    j = edgar_get(url).json()
    recent = j["filings"]["recent"]
    rows = []
    for form, acc, filed, period in zip(
        recent["form"], recent["accessionNumber"],
        recent["filingDate"], recent["reportDate"],
    ):
        if form in ("13F-HR", "13F-HR/A"):
            rows.append({"form": form, "accession": acc,
                         "filed": filed, "period": period})
    # 분기(period)별로 가장 나중에 제출된 것(정정 반영)을 채택
    by_period = {}
    for r in rows:
        cur = by_period.get(r["period"])
        if cur is None or r["filed"] >= cur["filed"]:
            by_period[r["period"]] = r
    return [by_period[p] for p in sorted(by_period, reverse=True)], j.get("name", "")


def get_holdings(cik, accession):
    """infotable XML을 찾아 보유내역 dict 리스트로 파싱."""
    acc_nodash = accession.replace("-", "")
    idx_url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc_nodash}/index.json"
    idx = edgar_get(idx_url).json()
    xmls = [f["name"] for f in idx["directory"]["item"]
            if f["name"].lower().endswith(".xml")]
    # primary_doc(표지)이 아닌 쪽이 infotable
    cands = [x for x in xmls if "primary_doc" not in x.lower()] or xmls
    holdings = []
    for name in cands:
        url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc_nodash}/{name}"
        text = edgar_get(url).text
        if "infoTable" not in text and "infotable" not in text:
            continue
        holdings = parse_infotable(text)
        if holdings:
            break
    if not holdings:
        # 폴백: 일부 필러는 infotable을 별도 XML 없이 마스터 제출파일(.txt)에 내장
        url = (f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc_nodash}/"
               f"{accession}.txt")
        text = edgar_get(url).text
        for block in re.findall(r"<XML>(.*?)</XML>", text, re.S | re.I):
            if "infoTable" in block or "infotable" in block:
                holdings = parse_infotable(block.strip())
                if holdings:
                    break
    return holdings


def _local(tag):
    return tag.rsplit("}", 1)[-1].lower()


def parse_infotable(xml_text):
    root = ET.fromstring(xml_text)
    out = []
    for node in root.iter():
        if _local(node.tag) != "infotable":
            continue
        rec = {"issuer": "", "cusip": "", "value": 0.0,
               "shares": 0.0, "shtype": "SH", "putcall": ""}
        for child in node.iter():
            t = _local(child.tag)
            v = (child.text or "").strip()
            if t == "nameofissuer":
                rec["issuer"] = v
            elif t == "cusip":
                rec["cusip"] = v.upper()
            elif t == "value":
                rec["value"] = float(v or 0)
            elif t == "sshprnamt":
                rec["shares"] = float(v or 0)
            elif t == "sshprnamttype":
                rec["shtype"] = v.upper() or "SH"
            elif t == "putcall":
                rec["putcall"] = v.strip().upper()
        if rec["cusip"]:
            out.append(rec)
    return out


# ------------------------------------------------------------------ 분석

def normalize_units(holdings):
    """value 단위 자동 판별(달러 vs 천달러).

    2023년 규정상 달러 단위이지만 천달러로 내는 필러가 여전히 있다.
    주식(SH) 보유분의 내재가격(value/shares) 중앙값이 비정상적으로 낮으면
    천달러로 보고 1000을 곱한다.
    """
    prices = sorted(h["value"] / h["shares"] for h in holdings
                    if h["shtype"] == "SH" and h["shares"] > 0 and h["value"] > 0)
    if prices:
        median = prices[len(prices) // 2]
        if median < 3:  # 대형 기관 포트가 평균 주가 $3 미만일 수는 없음
            for h in holdings:
                h["value"] *= 1000
    return holdings


def aggregate(holdings):
    """(cusip, putcall) 단위로 합산. 반환: {key: {issuer, value, shares, putcall}}"""
    holdings = normalize_units(holdings)
    agg = {}
    for h in holdings:
        key = (h["cusip"], h["putcall"])
        a = agg.setdefault(key, {"issuer": h["issuer"], "value": 0.0,
                                 "shares": 0.0, "putcall": h["putcall"],
                                 "cusip": h["cusip"]})
        a["value"] += h["value"]
        a["shares"] += h["shares"]
        if len(h["issuer"]) > len(a["issuer"]):
            a["issuer"] = h["issuer"]
    return agg


def diff_quarters(cur, prev):
    """직전 분기 대비 변동 계산."""
    cur_total = sum(a["value"] for a in cur.values()) or 1.0
    prev_total = sum(a["value"] for a in prev.values()) or 1.0
    rows = []
    for key, c in cur.items():
        p = prev.get(key)
        w_cur = c["value"] / cur_total * 100
        if p is None:
            rows.append({"kind": "NEW", "issuer": c["issuer"], "putcall": c["putcall"],
                         "cusip": c["cusip"], "value": c["value"], "shares": c["shares"],
                         "share_chg": None, "w_cur": w_cur, "w_prev": 0.0,
                         "w_pp": w_cur})
        else:
            w_prev = p["value"] / prev_total * 100
            chg = None
            if p["shares"] > 0:
                chg = (c["shares"] - p["shares"]) / p["shares"] * 100
            if chg is not None and abs(chg) < 0.5:
                kind = "KEEP"
            elif chg is not None and chg > 0:
                kind = "ADD"
            elif chg is not None:
                kind = "TRIM"
            else:
                kind = "KEEP"
            rows.append({"kind": kind, "issuer": c["issuer"], "putcall": c["putcall"],
                         "cusip": c["cusip"], "value": c["value"], "shares": c["shares"],
                         "share_chg": chg, "w_cur": w_cur, "w_prev": w_prev,
                         "w_pp": w_cur - w_prev})
    for key, p in prev.items():
        if key not in cur:
            w_prev = p["value"] / prev_total * 100
            rows.append({"kind": "EXIT", "issuer": p["issuer"], "putcall": p["putcall"],
                         "cusip": p["cusip"], "value": 0.0, "shares": 0.0,
                         "share_chg": -100.0, "w_cur": 0.0, "w_prev": w_prev,
                         "w_pp": -w_prev})
    return rows, cur_total, prev_total


# ------------------------------------------------------------------ 메시지

def fmt_usd(v):
    if v >= 1e9:
        return f"${v/1e9:.1f}B"
    if v >= 1e6:
        return f"${v/1e6:.0f}M"
    return f"${v/1e3:.0f}K"


def short_name(issuer):
    s = re.sub(r"\b(INC|CORP|CO|LTD|PLC|CL A|CL B|CL C|COM|NEW|DEL|HLDGS|HOLDINGS)\b\.?",
               "", issuer.upper())
    return re.sub(r"\s+", " ", s).strip().title()[:24]


def opt_tag(putcall):
    return f" [{putcall}]" if putcall else ""


def build_message(fund, cur_f, prev_f, rows, cur_total, prev_total):
    q = cur_f["period"][:7].replace("-", ".")
    lines = [f"🐋 13F | {fund['name']} ({fund['tag']})",
             f"기준 {q} 분기말 · 공시 {cur_f['filed']}",
             f"운용규모 {fmt_usd(cur_total)} (전분기 {fmt_usd(prev_total)}) · "
             f"보유 {sum(1 for r in rows if r['kind'] != 'EXIT')}종목"]

    def section(title, kinds, sort_key, n=5, fmt=None):
        sel = sorted([r for r in rows if r["kind"] in kinds],
                     key=sort_key, reverse=True)
        if not sel:
            return
        lines.append("")
        lines.append(title)
        for r in sel[:n]:
            lines.append(fmt(r))
        if len(sel) > n:
            lines.append(f"  … 외 {len(sel)-n}건")

    section("🆕 신규 편입", {"NEW"}, lambda r: r["w_cur"],
            fmt=lambda r: f"· {short_name(r['issuer'])}{opt_tag(r['putcall'])} "
                          f"— 비중 {r['w_cur']:.1f}% ({fmt_usd(r['value'])})")
    section("🗑 전량 청산", {"EXIT"}, lambda r: r["w_prev"],
            fmt=lambda r: f"· {short_name(r['issuer'])}{opt_tag(r['putcall'])} "
                          f"— 전분기 비중 {r['w_prev']:.1f}%")
    section("📈 확대 (주식수 기준)", {"ADD"}, lambda r: r["w_pp"],
            fmt=lambda r: f"· {short_name(r['issuer'])}{opt_tag(r['putcall'])} "
                          f"주식수 {r['share_chg']:+.0f}% → 비중 {r['w_cur']:.1f}% "
                          f"({r['w_pp']:+.1f}pp)")
    section("📉 축소 (주식수 기준)", {"TRIM"}, lambda r: -r["w_pp"],
            fmt=lambda r: f"· {short_name(r['issuer'])}{opt_tag(r['putcall'])} "
                          f"주식수 {r['share_chg']:+.0f}% → 비중 {r['w_cur']:.1f}% "
                          f"({r['w_pp']:+.1f}pp)")

    top = sorted([r for r in rows if r["kind"] != "EXIT"],
                 key=lambda r: r["w_cur"], reverse=True)[:5]
    lines.append("")
    lines.append("🏆 현재 상위 보유")
    for r in top:
        lines.append(f"· {short_name(r['issuer'])}{opt_tag(r['putcall'])} "
                     f"{r['w_cur']:.1f}%")
    return "\n".join(lines)


# ------------------------------------------------------------------ 시사점(LLM)

def gemini_comment(fund, message):
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        return None
    # 무료 쿼터(모델×프로젝트당 일 20회) 대비 모델 폴백 체인 (CODEX 봇들과 동일 순서)
    models = [os.environ.get("GEMINI_MODEL", "gemini-3.6-flash"),
              "gemini-flash-latest", "gemini-3.1-flash-lite"]
    prompt = (
        "아래는 미국 헤지펀드의 분기 13F 보유변동 요약이다. "
        "한국 기관투자자 관점에서 이 변동의 시사점을 2~3문장, 한국어로 요약하라. "
        "섹터 로테이션, 반도체/AI 방향성, 행동주의 캠페인 가능성 등 실전적 관점 위주로. "
        "과장 없이 담백하게. 13F는 분기말 기준 45일 지연 공시라는 한계는 언급하지 마라.\n\n"
        + message
    )
    body = {"contents": [{"parts": [{"text": prompt}]}]}
    for model in models:
        url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
               f"{model}:generateContent?key={key}")
        try:
            r = requests.post(url, json=body, timeout=60)
            r.raise_for_status()
            return r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
        except Exception as e:
            print(f"  [warn] Gemini({model}) 실패: {e}", file=sys.stderr)
    return None


# ------------------------------------------------------------------ 발송/저장

def send_telegram(text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        raise SystemExit("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 환경변수가 필요합니다")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    # 4096자 제한 대비 분할, 타임아웃 시 1회 재시도
    for i in range(0, len(text), 4000):
        body = {"chat_id": chat, "text": text[i:i+4000],
                "disable_web_page_preview": True}
        try:
            requests.post(url, json=body, timeout=60).raise_for_status()
        except requests.exceptions.Timeout:
            time.sleep(3)
            requests.post(url, json=body, timeout=60).raise_for_status()


def save_csv(fund, period, rows):
    DATA_DIR.mkdir(exist_ok=True)
    path = DATA_DIR / f"{fund['cik']}_{period}.csv"
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["kind", "issuer", "cusip", "putcall",
                                          "shares", "share_chg", "value",
                                          "w_prev", "w_cur", "w_pp"])
        w.writeheader()
        for r in sorted(rows, key=lambda x: x["w_cur"], reverse=True):
            w.writerow({k: r[k] for k in w.fieldnames})
    return path


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def save_state(state):
    DATA_DIR.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=1), encoding="utf-8")


# ------------------------------------------------------------------ main

def process_fund(fund, dry):
    filings, edgar_name = get_filings(fund["cik"])
    if len(filings) < 2:
        print(f"  [skip] {fund['name']}: 공시 2건 미만")
        return None
    cur_f, prev_f = filings[0], filings[1]
    print(f"  {fund['name']} ({edgar_name}): {prev_f['period']} → {cur_f['period']}"
          f" (공시 {cur_f['filed']})")
    cur = aggregate(get_holdings(fund["cik"], cur_f["accession"]))
    prev = aggregate(get_holdings(fund["cik"], prev_f["accession"]))
    if not cur or not prev:
        print(f"  [skip] {fund['name']}: infotable 파싱 실패")
        return None
    rows, cur_total, prev_total = diff_quarters(cur, prev)
    msg = build_message(fund, cur_f, prev_f, rows, cur_total, prev_total)
    comment = gemini_comment(fund, msg)
    if comment:
        msg += "\n\n💬 시사점\n" + comment
    save_csv(fund, cur_f["period"], rows)
    if dry:
        print("\n" + msg + "\n" + "─" * 40)
    else:
        send_telegram(msg)
    return cur_f["accession"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="텔레그램 대신 stdout")
    ap.add_argument("--full", action="store_true", help="전체 펀드 강제 처리")
    ap.add_argument("--check", action="store_true", help="새 공시만 (cron)")
    ap.add_argument("--fund", type=int, help="특정 CIK만")
    args = ap.parse_args()

    funds = json.loads((BASE_DIR / "funds.json").read_text(encoding="utf-8"))
    if args.fund:
        funds = [f for f in funds if f["cik"] == args.fund]
    state = load_state()

    for fund in funds:
        cik = str(fund["cik"])
        try:
            if args.check:
                filings, _ = get_filings(fund["cik"])
                if not filings:
                    continue
                if state.get(cik) == filings[0]["accession"]:
                    print(f"  {fund['name']}: 변동 없음")
                    continue
            acc = process_fund(fund, dry=args.dry)
            if acc and not args.dry:  # dry-run은 state를 남기지 않는다
                state[cik] = acc
                save_state(state)
        except Exception as e:
            print(f"  [error] {fund['name']}: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
