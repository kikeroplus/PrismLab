"""新しいステップ (2026-11-18): 最後のコマ・増やす・★を待つ・置き換え と、追加テキストのワイルドカードのテスト。
偽の ComfyUI (本物の短い mp4 を返す) と偽の LLM を使い、APPDATA を一時フォルダに差し替える (実際の設定に触れない)。

    python tests\\test_steps.py
"""

import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="prismlab_steps_"))
os.environ["APPDATA"] = str(TMP / "appdata")
os.environ["QT_QPA_PLATFORM"] = os.environ.get("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(ROOT.parent / "GenCommon"))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtGui import QColor, QImage  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from fakes import FakeComfy, FakeLLM, make_video, workflow_cn, workflow_i2v, workflow_t2i  # noqa: E402
from gencommon import comfy, enhancer, settings, store  # noqa: E402
from prismlab import engine, profile, recipes, theme  # noqa: E402

app = QApplication(sys.argv)
theme.apply(app)
profile.setup()
assert str(store.APP_DIR).startswith(str(TMP))
QMessageBox.warning = staticmethod(lambda *a, **k: None)
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)

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


def wait(eng, limit=60.0, until=None):
    end = time.time() + limit
    while time.time() < end:
        app.processEvents()
        run = eng.run
        if until is not None and until():
            return
        if until is None and run is not None and all(i.finished for i in run.items):
            pump(0.2)
            return
        time.sleep(0.05)
    raise AssertionError("時間内に終わりません: " + str([(i.state, i.stage, i.error) for i in eng.run.items]))


llm = FakeLLM()
settings.set_llm_url(llm.url)
ENHANCER = next(p.name for p in enhancer.load_presets() if not p.is_empty)
clip = TMP / "clip.mp4"
make_video(clip, [(200, 40, 40)] * 4 + [(40, 60, 220)] * 4, size=(96, 64))  # 赤 4 コマ → 青 4 コマ
a = FakeComfy(polls=1)
a.video_bytes = clip.read_bytes()
WF = TMP / "wf"
WF.mkdir()
for name, wf in (("Wan I2V", workflow_i2v()), ("Clone I2I CN", workflow_cn()), ("Flux T2I", workflow_t2i())):
    (WF / f"{name}.json").write_text(json.dumps(wf), encoding="utf-8")
I2V, I2I, T2I = (str(WF / f"{n}.json") for n in ("Wan I2V", "Clone I2I CN", "Flux T2I"))


def images(folder: Path, n: int) -> list[Path]:
    folder.mkdir(parents=True, exist_ok=True)
    out = []
    for i in range(n):
        img = QImage(48, 32, QImage.Format.Format_RGB32)
        img.fill(QColor(20 * i, 120, 200))
        p = folder / f"img{i + 1}.png"
        img.save(str(p))
        out.append(p)
    return out


def start(stages, sources, name="t", trial=False):
    r = recipes.Recipe(name, folder=str(sources[0].parent), stages=stages)
    eng = engine.Engine(run_file=None)
    eng.set_machines([a.url])
    run = engine.Run.create(r, sources, trial=trial)
    eng.start(run)
    return eng


def sent_texts(since: int) -> list[str]:
    return [wf["2"]["inputs"]["text"] for wf in a.received[since:]]


src = images(TMP / "in", 3)

# 1. ワイルドカード
check(recipes.has_wildcards("a {b|c}") and not recipes.has_wildcards("{plain}"), "| のある {…} だけがワイルドカード")
seen = {recipes.expand_wildcards("{red|blue} hat {x|{y|z}}") for _ in range(200)}
check(all(t.split()[0] in ("red", "blue") and t.split()[-1] in ("x", "y", "z") for t in seen) and len(seen) >= 4,
      f"1 つずつ選ぶ (入れ子も) ({sorted(seen)[:3]}…)")
n0 = len(a.received)
eng = start([recipes.Stage(recipes.TEXT, text="a {red|blue|green} hat"), recipes.Stage(recipes.GEN, workflow=I2I)],
            [src[0]] * 12)
wait(eng)
texts = sent_texts(n0)
check(len(texts) == 12 and all(t in ("a red hat", "a blue hat", "a green hat") for t in texts)
      and len(set(texts)) >= 2, f"1 件ごとに選んで送る ({sorted(set(texts))})")
check(all(it.outputs[0]["adds"][0]["text"] in ("a red hat", "a blue hat", "a green hat") for it in eng.run.items),
      "結果の欄の足した文は、選んだあとの文")

