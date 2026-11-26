"""画像のステップの入れる先 (2026-11-26): 流れている画像 / リファレンス / ラストフレーム / ラベル のテスト。
LoadImage 以外の画像の入口 (画像の名前を持つノード) にも入れる。
偽の ComfyUI を使い、APPDATA を一時フォルダに差し替える (実際の設定に触れない)。

    python tests\\test_image_targets.py
"""

import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="prismlab_target_"))
os.environ["APPDATA"] = str(TMP / "appdata")
os.environ["QT_QPA_PLATFORM"] = os.environ.get("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(ROOT.parent / "GenCommon"))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from fakes import FakeComfy, workflow_flf, workflow_i2v, workflow_t2i  # noqa: E402
from gencommon import comfy, store  # noqa: E402
from prismlab import engine, flow, kinds, profile, recipes, theme  # noqa: E402

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


def wait(eng, limit=60.0):
    end = time.time() + limit
    while time.time() < end:
        app.processEvents()
        run = eng.run
        if run is not None and all(i.finished for i in run.items):
            pump(0.2)
            return
        time.sleep(0.05)
    raise AssertionError("時間内に終わりません: " + str([(i.state, i.stage, i.error) for i in eng.run.items]))


a = FakeComfy(polls=1)
WF = TMP / "wf"
WF.mkdir()

# スタイルの画像を入れるカスタムノード (LoadImage ではない。入力の名前も image ではない) を持つ I2V
styled = workflow_i2v()
styled["8"] = {"class_type": "MyStyleImageLoader", "_meta": {"title": "Style"}, "inputs": {"picture": "style.webp",
                                                                                          "strength": 0.5}}
styled["4"]["inputs"]["style"] = ["8", 0]
# URL・絶対パスを読むノードは画像の入口にしない (送った名前を入れると壊れる)
styled["10"] = {"class_type": "LoadImageFromUrl", "_meta": {"title": "Style"}, "inputs": {"url": "https://x/y.png"}}
styled["11"] = {"class_type": "WASImageLoad", "inputs": {"image_path": "C:\\pics\\a.png"}}
styled["4"]["inputs"]["u"] = ["10", 0]
styled["4"]["inputs"]["w"] = ["11", 0]
for name, wf in (("FLF", workflow_flf()), ("Styled", styled), ("T2I", workflow_t2i())):
    (WF / f"{name}.json").write_text(json.dumps(wf), encoding="utf-8")
FLF, STYLED, T2I = (str(WF / f"{n}.json") for n in ("FLF", "Styled", "T2I"))


def marked(folder: Path, names: list) -> list[Path]:
    folder.mkdir(parents=True, exist_ok=True)
    out = []
    for n in names:
        p = folder / f"{n}.png"
        p.write_bytes(b"\x89PNG\r\n\x1a\n" + f"MARK-{n}-END".encode() + b"\0" * 64)
        out.append(p)
    return out


def start(stages, sources):
    r = recipes.Recipe("t", folder=str(sources[0].parent), stages=stages)
    eng = engine.Engine(run_file=None)
    eng.set_machines([a.url])
    eng.start(engine.Run.create(r, sources))
    return eng


def sent(since: int, node: str, key: str = "image") -> list[str]:
    """送ったワークフローの、そのノードに入った画像の印。"""
    out = []
    for wf in a.received[since:]:
        raw = a.uploads.get(wf[node]["inputs"][key], b"")
        out.append(raw.split(b"MARK-")[1].split(b"-END")[0].decode() if b"MARK-" in raw else wf[node]["inputs"][key])
    return out


src = marked(TMP / "in", ["src1", "src2"])
last = marked(TMP / "last", ["end1"])[0]
style = marked(TMP / "style", ["s1"])[0]
styles = marked(TMP / "styles", ["sa", "sb"])

# 1. 画像の入口の見分け方 (GenCommon)
wf = comfy.load_workflow(STYLED)
slots = {(n.get("class_type"), k) for n, k in comfy.image_slots(wf)}
check(slots == {("LoadImage", "image"), ("MyStyleImageLoader", "picture")},
      f"画像の名前を持つノードは画像の入口 (URL・絶対パスは除く) ({slots})")
check(comfy.image_labels(wf) == ["Style"] and comfy.has_label(wf, "style") and not comfy.has_label(wf, "Sty"),
      "ラベルはタイトルと同じもの (大文字・小文字は見ない。一部だけでは合わない)")
check(comfy.workflow_kind(comfy.load_workflow(T2I)) == "T2I", "画像の入口がなければ T2I のまま")
check(all(v["input"] != "picture" for v in comfy.value_slots(wf)), "画像の入口は「値を変える」の候補に出さない")

# 2. 保存と読み込み
s = recipes.Stage.from_dict(recipes.Stage(recipes.IMG_ONE, image="x", target=recipes.TARGET_LABEL,
                                          target_label=" Style ").to_dict())
check(s.target == recipes.TARGET_LABEL and s.target_label == "Style", "入れる先とラベルを覚える")
s = recipes.Stage.from_dict({"type": recipes.IMG_ONE, "target": recipes.TARGET_LABEL, "target_label": ""})
check(s.target == recipes.TARGET_FLOW, "ラベルが空なら流れている画像")
check(recipes.stage_title(recipes.Stage(recipes.IMG_FOLDER, target=recipes.TARGET_LAST)) == "ラストフレーム (フォルダ)"
      and recipes.stage_title(recipes.Stage(recipes.IMG_ONE)) == "画像 (1 枚)", "ステップの名前に入れる先")

# 3. ラストフレーム: ラストフレームの入口にだけ入れる (始めは流れている画像)
n0 = len(a.received)
eng = start([recipes.Stage(recipes.IMG_ONE, image=str(last), target=recipes.TARGET_LAST),
             recipes.Stage(recipes.GEN, workflow=FLF)], src)
wait(eng)
check(all(it.state == engine.DONE for it in eng.run.items), "ラストフレーム: 全部できる")
check(sent(n0, "7") == ["end1", "end1"] and sorted(sent(n0, "1")) == ["src1", "src2"],
      f"ラストフレームの入口にだけ入れる ({sent(n0, '7')} / {sent(n0, '1')})")
check(all(it.outputs[0].get("last_frame") == str(last) for it in eng.run.items), "結果に使ったラストフレームを残す")

# 4. ラベル: LoadImage でないノードにも、タイトルが同じなら入れる。生成を通っても残る
n0 = len(a.received)
eng = start([recipes.Stage(recipes.IMG_ONE, image=str(style), target=recipes.TARGET_LABEL, target_label="style"),
             recipes.Stage(recipes.GEN, workflow=STYLED), recipes.Stage(recipes.GEN, workflow=STYLED)], src[:1])
wait(eng)
it = eng.run.items[0]
check(it.state == engine.DONE, f"ラベル: できる ({it.error})")
check(sent(n0, "8", "picture") == ["s1", "s1"], f"タイトルが Style のノードに入れる (生成を通っても残る) ({sent(n0, '8', 'picture')})")
check(sent(n0, "1")[0] == "src1", "ほかの入口には流れている画像")
check([wf["10"]["inputs"]["url"] for wf in a.received[n0:]] == ["https://x/y.png"] * 2
      and a.received[n0]["11"]["inputs"]["image_path"] == "C:\\pics\\a.png", "URL・絶対パスのノードは書き換えない")
check(it.outputs[0].get("images") == {"style": str(style)}, "結果に使ったラベルの画像を残す")

# 5. フォルダ + ラベル: 1 件ごとに順に
n0 = len(a.received)
eng = start([recipes.Stage(recipes.IMG_FOLDER, folder=str(styles[0].parent), target=recipes.TARGET_LABEL,
                           target_label="Style"), recipes.Stage(recipes.GEN, workflow=STYLED)], src)
wait(eng)
check(sorted(sent(n0, "8", "picture")) == ["sa", "sb"], f"フォルダの画像を 1 件ごとに ({sent(n0, '8', 'picture')})")

# 6. 同じラベルの次のステップで置き換え。流れている画像の差し替えとも一緒に使える
n0 = len(a.received)
eng = start([recipes.Stage(recipes.IMG_ONE, image=str(style), target=recipes.TARGET_LABEL, target_label="Style"),
             recipes.Stage(recipes.GEN, workflow=STYLED),
             recipes.Stage(recipes.IMG_ONE, image=str(styles[1]), target=recipes.TARGET_LABEL, target_label="STYLE"),
             recipes.Stage(recipes.IMG_ONE, image=str(last)),
             recipes.Stage(recipes.GEN, workflow=STYLED)], src[:1])
wait(eng)
check(sent(n0, "8", "picture") == ["s1", "sb"] and sent(n0, "1") == ["src1", "end1"],
      f"同じラベルの次のステップで置き換え・流れている画像の差し替えは別 ({sent(n0, '8', 'picture')} / {sent(n0, '1')})")

# 7. 始める前の確かめ・テキストの入口
try:
    engine.stage_snapshot(recipes.Stage(recipes.IMG_ONE, image=str(style), target=recipes.TARGET_LABEL))
    check(False, "ラベルが空なら始めない")
except ValueError as exc:
    check("ラベル" in str(exc), "ラベルが空なら始めない")
P = recipes.line_problems
label_only = workflow_t2i()
label_only["6"] = {"class_type": "LoadImage", "_meta": {"title": "Pose"}, "inputs": {"image": "p.png"}}
label_only["4"]["inputs"]["pose"] = ["6", 0]
(WF / "Pose.json").write_text(json.dumps(label_only), encoding="utf-8")
POSE = str(WF / "Pose.json")
check(("needimage", 0) in P([recipes.Stage(recipes.GEN, workflow=POSE)], text_entry=True),
      "ラベルの画像を決めていなければ、画像が要る")
check(not P([recipes.Stage(recipes.IMG_ONE, image=str(style), target=recipes.TARGET_LABEL, target_label="pose"),
             recipes.Stage(recipes.GEN, workflow=POSE)], text_entry=True),
      "入口のラベルの画像を決めてあれば、テキストの入口で動く")
check(kinds.accepts(FLF, "last_frame") and kinds.accepts(STYLED, "label:style") and not kinds.accepts(FLF, "label:style"),
      "入れる先の入口があるか")

# 8. 流れの図: 入れる画像の段
views = [{"type": recipes.IMG_ONE, "target": recipes.TARGET_LABEL, "target_label": "Style"},
         {"type": recipes.GEN, "kind": "I2V"}]
g = flow.gaps(views)
check(g[1].lines[flow.REF].label == "Style", "流れの図に入れる画像の名前の段")

# 9. 画面: 札・設定の窓
from prismlab import dialogs, main_window  # noqa: E402

window = main_window.MainWindow()
window.show()
pump(0.5)
lv = window.line
lv.recipe.stages = [recipes.Stage(recipes.IMG_ONE, image=str(style), target=recipes.TARGET_LABEL, target_label="Style"),
                    recipes.Stage(recipes.IMG_ONE, image=str(last), target=recipes.TARGET_LAST),
                    recipes.Stage(recipes.GEN, workflow=STYLED)]
lv.refresh(rebuild=True)
pump(1.0)
c0, c1 = lv.stage_cards[0], lv.stage_cards[1]
check(c0.title.text() == "Style" and "⚠" not in c0.detail.text(), f"ラベルの札 ({c0.title.text()} / {c0.detail.text()!r})")
check(c1.title.text() == "ラストフレーム" and "⚠" in c1.detail.text() and "last" in c1.detail.text(),
      "あとの生成にラストフレームの入口がなければ知らせる")

st = recipes.Stage(recipes.IMG_ONE)
d = dialogs.StageDialog(st, lv)
d.show()
pump(0.2)
check(not d.target_label.isVisible(), "流れている画像ならラベルの欄は出さない")
d.target.setCurrentIndex(recipes.TARGETS.index(recipes.TARGET_LABEL))
pump(0.1)
check(d.target_label.isVisible() and [d.target_label.itemText(i) for i in range(d.target_label.count())] == ["Style"],
      "ラベルの候補は、レシピの生成のワークフローの画像の入口のタイトル")
d.ref_path.setText(str(style))
d.target_label.setCurrentText("")
d._accept()
check(d.result() == 0, "ラベルが空なら OK にしない")
d.target_label.setCurrentText("Style")
d._accept()
check(d.result() == 1 and st.target == recipes.TARGET_LABEL and st.target_label == "Style" and st.image == str(style),
      "設定の窓で入れる先とラベルを決める")
d.grab().save(str(TMP / "dialog.png"))

# 10. ＋ステップの画像は窓を出さずに足し、あとで画像・フォルダを札に落とす (2026-11-26)
from PySide6.QtCore import QMimeData, QUrl  # noqa: E402

opened = []
real_exec = dialogs.StageDialog.exec
dialogs.StageDialog.exec = lambda self: opened.append(self) or 0
line_view_mod = sys.modules["prismlab.line_view"]
line_view_mod.StageDialog.exec = dialogs.StageDialog.exec
lv.recipe.stages = [recipes.Stage(recipes.GEN, workflow=STYLED)]
lv.refresh(rebuild=True)
lv._add_stage(recipes.IMG_ONE, 0)
pump(0.5)
check(not opened and lv.recipe.stages[0].type == recipes.IMG_ONE and not lv.recipe.stages[0].image,
      "＋ステップの画像は、窓を出さずに画像なしで足す")
card = lv.stage_cards[0]
check("画像が選ばれていません" in card.io.text() and card.acceptDrops(), f"札に赤で知らせ、落とせる ({card.io.text()!r})")
check(("noimage", 0) in recipes.line_problems(lv.recipe.stages)
      and "画像が選ばれていません" in recipes.entry_problem(lv.recipe.stages, False), "画像がなければ始めない")
check(recipes.problems_text(recipes.line_problems(lv.recipe.stages), True).startswith("画像のステップに画像が選ばれていません"),
      "ほかのアプリに出すときの理由")


class FakeDrop:
    def __init__(self, path):
        self.m = QMimeData()
        self.m.setUrls([QUrl.fromLocalFile(str(path))])

    def mimeData(self):
        return self.m


check(card._dropped_path(FakeDrop(styles[0].parent)) and card._dropped_path(FakeDrop(style))
      and not card._dropped_path(FakeDrop(TMP / "wf" / "FLF.json")), "札は画像とフォルダを受け付ける (ほかのファイルは受けない)")
lv._set_ref_image(0, str(styles[0].parent))
pump(0.5)
s0 = lv.recipe.stages[0]
check(s0.type == recipes.IMG_FOLDER and s0.folder == str(styles[0].parent) and not recipes.line_problems(lv.recipe.stages),
      "1 枚の札にフォルダを落とすと、フォルダの画像ステップになる")
check("画像が選ばれていません" not in lv.stage_cards[0].io.text(), "選べば赤い知らせは消える")
s0.target, s0.target_label = recipes.TARGET_LABEL, "Style"
lv._set_ref_image(0, str(style))
pump(0.5)
s0 = lv.recipe.stages[0]
check(s0.type == recipes.IMG_ONE and s0.image == str(style) and s0.target_label == "Style",
      "フォルダの札に画像を落とすと 1 枚に (入れる先は残す)")
lv._set_ref_image(0, str(TMP / "wf"))
check(lv.recipe.stages[0].type == recipes.IMG_ONE, "画像のないフォルダは受けない")
lv._change_stage_type(0, recipes.IMG_FOLDER)
pump(0.3)
check(not opened and lv.recipe.stages[0].type == recipes.IMG_FOLDER and lv.recipe.stages[0].target_label == "Style",
      "種類を画像 (フォルダ) に変えるときも窓を出さず、入れる先を残す")
dialogs.StageDialog.exec = real_exec
window.close()
print(f"\nすべて OK ({len(ok)})  画像: {TMP}")
