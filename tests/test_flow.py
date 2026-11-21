"""ステップのあいだを流れるもの (flow.gaps) のテスト。ワークフローは読まない (views に種類を書く)。

    python tests\\test_flow.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT.parent / "GenCommon"))
sys.path.insert(0, str(ROOT))

from prismlab import flow, recipes  # noqa: E402
from prismlab.flow import MEDIA, MEMO, REF, TEXT  # noqa: E402


def v(t, **kw):
    d = {"type": t, "bypass": False}
    d.update(kw)
    return d


def lanes(g):
    return set(g.lines)


# 読み取り → 盛る → I2V: 画像 → 画像 + 文字 → 文字は生成で消え、動画が出る
g = flow.gaps([v(recipes.READ), v(recipes.ENHANCE), v(recipes.GEN, kind="I2V")])
assert len(g) == 4
assert lanes(g[0]) == {MEDIA} and g[0].lines[MEDIA].new
assert lanes(g[1]) == {MEDIA, TEXT} and g[1].lines[TEXT].new and not g[1].lines[MEDIA].new
assert g[2].lines[TEXT].new  # 盛ると変わる
assert lanes(g[3]) == {MEDIA} and g[3].lines[MEDIA].label == "動画" and g[3].lines[MEDIA].style == "thick"
assert TEXT in g[3].gone and not g[2].lines[MEDIA].cut

# T2I に画像が流れてくると、その前で捨てる。最後まで使われない文字は出力の前で捨てる
g = flow.gaps([v(recipes.GEN, kind="T2I"), v(recipes.TEXT)])
assert g[0].lines[MEDIA].cut
assert g[1].lines[MEDIA].new and g[1].lines[MEDIA].label == "画像"
assert g[2].lines[TEXT].cut

# テキストの入口: 文字だけから始まる
g = flow.gaps([v(recipes.GEN, kind="T2I")], text_entry=True)
assert lanes(g[0]) == {TEXT} and lanes(g[1]) == {MEDIA} and TEXT in g[1].gone

# 覚える → T2I → 思い出す → I2V: メモは思い出すまで流れ、そこで終わる
g = flow.gaps([v(recipes.TEXT), v(recipes.SAVE, memo="a"), v(recipes.GEN, kind="T2I"),
               v(recipes.LOAD, memo="a"), v(recipes.GEN, kind="I2V")], text_entry=True)
assert MEMO not in g[1].lines
assert g[2].lines[MEMO].new and MEMO in g[3].lines and TEXT not in g[3].lines
assert MEMO not in g[4].lines and MEMO in g[4].gone and g[4].lines[TEXT].new
assert lanes(g[5]) == {MEDIA}

# リファレンスは最後の生成まで。増やすは本数を数える。バイパスは何も変えない
g = flow.gaps([v(recipes.REF_IMAGE), v(recipes.SPLIT, count=3), v(recipes.GEN, kind="I2I"),
               v(recipes.READ, bypass=True)])
assert g[1].lines[REF].new and g[2].copies == 3 and REF in g[3].gone and REF not in g[3].lines
assert lanes(g[4]) == lanes(g[3]) and not g[4].lines[MEDIA].new

# 最後のコマ: 動画 → 画像
g = flow.gaps([v(recipes.GEN, kind="I2V"), v(recipes.FRAME)])
assert g[2].lines[MEDIA].label == "画像" and g[2].lines[MEDIA].new
assert "ここを流れるもの" in flow.describe(g[1], "a → b")
print("test_flow: ok")