# 2. 置き換え
rules = recipes.parse_rules("masterpiece\nlong hair => short hair\n# memo\nbest quality →\n\n")
check(rules == [("masterpiece", ""), ("long hair", "short hair"), ("best quality", "")], f"決まりを読む ({rules})")
check(recipes.apply_rules("Masterpiece, best quality, a girl, long hair, smile", rules) == "a girl, short hair, smile",
      "大文字・小文字を区別せず置き換え、残った , を詰める")
n0 = len(a.received)
eng = start([recipes.Stage(recipes.TEXT, text="masterpiece, a cat, long hair"),
             recipes.Stage(recipes.ENHANCE, enhancer=ENHANCER),
             recipes.Stage(recipes.SUB, rules="masterpiece\nlong hair => short hair\nenhanced =>"),
             recipes.Stage(recipes.GEN, workflow=I2I)], [src[0]])
wait(eng)
text = sent_texts(n0)[0]
check("masterpiece" not in text.lower() and "short hair" in text and "long hair" not in text
      and "enhanced" not in text, f"盛った文にも置き換えを使う ({text!r})")
try:
    engine.stage_snapshot(recipes.Stage(recipes.SUB, rules="# だけ"))
    check(False, "空の置き換えは始めない")
except ValueError:
    check(True, "空の置き換えは始めない")

# 3. 増やす (差し込みは枝ごとに別のファイル)
ins = TMP / "セリフ"
ins.mkdir()
for n in range(1, 8):
    (ins / f"{n:02d}.txt").write_text(f"line{n}", encoding="utf-8")
calls = len(llm.calls)
n0 = len(a.received)
stages = [recipes.Stage(recipes.TEXT, text="base"), recipes.Stage(recipes.ENHANCE, enhancer=ENHANCER),
          recipes.Stage(recipes.SPLIT, count=3), recipes.Stage(recipes.INSERT, folder=str(ins)),
          recipes.Stage(recipes.GEN, workflow=I2I)]
run_plan = engine.Run.create(recipes.Recipe("plan", folder=str(src[0].parent), stages=stages), src[:2], trial=True)
check(set(run_plan.items[0].plan) == {"3", "3/1", "3/2"}, f"差し込みを枝の数だけ割り当てる ({sorted(run_plan.items[0].plan)})")
eng = start(stages, src[:2])
wait(eng)
items = eng.run.items
check(len(items) == 6 and all(it.state == engine.DONE for it in items), f"2 件 × 3 = 6 件できる ({len(items)})")
check(len(llm.calls) - calls == 2, f"増やす前の盛るは 1 件に 1 回 ({len(llm.calls) - calls})")
lines = [t.splitlines()[-1] for t in sent_texts(n0)]
check(sorted(lines) == [f"line{n}" for n in range(1, 7)], f"差し込みは 1 本ごとに次のファイル ({sorted(lines)})")
check([it.source for it in items] == [str(src[0])] * 3 + [str(src[1])] * 3, "分けたものは元のすぐあとに並ぶ")
check(sorted(it.branch for it in items[:3]) == [0, 1, 2], "枝の番号")

# 4. 最後のコマ
n0 = len(a.received)
eng = start([recipes.Stage(recipes.GEN, workflow=I2V), recipes.Stage(recipes.FRAME),
             recipes.Stage(recipes.GEN, workflow=I2V)], [src[2]], name="続き")
