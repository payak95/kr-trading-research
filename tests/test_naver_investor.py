# 네이버 frgn(외국인·기관 순매매) 파서 검증(네트워크 없음)
"""실행: python tests/test_naver_investor.py
실제 페이지 구조(2026-07-02 실측, 상승/하락 두 마크업 변형 모두 포함)를 본뜬 합성 HTML로 parse_page 검증."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tools.naver_investor import parse_page

# 실제 페이지 구조 재현 — 1행은 하락(빨간 nv01), 2행은 상승(파란 계열), 등락률 마크업이 서로 달라도
# <td> 레벨 파싱이면 위치가 안 밀리는지 확인하는 게 핵심.
_HTML = """
<table class="type2">
<tr class="title1"><th>날짜</th><th>종가</th></tr>
<tr onMouseOver="mouseOver(this)" onMouseOut="mouseOut(this)">
<td width="62" class="tc"><span class="tah p10 gray03">2026.07.02</span></td>
<td width="67" class="num"><span class="tah p11">286,000</span></td>
<td width="67" class="num"><em class="bu_p bu_pdn"><span class="blind">하락</span></em><span class="tah p11 nv01">28,500</span></td>
<td width="67" class="num"><span class="tah p11 nv01">-9.06%</span></td>
<td width="67" class="num"><span class="tah p11">37,658,279</span></td>
<td width="66" class="num"><span class="tah p11 nv01">-2,166,435</span></td>
<td width="80" class="num"><span class="tah p11 nv01">-5,007,053</span></td>
<td width="76" class="num"><span class="tah p11">2,735,716,865</span></td>
<td width="60" class="num"><span class="tah p11">46.79%</span></td>
</tr>
<tr onMouseOver="mouseOver(this)" onMouseOut="mouseOut(this)">
<td width="62" class="tc"><span class="tah p10 gray03">2026.06.30</span></td>
<td width="67" class="num"><span class="tah p11">334,000</span></td>
<td width="67" class="num"><em class="bu_p bu_up"><span class="blind">상승</span></em><span class="tah p11 red01">11,000</span></td>
<td width="67" class="num"><span class="tah p11 red01">+3.41%</span></td>
<td width="67" class="num"><span class="tah p11">29,237,216</span></td>
<td width="66" class="num"><span class="tah p11 red01">+2,731,943</span></td>
<td width="80" class="num"><span class="tah p11 nv01">-2,627,167</span></td>
<td width="76" class="num"><span class="tah p11">2,745,545,855</span></td>
<td width="60" class="num"><span class="tah p11">46.96%</span></td>
</tr>
</table>
"""


def main() -> int:
    rows = parse_page(_HTML)
    assert len(rows) == 2, f"2행: {len(rows)}"
    # 페이지는 최신순(2026.07.02 먼저) → parse_page 는 시간순으로 뒤집어야 함
    assert [r["date"] for r in rows] == ["20260630", "20260702"], f"시간순: {rows}"
    assert rows[1] == {"date": "20260702", "close": 286000, "volume": 37658279,
                       "orgn_ntby_qty": -2166435, "frgn_ntby_qty": -5007053}, rows[1]
    assert rows[0] == {"date": "20260630", "close": 334000, "volume": 29237216,
                       "orgn_ntby_qty": 2731943, "frgn_ntby_qty": -2627167}, rows[0]
    assert parse_page("garbage") == [], "비정상 입력 → 빈 리스트"
    assert parse_page("") == [], "빈 입력 → 빈 리스트"
    print("✅ test_naver_investor: parse_page(상승/하락 마크업·td레벨 파싱·시간순·비정상입력) 통과")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
