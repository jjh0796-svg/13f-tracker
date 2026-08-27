# -*- coding: utf-8 -*-
"""
안녕하재 증시데이터 — 대화형 조회봇 (오라클 서버 상주)

명령:
  /13f 버크셔        — 펀드명 일부로 최근 분기 13F 요약 조회
  /펀드목록          — 조회 가능한 펀드 12곳
  /수출             — 최신 수출입 잠정치 메시지 재조회 (trade-pulse 실행, ~15초)
  /도움말

- 데이터: 서버의 13f-tracker clone(data/*.csv, 조회 전 git pull로 최신화)
- 보안: TELEGRAM_CHAT_ID 지정 대화만 응답
- 운영: systemd databot.service (죽으면 자동 재시작)
"""
import csv
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import requests

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = Path(__file__).resolve().parent
TRADE_DIR = Path(os.environ.get("TRADE_PULSE_DIR",
                                str(Path.home() / "trade-pulse")))
VENV_PY = str(Path.home() / "venv" / "bin" / "python")


def load_env():
    p = Path.home() / "bots" / "databot.env"
    if p.exists():
        for line in p.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())


HELP = (
    "📊 증시데이터 조회 명령\n"
    "/13f 버크셔 — 펀드명 일부로 최근 분기 13F 요약\n"
    "/펀드목록 — 조회 가능한 펀드\n"
    "/수출 — 최신 수출입 잠정치 (약 15초 소요)\n"
    "\n정기 발송(13F 시즌·수출입·마감스캔)은 평소처럼 자동으로 옵니다."
)


def funds():
    return json.loads((BASE_DIR / "funds.json").read_text(encoding="utf-8"))


def latest_csv(cik):
    files = sorted((BASE_DIR / "data").glob(f"{cik}_*.csv"))
    return files[-1] if files else None


def summarize_fund(fund):
    path = latest_csv(fund["cik"])
    if not path:
        return f"{fund['name']}: 저장된 분기 데이터가 없습니다."
    period = path.stem.split("_", 1)[1]
    rows = list(csv.DictReader(open(path, encoding="utf-8-sig")))
    for r in rows:
        for k in ("w_cur", "w_prev", "w_pp", "value"):
            r[k] = float(r[k] or 0)
    total = sum(r["value"] for r in rows)
    unit = total / 1e9

    def fmt(r, note):
        pc = f"[{r['putcall']}]" if r["putcall"] else ""
        return f"· {r['issuer'][:22]}{pc} {note}"

    lines = [f"🐋 {fund['name']} ({fund['tag']})",
             f"기준 {period} · 운용규모 ${unit:,.1f}B · "
             f"{sum(1 for r in rows if r['kind'] != 'EXIT')}종목"]
    tops = sorted([r for r in rows if r["kind"] != "EXIT"],
                  key=lambda r: -r["w_cur"])[:5]
    lines += ["", "🏆 상위 보유"] + [fmt(r, f"{r['w_cur']:.1f}%") for r in tops]
    news = sorted([r for r in rows if r["kind"] == "NEW"],
                  key=lambda r: -r["w_cur"])[:3]
    if news:
        lines += ["", "🆕 신규"] + [fmt(r, f"{r['w_cur']:.1f}%") for r in news]
    exits = sorted([r for r in rows if r["kind"] == "EXIT"],
                   key=lambda r: -r["w_prev"])[:3]
    if exits:
        lines += ["", "🗑 청산"] + [fmt(r, f"(전분기 {r['w_prev']:.1f}%)")
                                    for r in exits]
    moves = sorted([r for r in rows if r["kind"] in ("ADD", "TRIM")],
                   key=lambda r: -abs(r["w_pp"]))[:3]
    if moves:
        lines += ["", "↕️ 주요 증감"] + [
            fmt(r, f"{r['w_pp']:+.1f}pp → {r['w_cur']:.1f}%") for r in moves]
    return "\n".join(lines)


def handle(text):
    parts = text.strip().split()
    if not parts:
        return None
    cmd = parts[0].lstrip("/").lower()
    # 텔레그램 메뉴 버튼용 영문 별칭 (메뉴 등록은 영문만 허용)
    cmd = {"f13": "13f", "funds": "펀드목록", "trade": "수출"}.get(cmd, cmd)

    if cmd in ("start", "도움말", "help"):
        return HELP

    if cmd == "펀드목록":
        return "조회 가능한 펀드:\n" + "\n".join(
            f"· {f['name']} ({f['tag']})" for f in funds())

    if cmd == "13f":
        if len(parts) < 2:
            return "형식: /13f 펀드명일부 (예: /13f 버크셔)"
        # 최신 데이터로 갱신 (깃헙 Actions가 커밋한 분기 CSV)
        subprocess.run(["git", "-C", str(BASE_DIR), "pull", "-q"],
                       capture_output=True, timeout=60)
        key = " ".join(parts[1:]).casefold()
        alias = {"버크셔": "berkshire", "버핏": "berkshire", "아인혼": "dme",
                 "그린라이트": "dme", "드러켄밀러": "duquesne", "애크먼": "pershing",
                 "테퍼": "appaloosa", "클라만": "baupost", "엘리엇": "elliott",
                 "서드포인트": "third"}
        key = alias.get(key, key)
        hits = [f for f in funds() if key in f["name"].casefold()]
        if not hits:
            return f"'{' '.join(parts[1:])}' 와 맞는 펀드가 없습니다. /펀드목록 참고"
        return summarize_fund(hits[0])

    if cmd in ("수출", "수출입"):
        try:
            r = subprocess.run([VENV_PY, "pulse.py", "--dry"],
                               cwd=str(TRADE_DIR), capture_output=True,
                               text=True, timeout=120)
            out = (r.stdout or "").strip()
            return out if out else f"조회 실패: {(r.stderr or '')[-200:]}"
        except Exception as e:
            return f"조회 오류: {e}"

    return None


def main():
    load_env()
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    allowed = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not allowed:
        sys.exit("~/bots/databot.env 에 TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID 필요")
    api = f"https://api.telegram.org/bot{token}"
    offset = 0
    print("query_bot 시작")
    while True:
        try:
            r = requests.get(f"{api}/getUpdates",
                             params={"offset": offset, "timeout": 50},
                             timeout=60).json()
            for u in r.get("result", []):
                offset = u["update_id"] + 1
                msg = u.get("message") or {}
                chat_id = str((msg.get("chat") or {}).get("id", ""))
                text = msg.get("text", "")
                if chat_id != str(allowed) or not text:
                    continue
                reply = handle(text)
                if reply:
                    requests.post(f"{api}/sendMessage",
                                  json={"chat_id": chat_id, "text": reply[:4000]},
                                  timeout=20)
        except Exception as e:
            print(f"[warn] {e}")
            time.sleep(10)


if __name__ == "__main__":
    main()