wait(eng)
it = eng.run.items[0]
check(it.state == engine.DONE and len(it.outputs) == 2, f"動画 → コマ → 動画 ({it.state} {it.error})")
frame = Path(eng.run.output_dir) / engine.FRAME_DIR / (Path(it.outputs[0]["path"]).stem + "_last.png")
check(frame.is_file(), "最後のコマを 出力\\コマ に PNG で書く")
img = QImage(str(frame))
check(img.pixelColor(img.width() // 2, img.height() // 2).blue() > 150, "最後のコマ (青) を取り出した")
check(it.outputs[1]["from"] == str(frame), "次の生成はそのコマから")
eng = start([recipes.Stage(recipes.GEN, workflow=I2I), recipes.Stage(recipes.FRAME),
             recipes.Stage(recipes.GEN, workflow=I2V)], [src[2]])
wait(eng)
check(eng.run.items[0].state == engine.FAILED and "動画ではありません" in eng.run.items[0].error,
      "画像のあとの最後のコマは失敗にする")

# 5. ★を待つ
stars: dict = {}
engine.rating.get_stars = lambda path: stars.get(str(path), 0)  # J-Rate で付けた★のかわり
eng = start([recipes.Stage(recipes.GEN, workflow=I2I), recipes.Stage(recipes.GATE, stars=3),
             recipes.Stage(recipes.GEN, workflow=I2V)], src, name="選ぶ")
wait(eng, until=lambda: eng.gated() == 3)
pump(0.5)
check(eng.gated() == 3 and eng.run.unfinished == 3 and eng.active, "★がなければ★待ちで止める (量産は終わらない)")
first = [it.outputs[0]["path"] for it in eng.run.items]
stars[first[0]] = 4
stars[first[1]] = 2
check(eng.recheck_gates() == 1, "★3 以上の 1 件だけ先へ")
wait(eng, until=lambda: eng.run.items[0].state == engine.DONE)
check(len(eng.run.items[0].outputs) == 2 and eng.run.items[1].state == engine.GATED, "★を付けたものは続きを作る")
done_flag = []
eng.finished.connect(lambda: done_flag.append(1))
check(eng.end_gates() == 2, "★待ちを終える")
pump(0.3)
check([it.state for it in eng.run.items] == [engine.DONE, engine.SKIPPED, engine.SKIPPED] and done_flag,
      "★のないものは「★なし」で終わり、量産も終わる")
eng = start([recipes.Stage(recipes.GEN, workflow=I2I), recipes.Stage(recipes.GATE, stars=5),
             recipes.Stage(recipes.GEN, workflow=I2V)], [src[0]], trial=True)
wait(eng)
check(eng.run.items[0].state == engine.DONE and len(eng.run.items[0].outputs) == 2, "試作では★を待たずに通す")
eng = start([recipes.Stage(recipes.GEN, workflow=I2I), recipes.Stage(recipes.GATE, stars=3)], [src[0]])
wait(eng, until=lambda: eng.gated() == 1)
eng.cancel_all()
pump(0.3)
check(eng.run.items[0].state == engine.CANCELED, "中止すると★待ちも取り消す")

# 6. ラインの確かめ
P = recipes.line_problems
check(("needvideo", 1) in P([recipes.Stage(recipes.GEN, workflow=I2I), recipes.Stage(recipes.FRAME)]),
      "画像のあとの最後のコマは赤")
check(not P([recipes.Stage(recipes.GEN, workflow=I2V), recipes.Stage(recipes.FRAME),
             recipes.Stage(recipes.GEN, workflow=I2V)]), "動画のあとなら問題なし")
check(("notext", 0) in P([recipes.Stage(recipes.SUB, rules="a"), recipes.Stage(recipes.GEN, workflow=I2I)]),
      "文がないところの置き換えは赤")
check(("needimage", 0) in P([recipes.Stage(recipes.GATE), recipes.Stage(recipes.GEN, workflow=T2I)], text_entry=True),
      "テキストの入口では、生成の前に★を待てない")
s = recipes.Stage.from_dict({"type": recipes.SPLIT, "count": 99, "stars": 0})
check(s.count == recipes.MAX_SPLIT and s.stars == 1, "読み込むときに数を範囲に収める")

# 7. 画面: 新しいステップの札・設定の窓・★待ちのボタン
from prismlab.dialogs import StageDialog  # noqa: E402
from prismlab.main_window import MainWindow  # noqa: E402

for t in (recipes.FRAME, recipes.SPLIT, recipes.GATE, recipes.SUB):
    st = recipes.Stage(t)
    d = StageDialog(st)
    if t == recipes.SPLIT:
        d.count.setValue(5)
    elif t == recipes.GATE:
        d.stars.setCurrentIndex(3)
    elif t == recipes.SUB:
        d.rules.setPlainText("foo => bar")
    d._accept()
    check(d.result() == 1, f"設定の窓: {recipes.STAGE_TITLES[t]}")
    if t == recipes.SPLIT:
        check(st.count == 5, "分ける数を書く")
    elif t == recipes.GATE:
        check(st.stars == 4, "★の数を書く")
    elif t == recipes.SUB:
        check(st.rules == "foo => bar", "置き換えを書く")
d = StageDialog(recipes.Stage(recipes.SUB))
d._accept()
check(d.result() == 0, "置き換えが空なら OK にしない")

comfy.set_connections([comfy.Preset("FALCON", a.url)] + [comfy.Preset() for _ in range(comfy.PRESET_COUNT - 1)])
comfy.set_workflows([comfy.Preset(Path(p).stem, p) for p in (I2V, I2I, T2I)]
                    + [comfy.Preset() for _ in range(comfy.WORKFLOW_COUNT - 3)])
recipes.set_machines([a.url])
recipes.save([recipes.Recipe("全部", folder=str(src[0].parent), stages=[
    recipes.Stage(recipes.GEN, workflow=I2I), recipes.Stage(recipes.GATE, stars=2),
    recipes.Stage(recipes.TEXT, text="{a|b}"), recipes.Stage(recipes.SUB, rules="a => c"),
    recipes.Stage(recipes.SPLIT, count=2), recipes.Stage(recipes.GEN, workflow=I2V),
    recipes.Stage(recipes.FRAME), recipes.Stage(recipes.GEN, workflow=I2V)])])
recipes.set_last_index(0)
stars.clear()
window = MainWindow()
window.resize(1500, 900)
window.show()
lv = window.line
pump(0.5)
chips = [c.chip.text() for c in lv.stage_cards]
check(chips == ["I2I CN", "★★", "文字", "文字", "×2", "I2V", "コマ", "I2V"], f"札 ({chips})")
check(not any("⚠" in c.io.text() for c in lv.stage_cards), "問題のないラインは赤くしない")
lv._start()
end = time.time() + 30
while time.time() < end and lv.engine.gated() < 3:
    pump(0.2)
pump(0.6)
check(lv.gate_recheck_button.isVisible() and lv.gate_end_button.isVisible(), "★待ちがあればボタンを出す")
check("★待ち 3" in lv.summary.text() and "★待ち 3" in lv.stage_cards[1].count.text(),
      f"★待ちの数を出す ({lv.stage_cards[1].count.text()})")
# 量産中にステップの中身を直す (2026-11-19): 量産の写しとレシピの両方を直し、これから来る件から効く
from prismlab import line_view  # noqa: E402
check(lv.locked(), "量産中のレシピを見ている")
dg = StageDialog(recipes.Stage(recipes.GEN, workflow=I2V), running=True)
check(not dg.workflow.isEnabled() and dg.base.isEnabled(), "量産中の設定の窓では、ワークフローだけ選べない")


class _EditText:
    def __init__(self, stage, parent=None, running=False):
        self.stage, self.running = stage, running

    def exec(self):
        self.stage.text = "zebra"
        return self.running


real_dialog = line_view.StageDialog
line_view.StageDialog = _EditText
lv._edit_stage(2)
line_view.StageDialog = real_dialog
check(lv.engine.run.stages[2]["text"] == "zebra" and lv.recipe.stages[2].text == "zebra"
      and recipes.load()[0].stages[2].text == "zebra", "量産中に追加テキストを直すと、量産の写しとレシピを直す")
check("zebra" in lv.stage_cards[2].io.text() or "zebra" in lv.stage_cards[2].toolTip()
      or any("zebra" in w.text() for w in lv.stage_cards[2].findChildren(type(lv.summary))),
      "直した中身を札に出す")
stars[lv.engine.run.items[0].outputs[0]["path"]] = 5
lv.rescan()  # 戻ったとき (J-Rate で★を付けて)
end = time.time() + 40
while time.time() < end and not (lv.engine.run.items[0].state == engine.DONE and len(lv.engine.run.items) == 4
                                 and lv.engine.run.items[1].state == engine.DONE):
    pump(0.2)
items = lv.engine.run.items
check("zebrc" in items[0].outputs[1]["sent"], f"直したあとに来た件は新しい文で進む (あとの置き換え a => c も通る) ({items[0].outputs[1]['sent']!r})")
check(len(items) == 4 and items[0].state == items[1].state == engine.DONE and len(items[0].outputs) == 3,
      f"戻ったら★を読み直して続きを作る (増やすで 2 本) ({[(i.state, len(i.outputs)) for i in items]})")
pump(0.5)
window.grab().save(str(TMP / "steps.png"))
lv._end_gates()
pump(0.5)
check(not lv.running() and not lv.gate_end_button.isVisible(), "★待ちを終えたら量産も終わる")
check("★なし 2" in lv.summary.text(), f"★なしの数 ({lv.summary.text()})")
window.close()
print(f"\nすべて OK ({len(ok)})  画像: {TMP}")
