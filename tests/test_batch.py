"""物語のバッチ (story_batch.py) のテスト: 偽の ComfyUI (本物の短い mp4 を返す) と偽の LLM で、
物語を 2 本まとめて作る・失敗したら次の本へ・途中で止める・量産ラインと同時に動かさない、を見る。

    python tests\\test_batch.py (撮った画像の置き場所)
APPDATA を一時フォルダに差し替えるので、実際の設定には触れない。
"""

import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="prismlab_batch_"))
OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else TMP
os.environ["APPDATA"] = str(TMP / "appdata")
sys.path.insert(0, str(ROOT.parent / "GenCommon"))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtGui import QColor, QImage  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from fakes import FakeComfy, FakeLLM, make_video, workflow_flf, workflow_i2v, workflow_i2v_ref  # noqa: E402
from gencommon import comfy, enhancer, settings, store  # noqa: E402
from prismlab import profile, recipes, story, story_batch, theme  # noqa: E402
from prismlab.main_window import MainWindow  # noqa: E402

app = QApplication(sys.argv)
theme.apply(app)
profile.setup()
assert str(store.APP_DIR).startswith(str(TMP))

told = []
QMessageBox.information = staticmethod(lambda _p, title, text, *a, **k: told.append(text))
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)

ok = []


def check(cond, label):
    assert cond, label
    ok.append(label)
    print("OK  ", label)


# 1. 区切り
check(story_batch.split_prompts("歩く / 振り返る\n走る／止まる //") == ["歩く", "振り返る", "走る", "止まる"],
      "「/」「／」・改行で区切り、空は捨てる")

clip = TMP / "clip.mp4"
make_video(clip, [(200, 40, 40)] * 4 + [(40, 60, 220)] * 4, size=(160, 96))  # 赤 4 コマ → 青 4 コマ
a, llm = FakeComfy(polls=2), FakeLLM()
a.video_bytes = clip.read_bytes()
settings.set_llm_url(llm.url)
comfy.set_connections([comfy.Preset("メイン", a.url)] + [comfy.Preset() for _ in range(comfy.PRESET_COUNT - 1)])
wf_dir = TMP / "wf"
wf_dir.mkdir()
(wf_dir / "Wan I2V.json").write_text(json.dumps(workflow_i2v()), encoding="utf-8")
(wf_dir / "Wan I2V Face.json").write_text(json.dumps(workflow_i2v_ref()), encoding="utf-8")
(wf_dir / "Wan FLF.json").write_text(json.dumps(workflow_flf()), encoding="utf-8")
comfy.set_workflows([comfy.Preset("Wan I2V", str(wf_dir / "Wan I2V.json")),
                     comfy.Preset("Wan I2V Face", str(wf_dir / "Wan I2V Face.json")),
                     comfy.Preset("Wan FLF", str(wf_dir / "Wan FLF.json"))]
                    + [comfy.Preset() for _ in range(comfy.WORKFLOW_COUNT - 3)])
recipes.set_machines([a.url])

photo = TMP / "最初.png"
img = QImage(320, 200, QImage.Format.Format_RGB32)
img.fill(QColor(40, 160, 90))
img.save(str(photo))
face = TMP / "顔.png"
img.fill(QColor(230, 190, 160))
img.save(str(face))
parent = TMP / "物語"
parent.mkdir()

window = MainWindow()
window.resize(1360, 900)
window.show()
window.tabs.setCurrentWidget(window.story)
sv, lv = window.story, window.line


def pump(seconds: float):
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        time.sleep(0.03)


def wait_batch(limit: float = 120):
    end = time.time() + limit
    while time.time() < end and sv.batch.active:
        pump(0.2)
    pump(0.5)


pump(0.5)

# 2. 設定の窓: 使えるワークフローは I2V (ラストフレームを指定するものは除く)
names = [label for label, _p in story_batch.video_workflows()]
check(len(names) == 2 and not any("FLF" in n for n in names) and any("[顔]" in n for n in names), str(names))
plan = story_batch.Plan(name="駅", parent=str(parent), image=str(photo), reference=str(face),
                        prompts="歩いてくる / 振り返る / 走り去る", workflow=str(wf_dir / "Wan I2V Face.json"),
                        english=True, enhance=True,
                        enhancer=next(p.name for p in enhancer.load_presets() if not p.is_empty), runs=2)
dialog = story_batch.BatchDialog(plan)
check(dialog.cut_count.text() == "カット数 3 × 2 本 = 候補 6 本", dialog.cut_count.text())
dialog.grab().save(str(OUT / "batch_dialog.png"))
dialog.name.setText("")
dialog._accept()
check(told and "名前" in told[-1] and dialog.result() == 0, "足りないものがあれば始めずに知らせる")
dialog.name.setText("駅")
dialog._accept()
check(dialog.result() == 1 and plan.cuts == ["歩いてくる", "振り返る", "走り去る"], "始めると区切ったプロンプトが入る")
check(story_batch.Plan.load().prompts == plan.prompts, "設定を覚える (次に窓を開いたときに入れる)")

