"""ステップの並びの欄の高さ (入口の札が全部見える) と、同じワークフローを同じ台に寄せる緩い重み付けのテスト。
APPDATA を一時フォルダに差し替えるので、実際の設定には触れない。

    python tests\\test_layout_affinity.py
"""

import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="prismlab_layout_"))
os.environ["APPDATA"] = str(TMP / "appdata")
os.environ["QT_QPA_PLATFORM"] = os.environ.get("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(ROOT.parent / "GenCommon"))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtGui import QColor, QImage  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from fakes import FakeComfy, workflow_cn, workflow_i2v  # noqa: E402
from gencommon import comfy, store  # noqa: E402
from prismlab import engine, line_view, profile, recipes, theme  # noqa: E402
from prismlab.main_window import MainWindow  # noqa: E402

app = QApplication(sys.argv)
theme.apply(app)
profile.setup()
assert str(store.APP_DIR).startswith(str(TMP))
ok = []


def check(cond, label):
    assert cond, label
    ok.append(label)
    print("OK  ", label)


def pump(seconds: float):
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        time.sleep(0.03)


WF = TMP / "wf"
WF.mkdir()
(WF / "Clone I2I CN.json").write_text(json.dumps(workflow_cn()), encoding="utf-8")
(WF / "Wan I2V.json").write_text(json.dumps(workflow_i2v()), encoding="utf-8")
I2I, I2V = str(WF / "Clone I2I CN.json"), str(WF / "Wan I2V.json")

# 1. ステップの並びの欄は、入口の札が全部見える高さ (★の内訳で伸びても縦のスクロールバーを出さない)
folder = TMP / "入口"
folder.mkdir()
for n in range(12):
    img = QImage(40, 30, QImage.Format.Format_RGB32)
    img.fill(QColor(30 * (n % 8), 100, 160))
    img.save(str(folder / f"img{n}.png"))
engine.rating.get_stars = lambda p: int(Path(p).stem[3:]) % 6  # ★0〜5 をばらばらに (内訳が長くなるように)
line_view.rating.get_stars = engine.rating.get_stars
recipes.save([recipes.Recipe("高さ", folder=str(folder), stages=[recipes.Stage(recipes.GEN, workflow=I2I)])])
recipes.set_last_index(0)
window = MainWindow()
window.resize(1360, 900)
window.show()
lv = window.line
end = time.time() + 10
while time.time() < end and "★" not in lv.breakdown.text():
    pump(0.2)
pump(0.5)
entry = lv.entry
view = lv.flow_scroll.viewport()
check("★" in lv.breakdown.text(), f"★の内訳を出す ({lv.breakdown.text()[:40]}…)")
check(entry.height() >= entry.minimumSizeHint().height(), f"入口の札は縮めない ({entry.height()} / {entry.minimumSizeHint().height()})")
check(not lv.flow_scroll.verticalScrollBar().isVisible(), "縦のスクロールバーを出さない")
check(entry.geometry().bottom() <= view.height(), f"入口の札が全部見える ({entry.geometry().bottom()} / {view.height()})")
check(lv.flow_scroll.height() >= line_view.FLOW_MIN_HEIGHT, f"欄の高さ {lv.flow_scroll.height()}")
check(lv.results.height() > 100, f"結果の欄も残る ({lv.results.height()})")
window.grab().save(str(TMP / "layout.png"))
small = lv.flow_scroll.height()
window.resize(1360, 420)
pump(0.4)
check(lv.flow_scroll.height() <= max(line_view.FLOW_MIN_HEIGHT, lv.height() // 2), "低い窓では窓の半分まで")
window.resize(1360, 900)
pump(0.4)
check(lv.flow_scroll.height() == small, "窓を戻せば高さも戻る")
window.close()


# 2. 同じワークフローを同じ台に寄せる (緩く)
def make_run(kinds_):
    stages = [{"type": recipes.GEN, "workflow": I2I}, {"type": recipes.GEN, "workflow": I2V}]
    items = [engine.Item(f"s{n}", stage=k) for n, k in enumerate(kinds_)]
    return engine.Run({"name": "寄せ"}, stages, str(TMP / "out"), items)


eng = engine.Engine(run_file=None)
eng.run = make_run([0, 1, 0, 1])  # 画像・動画・画像・動画 の順に待っている
url = "http://machine-a"
check(eng._pick(True, url) is eng.run.items[0], "まだ何も動かしていない台は先頭から")
engine._set_last_workflow(url, I2V)
picked = eng._pick(True, url)
check(picked is eng.run.items[1] and eng._passed is eng.run.items[0], "動画を動かした台は、待っている動画を先に取る")
check(eng._pick(True, "http://machine-b") is eng.run.items[0], "ほかの台は先頭から")
eng.run.items[0].skips = engine.AFFINITY_MAX_SKIPS
check(eng._pick(True, url) is eng.run.items[0], f"先頭を {engine.AFFINITY_MAX_SKIPS} 回後回しにしたら、次は先頭を取る")
eng.run = make_run([0] * engine.AFFINITY_LOOKAHEAD + [1])
check(eng._pick(True, url) is eng.run.items[0], f"先頭から {engine.AFFINITY_LOOKAHEAD} 件より後ろは見ない")
eng.run = make_run([0, 1])
eng.run.items[0].machine = url  # この台に頼み済み (続きを待つ)
check(eng._pick(True, url) is eng.run.items[0], "頼み済みの件は寄せより先")
check("skips" not in engine.Item("x").to_dict(), "後回しの回数は保存しない")

# 実際に回す: 1 台で 画像 → 動画 を 4 件。寄せると、画像・動画の切り替えが少なくなる
a = FakeComfy(polls=2)
comfy.set_connections([comfy.Preset("FALCON", a.url)] + [comfy.Preset() for _ in range(comfy.PRESET_COUNT - 1)])
src = sorted(folder.glob("*.png"))[:4]
r = recipes.Recipe("寄せ", folder=str(folder), stages=[recipes.Stage(recipes.GEN, workflow=I2I),
                                                     recipes.Stage(recipes.GEN, workflow=I2V)])
eng = engine.Engine(run_file=None)
eng.set_machines([a.url])
eng.start(engine.Run.create(r, src))
end = time.time() + 60
while time.time() < end and not all(i.finished for i in eng.run.items):
    pump(0.1)
check(all(i.state == engine.DONE for i in eng.run.items), "全部できる")
order = ["I" if "SaveVideo" not in json.dumps(wf) else "V" for wf in a.received]
switches = sum(1 for x, y in zip(order, order[1:]) if x != y)
check(switches <= 3, f"切り替えは少なめ ({''.join(order)} 切り替え {switches} 回)")

print(f"\nすべて OK ({len(ok)})  画像: {TMP}")
