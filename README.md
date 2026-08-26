# 13F Tracker

미국 주요 헤지펀드 12곳의 분기 13F 공시를 SEC EDGAR에서 수집해, 직전 분기 대비
**신규 편입 / 전량 청산 / 주식수 증감 / 비중(pp) 변화 / put·call 구분**을 계산하고
텔레그램으로 발송한다. GEMINI_API_KEY가 있으면 변동 시사점 코멘트를 붙인다.

## 대상 펀드
[funds.json](funds.json) — Berkshire, Duquesne, Pershing Square, Appaloosa, Baupost,
Greenlight, Coatue, Altimeter, Lone Pine, Elliott, Starboard, Third Point.
추가/삭제는 funds.json에 CIK만 넣으면 된다 (CIK는 EDGAR company search에서 확인).

## 실행
```
python tracker.py --dry            # 텔레그램 없이 콘솔 출력 (전체)
python tracker.py --dry --fund 1536411   # 특정 CIK만
python tracker.py --full           # 전체 발송
python tracker.py --check          # 새 공시가 뜬 펀드만 발송 (cron용)
```

## 운영
- GitHub Actions가 매일 07:30 KST에 `--check` 실행. 13F 마감 시즌(2/15·5/15·8/14·11/16 전후)에만
  실제 발송이 발생하고 평소에는 "변동 없음"으로 지나간다.
- 처리한 공시 accession은 `data/state.json`에 기록하고 커밋해 중복 발송을 막는다.
- 종목별 상세 diff는 `data/{cik}_{분기말}.csv`로 저장된다.

## Secrets (repo Settings > Secrets and variables > Actions)
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` — 필수
- `GEMINI_API_KEY` — 선택 (시사점 코멘트)

## 구현 메모
- value 단위는 규정상 달러이지만 천달러로 내는 필러가 있어, 주식형(SH) 보유분의
  내재가격 중앙값이 $3 미만이면 천달러로 판단해 1000을 곱한다 (`normalize_units`).
- 정정공시(13F-HR/A)는 같은 분기 내 최신 제출본으로 자동 대체된다.
- "확대/축소"는 평가액이 아니라 **주식수 기준**이다. 평가액 변동은 주가 등락이 섞여
  실제 매매와 다르기 때문.
- 13F 한계: 분기말 기준 45일 지연, 미국 상장 롱 포지션만 (공매도·해외주식 미포함).
