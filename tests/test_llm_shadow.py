# 섀도 판단 스크립트 순수 함수 검증(네트워크·Gemini 호출은 모두 mock)
"""실행: python tests/test_llm_shadow.py"""
import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tools.llm_shadow import (
    _COMBO_PROMPT_VERSION, _MODEL, _MODEL_STAGE2, _PROMPT_VERSION, _parent_permits, build_combo_prompt,
    build_prompt, build_reflection_note, build_snapshot, call_gemini, fetch_bars, is_notable, judge_combo,
    judge_from_bars, parse_judgment, run_once,
)

_BARS = [{"close": 50000 + i * 100, "high": 50200 + i * 100, "low": 49800 + i * 100, "volume": 100000}
         for i in range(70)]


def main() -> int:
    snap = build_snapshot(_BARS)
    assert snap is not None, "70봉이면 스냅샷 생성돼야 함"
    assert snap["close"] == _BARS[-1]["close"]
    assert snap["sma5"] is not None and snap["sma20"] is not None and snap["sma60"] is not None
    assert snap["rsi14"] == 100.0, "단조 상승 시리즈면 RSI=100"

    # 지표 히스토리 — 단일 시점 숫자만으론 방향성을 못 봐서 오판이 잦다는 피드백 반영(최근 4거래일 추이)
    assert len(snap["rsi14_history"]) == 4 and snap["rsi14_history"][-1] == snap["rsi14"], "히스토리 마지막=오늘"
    assert len(snap["rvol20_history"]) == 4 and snap["rvol20_history"][-1] == snap["rvol20"]
    assert len(snap["bollinger_pct_history"]) == 4 and snap["bollinger_pct_history"][-1] == snap["bollinger_pct"]
    assert all(v == 100.0 for v in snap["rsi14_history"]), "단조 상승이면 히스토리 전부 RSI=100"

    assert build_snapshot(_BARS[:10]) is None, "봉 부족(30미만)이면 None"

    # is_notable — 규칙 기반 사전 필터(유니버스 스캔 전용, Gemini 호출 없음)
    assert is_notable({"rsi14": 100.0, "rvol20": 1.0, "bollinger_pct": 0.5}), "RSI 과매수(>=70)면 특이점"
    assert is_notable({"rsi14": 25.0, "rvol20": 1.0, "bollinger_pct": 0.5}), "RSI 과매도(<=30)면 특이점"
    assert is_notable({"rsi14": 50.0, "rvol20": 3.0, "bollinger_pct": 0.5}), "거래량 급증(>=2.0)이면 특이점"
    assert is_notable({"rsi14": 50.0, "rvol20": 1.0, "bollinger_pct": 0.02}), "볼린저 밴드 하단 이탈 근접"
    assert is_notable({"rsi14": 50.0, "rvol20": 1.0, "bollinger_pct": 0.98}), "볼린저 밴드 상단 이탈 근접"
    assert not is_notable({"rsi14": 50.0, "rvol20": 1.0, "bollinger_pct": 0.5}), "전부 중립이면 특이점 아님"
    assert not is_notable({"rsi14": None, "rvol20": None, "bollinger_pct": None}), "값 없으면 특이점 아님(방어)"

    # _parent_permits — ③ 콤보 관찰 상위 프레임 게이트(AND 조건 — 둘 다 만족해야 허가, 하나라도 없으면 차단)
    assert _parent_permits({"close": 105, "sma20": 100, "rsi14": 50}), "종가>=sma20 이고 RSI 정상이면 허가"
    assert not _parent_permits({"close": 95, "sma20": 100, "rsi14": 50}), "종가<sma20(하락 추세)이면 차단"
    assert not _parent_permits({"close": 105, "sma20": 100, "rsi14": 15}), "RSI<20(패닉)이면 차단"
    assert _parent_permits({"close": 100, "sma20": 100, "rsi14": 20}), "경계값(==)은 허가"
    assert not _parent_permits({"close": None, "sma20": 100, "rsi14": 50}), "값 없으면 보수적으로 차단"
    assert not _parent_permits({"close": 105, "sma20": None, "rsi14": 50}), "값 없으면 보수적으로 차단"
    assert not _parent_permits({"close": 105, "sma20": 100, "rsi14": None}), "값 없으면 보수적으로 차단"

    prompt = build_prompt("005930", {"close": 50000})
    assert "005930" in prompt and '"close":50000' in prompt, "JSON 압축 직렬화(공백 없음)로 포함돼야 함"
    assert "market_context_analysis" in prompt and "counter_argument" in prompt, "CoT 필드 요청 포함돼야 함"

    # 되먹임(reflection) — 없으면(None, 기본값) 프롬프트가 byte-identical 이어야 기존 테스트가 안 깨짐
    assert build_prompt("005930", {"close": 50000}, reflection=None) == prompt, "reflection=None 이면 기존과 동일"
    prompt_r = build_prompt("005930", {"close": 50000}, reflection="참고: 테스트 노트")
    assert "참고: 테스트 노트" in prompt_r and prompt_r != prompt

    combo_prompt_no_r = build_combo_prompt("005930", "daily", {"close": 70000}, "60m", {"close": 70100})
    assert build_combo_prompt("005930", "daily", {"close": 70000}, "60m", {"close": 70100},
                              reflection=None) == combo_prompt_no_r, "콤보도 reflection=None 이면 기존과 동일"
    combo_prompt_r = build_combo_prompt("005930", "daily", {"close": 70000}, "60m", {"close": 70100},
                                        reflection="참고: 콤보 노트")
    assert "참고: 콤보 노트" in combo_prompt_r and combo_prompt_r != combo_prompt_no_r

    assert parse_judgment('{"action":"buy","confidence":0.7,"reason":"상승추세"}') == {
        "action": "buy", "confidence": 0.7, "reason": "상승추세",
        "market_context_analysis": "", "counter_argument": ""}, "CoT 필드 없어도(구버전 응답) 기본값으로 채움"
    assert parse_judgment(
        '{"market_context_analysis":"상승 우세","counter_argument":"거래량 부족","action":"hold",'
        '"confidence":0.5,"reason":"중립"}'
    ) == {
        "action": "hold", "confidence": 0.5, "reason": "중립",
        "market_context_analysis": "상승 우세", "counter_argument": "거래량 부족"}
    assert parse_judgment('```json\n{"action":"hold","confidence":0.5,"reason":"중립"}\n```') == {
        "action": "hold", "confidence": 0.5, "reason": "중립",
        "market_context_analysis": "", "counter_argument": ""}, "마크다운 코드펜스 제거"
    assert parse_judgment('{"action":"buy_now","confidence":0.9}') is None, "허용 안 된 action → None"
    assert parse_judgment("garbage") is None, "JSON 아니면 None"

    # call_gemini — 일시 오류(429) 1회 후 재시도로 성공
    ok_resp = MagicMock(text='{"action":"sell","confidence":0.4,"reason":"과매수"}')
    mock_client = MagicMock()
    mock_client.models.generate_content.side_effect = [Exception("429 rate limited"), ok_resp]
    with patch("google.genai.Client", return_value=mock_client), patch("tools.llm_shadow.time.sleep"):
        result = call_gemini("prompt", "fake-key")
    assert result == {"action": "sell", "confidence": 0.4, "reason": "과매수",
                       "market_context_analysis": "", "counter_argument": ""}, "429 이후 재시도로 성공해야 함"
    assert mock_client.models.generate_content.call_count == 2

    # call_gemini — model 파라미터 미지정 시 기본(_MODEL), 지정 시 그대로 전달(유니버스 필터 통과분→_MODEL_STAGE2)
    mock_client3 = MagicMock()
    mock_client3.models.generate_content.return_value = MagicMock(
        text='{"action":"hold","confidence":0.5,"reason":"x"}')
    with patch("google.genai.Client", return_value=mock_client3):
        call_gemini("prompt", "fake-key")
        assert mock_client3.models.generate_content.call_args.kwargs["model"] == _MODEL
        call_gemini("prompt", "fake-key", model=_MODEL_STAGE2)
        assert mock_client3.models.generate_content.call_args.kwargs["model"] == _MODEL_STAGE2

    # call_gemini — thinking_budget=0 을 항상 명시(실측: gemini-3-flash-preview/pro 는 응답에 안 보이는
    # thinking 토큰을 출력 단가로 청구해 월 예산을 크게 초과시켰음 — 0으로 꺼서 비용만 제거)
    with patch("google.genai.Client", return_value=mock_client3):
        call_gemini("prompt", "fake-key")
        cfg = mock_client3.models.generate_content.call_args.kwargs["config"]
        assert cfg.thinking_config.thinking_budget == 0, "thinking 토큰 비용을 막으려면 항상 0이어야 함"

    # call_gemini — 비일시적 오류(예: 인증 실패)는 재시도 없이 즉시 raise
    mock_client2 = MagicMock()
    mock_client2.models.generate_content.side_effect = Exception("401 invalid api key")
    with patch("google.genai.Client", return_value=mock_client2), patch("tools.llm_shadow.time.sleep"):
        try:
            call_gemini("prompt", "fake-key")
            assert False, "비일시적 오류는 raise 되어야 함"
        except Exception as e:
            assert "401" in str(e)
    assert mock_client2.models.generate_content.call_count == 1, "재시도 없이 1회만 호출돼야 함"

    # run_once — 레코드에 forward_returns 계산용 entry_price·trade_date 포함
    fake_bars = [{"date": f"202601{(i % 28) + 1:02d}", "close": 50000 + i * 10,
                  "high": 50200 + i * 10, "low": 49800 + i * 10, "volume": 100000} for i in range(60)]
    with patch("tools.llm_shadow.daily_ohlcv", return_value=fake_bars), \
         patch("tools.llm_shadow.call_gemini", return_value={"action": "buy", "confidence": 0.6, "reason": "상승"}):
        rec = run_once("005930", lookback_days=60, api_key="fake-key")
    assert rec is not None
    assert rec["code"] == "005930" and rec["action"] == "buy"
    assert rec["trade_date"] == fake_bars[-1]["date"]
    assert rec["entry_price"] == fake_bars[-1]["close"], "entry_price는 스냅샷 종가와 같아야 함"
    assert "snapshot" in rec and "ts" in rec
    assert rec["model"] == _MODEL, "model 미지정 시 기본(저비용) 모델이 레코드에 남아야 함"

    # run_once — model 을 넘기면(유니버스 필터 통과분 전용) call_gemini 에 그대로 전달되고 레코드에도 남음
    with patch("tools.llm_shadow.daily_ohlcv", return_value=fake_bars), \
         patch("tools.llm_shadow.call_gemini", return_value={"action": "buy", "confidence": 0.9, "reason": "y"}) as mock_gemini:
        rec_pro = run_once("005930", lookback_days=60, api_key="fake-key", model=_MODEL_STAGE2)
    assert rec_pro["model"] == _MODEL_STAGE2
    assert mock_gemini.call_args[0][2] == _MODEL_STAGE2, "call_gemini 세 번째 위치 인자로 model 전달돼야 함"

    # run_once — 봉 부족이면 None(Gemini 호출 자체를 안 함)
    with patch("tools.llm_shadow.daily_ohlcv", return_value=fake_bars[:10]), \
         patch("tools.llm_shadow.call_gemini") as mock_gemini:
        assert run_once("005930", lookback_days=60, api_key="fake-key") is None
        mock_gemini.assert_not_called()

    # run_once — last_trade_date 가 최신 봉과 같으면 Gemini 호출 없이 스킵(중복 호출 방지)
    with patch("tools.llm_shadow.daily_ohlcv", return_value=fake_bars), \
         patch("tools.llm_shadow.call_gemini", return_value={"action": "buy", "confidence": 0.5, "reason": "x"}) as mock_gemini:
        same = run_once("005930", lookback_days=60, api_key="fake-key", last_trade_date=fake_bars[-1]["date"])
        assert same is None
        mock_gemini.assert_not_called()
        changed = run_once("005930", lookback_days=60, api_key="fake-key", last_trade_date="19990101")
        assert changed is not None
        mock_gemini.assert_called_once()

    # judge_from_bars — run_once 와 동일 로직을 봉을 직접 받아 수행(캐시 재사용 경로, ai_universe_scan.py 용)
    with patch("tools.llm_shadow.call_gemini", return_value={"action": "hold", "confidence": 0.5, "reason": "y"}) as mock_gemini:
        rec = judge_from_bars("005930", fake_bars, api_key="fake-key")
        assert rec is not None and rec["trade_date"] == fake_bars[-1]["date"]
        assert judge_from_bars("005930", fake_bars, "fake-key", last_trade_date=fake_bars[-1]["date"]) is None
        assert mock_gemini.call_count == 1, "dedup 스킵 시 Gemini 재호출 없어야 함"

    # prompt_version — 저장용 snapshot 에만 태깅되고, Gemini 에 보내는 프롬프트엔 안 섞여야 함
    with patch("tools.llm_shadow.call_gemini", return_value={"action": "hold", "confidence": 0.5, "reason": "z"}) as mock_gemini:
        rec = judge_from_bars("005930", fake_bars, api_key="fake-key")
        assert rec["snapshot"]["_prompt_version"] == _PROMPT_VERSION
        prompt_arg = mock_gemini.call_args[0][0]
        assert "_prompt_version" not in prompt_arg, "버전 태그가 Gemini 프롬프트에 섞이면 안 됨"

    # ── build_reflection_note — 과거 판단 이력 → 프롬프트용 되먹임 문구(순수 함수) ──
    assert build_reflection_note([]) is None, "이력 없으면 None"
    assert build_reflection_note([{"action": "buy", "ret_d5": 0.02} for _ in range(4)]) is None, \
        "min_n(기본 5) 미만이면 None(초기 노이즈 방지)"
    # 미평가(ret_d5=None, pending) 행은 표본에서 제외 — evaluated 3건뿐이면 min_n 미달
    pending_mix = [{"action": "buy", "ret_d5": 0.02} for _ in range(3)] + \
                  [{"action": "buy", "ret_d5": None} for _ in range(5)]
    assert build_reflection_note(pending_mix) is None, "평가완료 3건<min_n(5) — pending 은 표본 아님"
    pending_mix_ok = [{"action": "buy", "ret_d5": 0.02} for _ in range(5)] + \
                     [{"action": "buy", "ret_d5": None} for _ in range(3)]
    note_ok = build_reflection_note(pending_mix_ok)
    assert note_ok is not None and "5건" in note_ok, "pending 을 빼고도 평가완료 5건이면 통과"

    # sell 이 많고 가격이 '올랐다'(=매도 판단이 틀렸다) — 부호반전 없이 raw 집계하면 승률이 100%처럼
    # 보이는 함정이 있음(summarize_actions 가 sell 부호를 반전해 "판단이 맞았는가"로 통일해줘야 함).
    sell_wrong = [{"action": "sell", "ret_d5": 0.03} for _ in range(6)]
    note_sell = build_reflection_note(sell_wrong, min_n=5)
    assert note_sell is not None and "6건" in note_sell
    assert "0%" in note_sell, "매도 후 가격이 올랐으니(판단 실패) 부호반전 적용 시 승률 0%여야 함"
    assert "-3.0" in note_sell, "부호반전 적용 시 평균수익도 음수(-3.0%)여야 함"

    # hold 행은 방향적 베팅이 아니므로 집계에서 완전히 제외돼야 함(섞여도 결과가 그대로여야 함)
    mixed_with_hold = sell_wrong + [{"action": "hold", "ret_d5": 0.9} for _ in range(10)]
    assert build_reflection_note(mixed_with_hold, min_n=5) == note_sell, "hold 는 집계에 영향 없어야 함"

    # judge_from_bars — reflection 이 프롬프트에 실리고, snapshot 에 주입 여부(_reflection_injected) 태깅
    with patch("tools.llm_shadow.call_gemini",
               return_value={"action": "hold", "confidence": 0.5, "reason": "r"}) as mock_gemini:
        rec_r = judge_from_bars("005930", fake_bars, api_key="fake-key", reflection="참고: 히스토리 노트")
        assert rec_r is not None
        assert rec_r["snapshot"]["_reflection_injected"] is True
        assert "참고: 히스토리 노트" in mock_gemini.call_args[0][0], "reflection 이 실제 프롬프트에 포함돼야 함"
        rec_no_r = judge_from_bars("005930", fake_bars, api_key="fake-key")
        assert rec_no_r is not None
        assert rec_no_r["snapshot"]["_reflection_injected"] is False, "reflection 미지정 시 플래그 False"

    # fetch_bars — daily 는 naver daily_ohlcv(count=lookback_days), 그 외는 intraday resolve_and_fetch
    # (run_once 가 내부적으로 이 함수를 쓰도록 리팩터됐음 — 위 run_once 테스트들이 그대로 통과하면 행동 보존 확인됨)
    with patch("tools.llm_shadow.daily_ohlcv", return_value=fake_bars) as mock_daily:
        bars = fetch_bars("005930", "daily", lookback_days=77)
        assert bars == fake_bars
        mock_daily.assert_called_once_with("005930", count=77)
    with patch("tools.intraday_ohlcv.resolve_and_fetch", return_value=fake_bars) as mock_intraday:
        bars = fetch_bars("005930", "60m")
        assert bars == fake_bars
        mock_intraday.assert_called_once_with("005930", "60m")

    # ── ③ 콤보 관찰: build_combo_prompt·judge_combo(하위 게이트 제거, 상위 게이트+dedup 만 유지) ──
    parent_prompt = build_combo_prompt("005930", "daily", {"close": 70000}, "60m", {"close": 70100})
    assert "daily" in parent_prompt and "60m" in parent_prompt
    assert '"close":70000' in parent_prompt and '"close":70100' in parent_prompt
    assert "market_context_analysis" in parent_prompt and "counter_argument" in parent_prompt
    assert "이미 1차로 걸러졌고" not in parent_prompt, "하위 게이트가 없어졌으니 이미 필터링됐다는 거짓 전제가 있으면 안 됨"
    assert "필터도 걸려있지 않으니" in parent_prompt, "하위 프레임은 필터 없이 그대로 넘긴다는 문구가 있어야 함"

    parent_bars_ok = fake_bars  # close 50600, sma20 계산상 종가 우상향이라 close>=sma20 성립(과거 확인된 패턴)
    child_bars = [{"date": f"202601{(i % 28) + 1:02d}", "close": 50000 + i * 10,
                   "high": 50200 + i * 10, "low": 49800 + i * 10, "volume": 10000} for i in range(60)]

    # 상위 프레임 게이트가 막으면(대세 하락) Gemini 호출 자체를 안 함 — 이 안전장치만 유지
    with patch("tools.llm_shadow._parent_permits", return_value=False), \
         patch("tools.llm_shadow.call_gemini") as mock_gemini:
        assert judge_combo("005930", "daily", parent_bars_ok, "60m", child_bars, "fake-key") is None
        mock_gemini.assert_not_called()

    # 상위 게이트만 통과하면(하위 프레임에 특이점이 있든 없든 무관) 바로 Gemini 호출 —
    # is_notable 은 더 이상 호출조차 안 하니 patch 없이도 그대로 통과해야 함
    with patch("tools.llm_shadow._parent_permits", return_value=True), \
         patch("tools.llm_shadow.call_gemini",
               return_value={"action": "buy", "confidence": 0.8, "reason": "z"}) as mock_gemini:
        rec = judge_combo("005930", "daily", parent_bars_ok, "60m", child_bars, "fake-key")
    assert rec is not None
    mock_gemini.assert_called_once()
    assert rec["trade_date"] == child_bars[-1]["date"], "trade_date 는 하위(자식) 프레임 기준"
    assert rec["entry_price"] == child_bars[-1]["close"], "entry_price 는 하위(자식) 프레임 기준"
    assert rec["snapshot"]["_prompt_version"] == _COMBO_PROMPT_VERSION
    assert "parent" in rec["snapshot"] and "child" in rec["snapshot"], "상위·하위 스냅샷이 함께 저장돼야 함"
    assert rec["model"] == _MODEL_STAGE2, "콤보 기본 모델은 _MODEL_STAGE2"

    # 하위 프레임 데이터가 부족하면(30봉 미만) 게이트와 무관하게 None — 데이터 부족 방어는 그대로 유지
    with patch("tools.llm_shadow._parent_permits", return_value=True), \
         patch("tools.llm_shadow.call_gemini") as mock_gemini:
        assert judge_combo("005930", "daily", parent_bars_ok, "60m", child_bars[:10], "fake-key") is None
        mock_gemini.assert_not_called()

    # dedup — 하위(자식) 프레임의 최신 봉 날짜가 last_trade_date 와 같으면 Gemini 호출 자체를 생략(그대로 유지)
    with patch("tools.llm_shadow._parent_permits", return_value=True), \
         patch("tools.llm_shadow.call_gemini") as mock_gemini:
        same = judge_combo("005930", "daily", parent_bars_ok, "60m", child_bars, "fake-key",
                            last_trade_date=child_bars[-1]["date"])
        assert same is None
        mock_gemini.assert_not_called()

    # judge_combo — reflection 도 build_prompt 와 동일하게 프롬프트에 실리고 _reflection_injected 태깅됨
    with patch("tools.llm_shadow._parent_permits", return_value=True), \
         patch("tools.llm_shadow.call_gemini",
               return_value={"action": "buy", "confidence": 0.8, "reason": "z"}) as mock_gemini:
        rec_combo_r = judge_combo("005930", "daily", parent_bars_ok, "60m", child_bars, "fake-key",
                                  reflection="참고: 콤보 히스토리")
        assert rec_combo_r is not None
        assert rec_combo_r["snapshot"]["_reflection_injected"] is True
        assert "참고: 콤보 히스토리" in mock_gemini.call_args[0][0]

    print("✅ test_llm_shadow: build_snapshot(히스토리)·is_notable·_parent_permits·build_prompt(CoT)·"
          "parse_judgment·call_gemini(재시도·model)·run_once·judge_from_bars·prompt_version·fetch_bars·"
          "build_combo_prompt·judge_combo(상위 게이트만+dedup, 하위 게이트 제거)·"
          "build_reflection_note(되먹임, sell 부호반전·hold 제외·pending 제외)·reflection 프롬프트 삽입 통과")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
