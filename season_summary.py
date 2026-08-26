# -*- coding: utf-8 -*-
"""
13F 시즌 종합 시사점 — data/에 저장된 최신 분기 diff CSV들을 모아
펀드 전체를 관통하는 테마를 Gemini로 요약해 1건의 메시지로 발송한다.

사용법: python season_summary.py [--dry]
(분기 시즌이 끝난 뒤 1회 실행하는 용도)
"""
import argparse
import csv
import json
import sys
from collections import defaultdict
from pathlib import Path

from tracker import BASE_DIR, DATA_DIR, gemini_comment, send_telegram

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")


def latest_csv(cik):
    files = sorted(DATA_DIR.glob(f"{cik}_*.csv"))
    return files[-1] if files else None


def fund_digest(fund, path):
    rows = list(csv.DictReader(open(path, encoding="utf-8-sig")))
    for r in rows:
        for k in ("w_cur", "w_prev", "w_pp"):
            r[k] = float(r[k] or 0)

    def fmt(r, note=""):
        tag = f"[{r['putcall']}]" if r["putcall"] else ""
        return f"{r['issuer']}{tag}{note}"

    parts = []
    news = sorted([r for r in rows if r["kind"] == "NEW"],
                  key=lambda r: -r["w_cur"])[:3]
    exits = sorted([r for r in rows if r["kind"] == "EXIT"],
                   key=lambda r: -r["w_prev"])[:3]
    moves = sorted([r for r in rows if r["kind"] in ("ADD", "TRIM")],
                   key=lambda r: -abs(r["w_pp"]))[:3]
    tops = sorted([r for r in rows if r["kind"] != "EXIT"],
                  key=lambda r: -r["w_cur"])[:5]
    if news:
        parts.append("신규: " + ", ".join(fmt(r, f"({r['w_cur']:.1f}%)") for r in news))
    if exits:
        parts.append("청산: " + ", ".join(fmt(r) for r in exits))
    if moves:
        parts.append("증감: " + ", ".join(fmt(r, f"({r['w_pp']:+.1f}pp)") for r in moves))
    parts.append("상위보유: " + ", ".join(fmt(r, f"{r['w_cur']:.0f}%") for r in tops))
    return f"### {fund['name']} ({fund['tag']})\n" + "\n".join(parts)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()

    funds = json.loads((BASE_DIR / "funds.json").read_text(encoding="utf-8"))
    digests, period = [], ""
    for fund in funds:
        path = latest_csv(fund["cik"])
        if not path:
            continue
        period = path.stem.split("_", 1)[1]
        digests.append(fund_digest(fund, path))
    if not digests:
        raise SystemExit("data/에 diff CSV가 없습니다. tracker를 먼저 실행하세요.")

    prompt_body = "\n\n".join(digests)
    fake_fund = {"name": "종합", "tag": "시즌"}
    comment = gemini_comment(fake_fund, (
        f"아래는 미국 주요 헤지펀드 {len(digests)}곳의 {period} 분기말 기준 13F "
        "보유변동 요약이다. 개별 펀드 나열이 아니라 **전체를 관통하는 테마**를 "
        "한국 기관투자자 관점에서 한국어로 정리하라. 형식: '■ 소제목' + 2~3문장 "
        "단락 4~6개. 다룰 것: AI/반도체 방향성(확대냐 차익실현이냐), 섹터 로테이션, "
        "헤지 포지션(지수 PUT 등) 확대 여부, 행동주의 신규 타깃, 눈에 띄는 역발상 "
        "베팅. 과장 없이 담백하게.\n\n" + prompt_body))
    if not comment:
        raise SystemExit("Gemini 요약 실패 — 쿼터/키 확인")
    comment = comment.replace("**", "")  # 텔레그램은 일반 텍스트 발송

    q = period[:7].replace("-", ".")
    msg = (f"🐋 13F 시즌 종합 시사점 ({q} 분기말, {len(digests)}개 펀드)\n\n"
           + comment)
    if args.dry:
        print(msg)
    else:
        send_telegram(msg)
        print("발송 완료")


if __name__ == "__main__":
    main()
