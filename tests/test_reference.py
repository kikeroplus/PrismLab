"""リファレンスのステップ (2026-11-20): リファレンス (1 枚)・リファレンス (フォルダ) のテスト。
偽の ComfyUI を使い、APPDATA を一時フォルダに差し替える (実際の設定に触れない)。

    python tests\\test_reference.py
"""

import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="prismlab_ref_"))
os.environ["APPDATA"] = str(TMP / "appdata")
os.environ["QT_QPA_PLATFORM"] = os.environ.get("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(ROOT.parent / "GenCommon"))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from fakes import FakeComfy, workflow_i2v, workflow_i2v_ref, workflow_t2i  # noqa: E402
from gencommon import store  # noqa: E402
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
t2i_ref = workflow_t2i()  # T2I + 顔だけ (LoadImage はリファレンス用だけ)
t2i_ref["6"] = {"class_type": "LoadImage", "_meta": {"title": "顔"}, "inputs": {"image": "face.png"}}
t2i_ref["4"]["inputs"]["ref"] = ["6", 0]
for name, wf in (("I2V", workflow_i2v()), ("I2V ref", workflow_i2v_ref()), ("T2I ref", t2i_ref),
                 ("T2I", workflow_t2i())):
    (WF / f"{name}.json").write_text(json.dumps(wf), encoding="utf-8")
I2V, I2V_REF, T2I_REF, T2I = (str(WF / f"{n}.json") for n in ("I2V", "I2V ref", "T2I ref", "T2I"))


def marked(folder: Path, names: list) -> list[Path]:
    """中身に名前の印を入れた画像 (偽の ComfyUI が受けた中身から、どれを送ったか分かるように)。"""
    folder.mkdir(parents=True, exist_ok=True)
    out = []
    for n in names:
        p = folder / f"{n}.png"
        p.write_bytes(b"\x89PNG\r\n\x1a\n" + f"MARK-{n}-END".encode() + b"\0" * 64)
        out.append(p)
    return out


def start(stages, sources, trial=False):
    r = recipes.Recipe("t", folder=str(sources[0].parent), stages=stages)
    eng = engine.Engine(run_file=None)
    eng.set_machines([a.url])
    run = engine.Run.create(r, sources, trial=trial)
    eng.start(run)
    return eng


def sent_refs(since: int, node: str = "6") -> list[str]:
    """送ったワークフローの、リファレンスの LoadImage に入った画像の印。"""
    out = []
    for wf in a.received[since:]:
        raw = a.uploads.get(wf[node]["inputs"]["image"], b"")
        out.append(raw.split(b"MARK-")[1].split(b"-END")[0].decode() if b"MARK-" in raw else "")
    return out


src = marked(TMP / "in", ["src1", "src2", "src3"])
face = marked(TMP / "face", ["alice"])[0]
faces = marked(TMP / "faces", ["f1", "f2", "f3"])

# 1. 種類と保存
# 2026-11-26: リファレンスは「画像」のステップの入れる先になった (メニューには画像の 2 種類だけ)
check(recipes.REF_IMAGE not in recipes.STAGE_ORDER and recipes.IMG_ONE in recipes.STAGE_ORDER
      and recipes.IMG_FOLDER in recipes.STAGE_ORDER, "＋ステップのメニューは画像の 2 種類 (リファレンスは入れる先)")
s = recipes.Stage.from_dict(recipes.Stage(recipes.REF_FOLDER, folder="x", shuffle=True).to_dict())
check(s.type == recipes.IMG_FOLDER and s.target == recipes.TARGET_REF and s.folder == "x" and s.shuffle,
      "前のリファレンス (フォルダ) は、入れる先がリファレンスの画像 (フォルダ) として読む (フォルダ・ランダムも)")
s = recipes.Stage.from_dict(recipes.Stage(recipes.REF_IMAGE, image=str(face)).to_dict())
check(s.image == str(face), "1 枚の画像を覚える")

# 2. 1 枚: 元画像は元の LoadImage、顔はリファレンスの LoadImage に
n0 = len(a.received)
eng = start([recipes.Stage(recipes.REF_IMAGE, image=str(face)), recipes.Stage(recipes.GEN, workflow=I2V_REF)], src)
wait(eng)
check(all(it.state == engine.DONE for it in eng.run.items), "1 枚: 全部できる")
check(sent_refs(n0) == ["alice"] * 3, f"1 枚: どの件もリファレンスに同じ画像 ({sent_refs(n0)})")
check(sorted(sent_refs(n0, "1")) == ["src1", "src2", "src3"], f"元画像は元の LoadImage に ({sent_refs(n0, '1')})")
check(all(it.outputs[0].get("reference") == str(face) for it in eng.run.items), "結果に使ったリファレンスを残す")

# 3. フォルダ: 名前順に 1 件ずつ、最後まで使ったら最初から
n0 = len(a.received)
eng = start([recipes.Stage(recipes.REF_FOLDER, folder=str(faces[0].parent)),
             recipes.Stage(recipes.GEN, workflow=I2V_REF)], src + src[:2])
wait(eng)
by_src = sorted(zip([Path(it.source).stem for it in eng.run.items], [Path(it.reference).stem for it in eng.run.items]))
check([Path(it.reference).stem for it in eng.run.items] == ["f1", "f2", "f3", "f1", "f2"],
      f"フォルダ: 名前順に 1 件ずつ・一巡したら最初から ({by_src})")
check(sorted(sent_refs(n0)) == sorted(["f1", "f2", "f3", "f1", "f2"]), f"送った顔 ({sorted(sent_refs(n0))})")

# 4. ランダム: 一巡のあいだは重ならない
seen = set()
for _ in range(10):
    run = engine.Run.create(recipes.Recipe("r", folder=str(src[0].parent), stages=[
        recipes.Stage(recipes.REF_FOLDER, folder=str(faces[0].parent), shuffle=True),
        recipes.Stage(recipes.GEN, workflow=I2V_REF)]), src, trial=True)
    order = tuple(Path(it.plan["0"]["file"]).stem for it in run.items)
    check(sorted(order) == ["f1", "f2", "f3"], f"ランダム: 一巡で全部を 1 回ずつ ({order})") if not seen else None
    assert sorted(order) == ["f1", "f2", "f3"], order
    seen.add(order)
check(len(seen) >= 2, f"ランダム: 量産ごとに順が変わる ({len(seen)} 通り)")

# 5. 増やすのあと: 枝ごとに別の画像
run = engine.Run.create(recipes.Recipe("s", folder=str(src[0].parent), stages=[
    recipes.Stage(recipes.SPLIT, count=3), recipes.Stage(recipes.REF_FOLDER, folder=str(faces[0].parent)),
    recipes.Stage(recipes.GEN, workflow=I2V_REF)]), src[:1], trial=True)
check({k: Path(v["file"]).stem for k, v in run.items[0].plan.items()} == {"1": "f1", "1/1": "f2", "1/2": "f3"},
      "増やすのあと: 枝の数だけ割り当てる")
n0 = len(a.received)
eng = start([recipes.Stage(recipes.SPLIT, count=3), recipes.Stage(recipes.REF_FOLDER, folder=str(faces[0].parent)),
             recipes.Stage(recipes.GEN, workflow=I2V_REF)], src[:1])
wait(eng)
check(sorted(sent_refs(n0)) == ["f1", "f2", "f3"], f"増やすのあと: 1 本ごとに別の顔 ({sent_refs(n0)})")

# 6. 生成を通っても残る・次のリファレンスで置き換え
n0 = len(a.received)
eng = start([recipes.Stage(recipes.REF_IMAGE, image=str(face)), recipes.Stage(recipes.GEN, workflow=I2V_REF),
             recipes.Stage(recipes.GEN, workflow=I2V_REF), recipes.Stage(recipes.REF_IMAGE, image=str(faces[2])),
             recipes.Stage(recipes.GEN, workflow=I2V_REF)], src[:1])
wait(eng)
check(sent_refs(n0) == ["alice", "alice", "f3"], f"生成を通っても残り、次のリファレンスで変わる ({sent_refs(n0)})")

# 7. リファレンスの入口がないワークフローには使わない (元画像だけ)
n0 = len(a.received)
eng = start([recipes.Stage(recipes.REF_IMAGE, image=str(face)), recipes.Stage(recipes.GEN, workflow=I2V)], src[:1])
wait(eng)
check(eng.run.items[0].state == engine.DONE and sent_refs(n0, "1") == ["src1"]
      and "reference" not in eng.run.items[0].outputs[0], "入口のないワークフローは今までどおり (記録にも残さない)")

# 8. 始める前の確かめ
for bad, label in ((recipes.Stage(recipes.REF_IMAGE), "画像なし"),
                   (recipes.Stage(recipes.REF_IMAGE, image=str(TMP / "none.png")), "画像が見つからない"),
                   (recipes.Stage(recipes.REF_FOLDER, folder=str(TMP / "in" / "nothing")), "フォルダなし")):
    try:
        engine.stage_snapshot(bad)
        check(False, f"始めない: {label}")
    except ValueError:
        check(True, f"始めない: {label}")
check(engine.stage_snapshot(recipes.Stage(recipes.REF_FOLDER, folder="", bypass=True))["bypass"],
      "バイパスなら中身は確かめない")

# 9. テキストの入口 + T2I の顔だけのワークフロー (LoadImage がリファレンス用だけ) は画像なしで動く
P = recipes.line_problems
check(("needimage", 0) in P([recipes.Stage(recipes.GEN, workflow=T2I_REF)], text_entry=True),
      "リファレンスなしなら、顔だけのワークフローも画像が要る")
check(not P([recipes.Stage(recipes.REF_IMAGE, image=str(face)), recipes.Stage(recipes.GEN, workflow=T2I_REF)],
            text_entry=True), "リファレンスを決めてあれば、顔だけのワークフローはテキストの入口で動く")
check(("needimage", 1) in P([recipes.Stage(recipes.REF_IMAGE, image=str(face)), recipes.Stage(recipes.GEN, workflow=I2V_REF)],
                            text_entry=True), "元画像の LoadImage もあるなら、やはり画像が要る")
txt = TMP / "texts"
txt.mkdir()
(txt / "a.txt").write_text("a girl", encoding="utf-8")
r = recipes.Recipe("tx", folder=str(txt), source=recipes.TEXTS,
                   stages=[recipes.Stage(recipes.REF_IMAGE, image=str(face)), recipes.Stage(recipes.GEN, workflow=T2I_REF)])
n0 = len(a.received)
eng = engine.Engine(run_file=None)
eng.set_machines([a.url])
eng.start(engine.Run.create(r, [txt / "a.txt"]))
wait(eng)
check(eng.run.items[0].state == engine.DONE and sent_refs(n0) == ["alice"],
      f"テキストの入口から、顔だけの T2I に顔を入れて作る ({eng.run.items[0].error})")
check(recipes.can_bypass([recipes.Stage(recipes.REF_IMAGE, image=str(face)), recipes.Stage(recipes.GEN, workflow=I2V_REF)], 0),
      "画像の入口ならリファレンスはバイパスできる")
check(not recipes.can_bypass([recipes.Stage(recipes.REF_IMAGE, image=str(face)), recipes.Stage(recipes.GEN, workflow=T2I_REF)],
                             0, text_entry=True), "テキストの入口で顔だけの T2I の前のリファレンスはバイパスできない")

# 10. 量産中にフォルダを変えると、まだ通っていない件から新しいフォルダ
other = marked(TMP / "faces2", ["g1", "g2"])
eng = start([recipes.Stage(recipes.REF_FOLDER, folder=str(faces[0].parent)), recipes.Stage(recipes.GEN, workflow=I2V_REF)],
            src)
eng.set_paused(True)
eng.update_stage(0, engine.stage_snapshot(recipes.Stage(recipes.REF_FOLDER, folder=str(other[0].parent))))
check([Path(it.plan["0"]["file"]).stem for it in eng.run.items if it.stage == 0] ==
      ["g1", "g2", "g1"][:sum(1 for it in eng.run.items if it.stage == 0)], "量産中にフォルダを変えると割り当て直す")
eng.set_paused(False)
wait(eng)

# 11. 画面: 札・設定の窓・ドロップ
from prismlab import dialogs, line_view, main_window  # noqa: E402

window = main_window.MainWindow()
window.show()
pump(0.5)
lv = window.line
lv.recipe.stages = [recipes.Stage(recipes.REF_FOLDER, folder=str(faces[0].parent)),
                    recipes.Stage(recipes.REF_IMAGE, image=str(face)), recipes.Stage(recipes.GEN, workflow=I2V)]
lv.refresh(rebuild=True)
pump(1.0)
c0, c1 = lv.stage_cards[0], lv.stage_cards[1]
check(c0.title.text() == "リファレンス" and c0.chip.text() == "フォルダ" and "faces" in c0.detail.text() and "名前順" in c0.detail.text(),
      f"フォルダの札 ({c0.detail.text()!r})")
check("⚠" in c1.detail.text(), "あとの生成にリファレンスの入口がなければ知らせる")
check(len(lv._ref_thumbs[0]) == 3 and lv._ref_thumbs[1] == [str(face)], "札に画像を並べる")
lv.recipe.stages[2].workflow = I2V_REF
lv.refresh(rebuild=True)
pump(0.3)
check("⚠" not in lv.stage_cards[1].detail.text(), "入口があれば知らせない")
check("⚠" in lv.stage_cards[0].detail.text(), "次のリファレンスで置き換わるものは知らせる")
lv._set_ref_image(1, str(faces[1]))
pump(0.3)
check(lv.recipe.stages[1].image == str(faces[1]) and recipes.load()[lv.index].stages[1].image == str(faces[1]),
      "1 枚の札に画像を落とすと、その画像にする")

for t, value in ((recipes.REF_IMAGE, str(face)), (recipes.REF_FOLDER, str(faces[0].parent))):
    st = recipes.Stage(t)
    d = dialogs.StageDialog(st, lv)
    d.show()
    d.ref_path.setText(value)
    pump(0.2)
    check(d.ref_preview.isVisible() and d.ref_note.text() in ("", "画像 3 枚"), f"設定の窓に画像を出す ({t})")
    if t == recipes.REF_FOLDER:
        d.place.button(1).setChecked(True)
    d._accept()
    check(d.result() == 1 and (st.image == value if t == recipes.REF_IMAGE else st.folder == value and st.shuffle),
          f"設定の窓で決める ({t})")
d = dialogs.StageDialog(recipes.Stage(recipes.REF_FOLDER), lv)
d.ref_path.setText(str(TMP / "in" / "nothing"))
d._accept()
check(d.result() == 0, "画像のないフォルダでは OK にしない")
window.grab().save(str(TMP / "reference.png"))
window.close()
print(f"\nすべて OK ({len(ok)})  画像: {TMP}")
