"""画像のステップ (2026-11-24): 画像 (1 枚)・画像 (フォルダ) のテスト。
流れている画像を差し替え、文はそのまま残す。偽の ComfyUI を使い、APPDATA を一時フォルダに差し替える (実際の設定に触れない)。

    python tests\\test_image_step.py
"""

import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="prismlab_img_"))
os.environ["APPDATA"] = str(TMP / "appdata")
os.environ["QT_QPA_PLATFORM"] = os.environ.get("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(ROOT.parent / "GenCommon"))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from fakes import FakeComfy, workflow_i2v, workflow_t2i  # noqa: E402
from gencommon import store  # noqa: E402
from prismlab import engine, flow, profile, recipes, theme  # noqa: E402

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
for name, wf in (("I2V", workflow_i2v()), ("T2I", workflow_t2i())):
    (WF / f"{name}.json").write_text(json.dumps(wf), encoding="utf-8")
I2V, T2I = (str(WF / f"{n}.json") for n in ("I2V", "T2I"))


def marked(folder: Path, names: list) -> list[Path]:
    """中身に名前の印を入れた画像 (偽の ComfyUI が受けた中身から、どれを送ったか分かるように)。"""
    folder.mkdir(parents=True, exist_ok=True)
    out = []
    for n in names:
        p = folder / f"{n}.png"
        p.write_bytes(b"\x89PNG\r\n\x1a\n" + f"MARK-{n}-END".encode() + b"\0" * 64)
        out.append(p)
    return out


def sent_images(since: int, node: str = "1") -> list[str]:
    """送ったワークフローの、元画像の LoadImage に入った画像の印。"""
    out = []
    for wf in a.received[since:]:
        raw = a.uploads.get(wf[node]["inputs"]["image"], b"")
        out.append(raw.split(b"MARK-")[1].split(b"-END")[0].decode() if b"MARK-" in raw else "")
    return out


def sent_texts(since: int) -> list[str]:
    return [wf["2"]["inputs"]["text"] for wf in a.received[since:]]


def run_texts(stages, names):
    txt = TMP / "texts"
    txt.mkdir(exist_ok=True)
    files = []
    for n in names:
        (txt / f"{n}.txt").write_text(f"prompt {n}", encoding="utf-8")
        files.append(txt / f"{n}.txt")
    r = recipes.Recipe("tx", folder=str(txt), source=recipes.TEXTS, stages=stages)
    eng = engine.Engine(run_file=None)
    eng.set_machines([a.url])
    eng.start(engine.Run.create(r, files))
    return eng


src = marked(TMP / "in", ["src1", "src2"])
pic = marked(TMP / "pic", ["outfit"])[0]
pics = marked(TMP / "pics", ["p1", "p2", "p3"])
P = recipes.line_problems

# 1. 種類と保存
check(recipes.IMG_ONE in recipes.STAGE_ORDER and recipes.IMG_FOLDER in recipes.STAGE_ORDER, "メニューに出る")
s = recipes.Stage.from_dict(recipes.Stage(recipes.IMG_FOLDER, folder="x", shuffle=True).to_dict())
check(s.type == recipes.IMG_FOLDER and s.folder == "x" and s.shuffle, "フォルダ・ランダムを覚える")
check(recipes.Stage.from_dict(recipes.Stage(recipes.IMG_ONE, image="y.png").to_dict()).image == "y.png", "1 枚を覚える")

# 2. ラインの成り立ち: テキストの入口でも、画像のあとなら I2V・読み取りができる
check(("needimage", 0) in P([recipes.Stage(recipes.GEN, workflow=I2V)], text_entry=True), "(画像がなければ I2V はできない)")
check(not P([recipes.Stage(recipes.IMG_ONE, image=str(pic)), recipes.Stage(recipes.GEN, workflow=I2V)], text_entry=True),
      "テキスト + 画像 (1 枚) → I2V は成り立つ")
check(not P([recipes.Stage(recipes.IMG_FOLDER, folder=str(pics[0].parent)), recipes.Stage(recipes.READ),
             recipes.Stage(recipes.GEN, workflow=I2V)], text_entry=True), "テキスト + 画像 → 読み取りも成り立つ")
check(not recipes.can_bypass([recipes.Stage(recipes.IMG_ONE, image=str(pic)), recipes.Stage(recipes.GEN, workflow=I2V)],
                             0, text_entry=True), "テキストの入口で I2V の前の画像はバイパスできない")
check(recipes.can_bypass([recipes.Stage(recipes.IMG_ONE, image=str(pic)), recipes.Stage(recipes.GEN, workflow=I2V)], 0),
      "画像の入口ならバイパスできる")
check(("needvideo", 2) in P([recipes.Stage(recipes.GEN, workflow=I2V), recipes.Stage(recipes.IMG_ONE, image=str(pic)),
                            recipes.Stage(recipes.FRAME)]), "画像のあとは動画ではない (最後のコマは置けない)")

# 3. テキスト + 1 枚 → I2V: 文はそのまま、画像は決めた 1 枚
n0 = len(a.received)
eng = run_texts([recipes.Stage(recipes.IMG_ONE, image=str(pic)), recipes.Stage(recipes.GEN, workflow=I2V)], ["a", "b"])
wait(eng)
check(all(it.state == engine.DONE for it in eng.run.items), f"テキストから作れる ({[it.error for it in eng.run.items]})")
check(sent_images(n0) == ["outfit", "outfit"], f"どの件も決めた画像を送る ({sent_images(n0)})")
texts = sent_texts(n0)
check(any("prompt a" in t for t in texts) and any("prompt b" in t for t in texts), f"文はそのまま送る ({texts})")
check(all(it.outputs[0]["from"] == str(pic) for it in eng.run.items), "結果の元は読み込んだ画像")

# 4. 画像の入口 + フォルダ: 流れている画像を 1 件ごとに差し替える (名前順・一巡したら最初から)
n0 = len(a.received)
r = recipes.Recipe("t", folder=str(src[0].parent),
                   stages=[recipes.Stage(recipes.IMG_FOLDER, folder=str(pics[0].parent)), recipes.Stage(recipes.GEN, workflow=I2V)])
eng = engine.Engine(run_file=None)
eng.set_machines([a.url])
eng.start(engine.Run.create(r, src + src))
wait(eng)
check(all(it.state == engine.DONE for it in eng.run.items), "フォルダ: 全部できる")
check(sorted(sent_images(n0)) == sorted(["p1", "p2", "p3", "p1"]), f"フォルダの画像を 1 件ずつ ({sent_images(n0)})")
check("src1" not in sent_images(n0) and "src2" not in sent_images(n0), "入口の画像は送らない (差し替えた)")

# 5. 確かめ (画像なし・見つからない・空のフォルダ)
for bad, label in ((recipes.Stage(recipes.IMG_ONE), "画像なし"),
                   (recipes.Stage(recipes.IMG_ONE, image=str(TMP / "none.png")), "画像が見つからない"),
                   (recipes.Stage(recipes.IMG_FOLDER, folder=str(TMP / "nothing")), "フォルダなし")):
    try:
        engine.stage_snapshot(bad)
        check(False, label)
    except ValueError as exc:
        check("画像" in str(exc), f"始める前に断る: {label} ({exc})")

# 6. 流れの線: 流れてきた動画はここで捨て、画像を作る。文は残る
v = lambda t, **k: {"type": t, **k}  # noqa: E731
g = flow.gaps([v(recipes.GEN, kind="T2V"), v(recipes.IMG_ONE), v(recipes.GEN, kind="I2V")])
check(g[1].lines[flow.MEDIA].cut and g[2].lines[flow.MEDIA].new and g[2].lines[flow.MEDIA].label == "画像",
      "流れの線: 前の動画は捨てて、新しい画像")
g = flow.gaps([v(recipes.IMG_ONE), v(recipes.GEN, kind="I2V")], text_entry=True)
check(flow.TEXT in g[1].lines and flow.MEDIA in g[1].lines, "流れの線: テキストの入口では文と画像が並んで流れる")

# 7. 画面: 札・設定の窓・ドロップ
from prismlab import dialogs, main_window  # noqa: E402

window = main_window.MainWindow()
window.show()
pump(0.5)
lv = window.line
lv.recipe.stages = [recipes.Stage(recipes.IMG_FOLDER, folder=str(pics[0].parent)),
                    recipes.Stage(recipes.IMG_ONE, image=str(pic)), recipes.Stage(recipes.GEN, workflow=I2V)]
lv.refresh(rebuild=True)
pump(0.5)
check(lv.stage_cards[0].title.text() == "画像" and lv.stage_cards[1].title.text() == "画像", "札の見出しは「画像」")
check(len(lv._ref_thumbs[0]) == 3 and lv._ref_thumbs[1] == [str(pic)], "札に画像を並べる")
lv._set_ref_image(1, str(pics[1]))
pump(0.3)
check(lv.recipe.stages[1].image == str(pics[1]), "1 枚の札に画像を落とすと、その画像にする")
for t, value in ((recipes.IMG_ONE, str(pic)), (recipes.IMG_FOLDER, str(pics[0].parent))):
    st = recipes.Stage(t)
    d = dialogs.StageDialog(st, lv)
    d.show()
    d.ref_path.setText(value)
    pump(0.2)
    if t == recipes.IMG_FOLDER:
        d.place.button(1).setChecked(True)
    d._accept()
    check(d.result() == 1 and (st.image == value if t == recipes.IMG_ONE else st.folder == value and st.shuffle),
          f"設定の窓で決める ({t})")
window.grab().save(str(TMP / "image_step.png"))
window.close()
print(f"\nすべて OK ({len(ok)})  画像: {TMP}")
