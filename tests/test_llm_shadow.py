# 섀도 판단 스크립트 순수 함수 검증(네트워크·Gemini 호출은 모두 mock)
"""실행: python tests/test_llm_shadow.py"""
import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tools.llm_shadow import (
    _MODEL, _MODEL_STAGE2, _PROMPT_VERSION, build_prompt, build_snapshot, call_gemini, is_notable, judge_from_bars,
    parse_judgment, run_once,
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

    prompt = build_prompt("005930", {"close": 50000})
    assert "005930" in prompt and '"close":50000' in prompt, "JSON 압축 직렬화(공백 없음)로 포함돼야 함"
    assert "market_context_analysis" in prompt and "counter_argument" in prompt, "CoT 필드 요청 포함돼야 함"

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

    print("✅ test_llm_shadow: build_snapshot(히스토리)·is_notable·build_prompt(CoT)·parse_judgment·"
          "call_gemini(재시도·model)·run_once·judge_from_bars·prompt_version 통과")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