# 3. 2 本作る (英語にしてから盛る・顔は全部のシーンに)
story_batch.ask_plan = lambda _parent, _defaults: plan
before_sent = len(a.received)
told.clear()
sv._start_batch()
pump(0.3)
check(sv.batch.active and not sv.detail.isEnabled() and not sv.story_box.isEnabled() and sv.batch_bar.isVisible(),
      "バッチのあいだは見るだけ (帯を出す)")
check(not lv._ready("開始"), "(量産ラインは始めない)")
check("同時には" in told[-1], "量産ラインを始めようとしたら知らせる")
check(sv.receive([str(photo)], "scenes") and "バッチ" in sv.receive([str(photo)], "scenes"), "送られたものは入れない")
pump(1.5)
window.grab().save(str(OUT / "batch_running.png"))
wait_batch()
check(not sv.batch.active and sv.detail.isEnabled() and not sv.batch_bar.isVisible(), "終わったら元に戻す")
check(told and told[-1].startswith("出来た物語 2 本") and "途中" not in told[-1], told[-1] if told else "")
folders = sorted(p.name for p in parent.iterdir())
check(folders == ["駅_01", "駅_02"], str(folders))
for name in folders:
    st = story.Story.load(str(parent / name))
    check(len(st.scenes) == 3 and all(s.adopted and Path(s.adopted).is_file() for s in st.scenes),
          f"{name}: 3 シーンとも候補 1 つを作って採用")
    check([s.link for s in st.scenes] == [story.CUT, story.CONT, story.CONT]
          and all(s.source_kind == story.SOURCE_LAST for s in st.scenes[1:]), f"{name}: 2 つ目からは続き (最後のコマ)")
    check([s.original for s in st.scenes] == ["歩いてくる", "振り返る", "走り去る"]
          and st.scenes[1].prompt == "振り返る in English enhanced", f"{name}: 英語にしてから盛った文を送る")
    check(all(len(s.candidates) == 1 for s in st.scenes) and len(list(st.adopted_dir().glob("0*.mp4"))) == 3,
          f"{name}: 採用のフォルダに 3 本")
    check(st.reference_of(st.scenes[2]) == st.scenes[0].reference != "", f"{name}: 顔は最初のシーンのもの")
sent = a.received[before_sent:]
check(len(sent) == 6 and all(w["6"]["inputs"]["image"] != w["1"]["inputs"]["image"] for w in sent),
      "6 本とも、顔の LoadImage に元画像とは別の画像を送る")
check(sv.story is not None and sv.story.name == "駅 02", "最後に作った物語を開いている")
window.grab().save(str(OUT / "batch_done.png"))

# 4. 失敗したらその物語は止め、次の本へ
plan2 = story_batch.Plan(name="失敗", parent=str(parent), image=str(photo), prompts="a / b",
                         workflow=str(wf_dir / "Wan I2V.json"), runs=2)
plan2.cuts = story_batch.split_prompts(plan2.prompts)
story_batch.ask_plan = lambda _parent, _defaults: plan2
a.fail = True
sv._start_batch()
wait_batch()
a.fail = False
check(told[-1].startswith("出来た物語 0 本") and told[-1].count("候補が出来ませんでした") == 2, told[-1])
st = story.Story.load(str(parent / "失敗_02"))
check(not st.scenes[0].adopted and not st.scenes[1].source, "失敗したシーンで止まる (次のシーンは元画像なし)")

# 5. 途中で止める
plan3 = story_batch.Plan(name="止める", parent=str(parent), image=str(photo), prompts="a / b / c",
                         workflow=str(wf_dir / "Wan I2V.json"), runs=3)
plan3.cuts = story_batch.split_prompts(plan3.prompts)
story_batch.ask_plan = lambda _parent, _defaults: plan3
a.polls = 6
sv._start_batch()
pump(0.8)
sv._stop_batch()
pump(0.3)
check(not sv.batch.active and "止めました" in told[-1] and sv.detail.isEnabled(), told[-1])
end = time.time() + 30
while time.time() < end and sv.busy():
    pump(0.2)
check(sorted(p.name for p in parent.iterdir() if p.name.startswith("止める")) == ["止める_01"], "次の本は作らない")
a.polls = 2

# 6. 量産ラインが動いていればバッチは始めない
sv.line_busy = lambda: True
told.clear()
sv._start_batch()
check(not sv.batch.active and told and "量産ライン" in told[-1], "量産ラインが動いていれば始めない")

window.story.shutdown()
window.engine.stop_threads()
for s in (a, llm):
    s.close()
print(f"\nすべて OK ({len(ok)})  画像: {OUT}")
if OUT == TMP:
    shutil.rmtree(TMP, ignore_errors=True)
