"""物語のバッチ (story_batch.py) のテスト: 偽の ComfyUI (本物の短い mp4 を返す) と偽の LLM で、
物語を 2 本まとめて作る・失敗したら次の本へ・途中で止める・量産ラインが終わるのを待つ・
待ち行列 (順に進める・一時停止して続き・前回の残りから続ける・送られた画像で窓を出す)、を見る。

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
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox  # noqa: E402

from fakes import FakeComfy, FakeLLM, make_video, workflow_flf, workflow_i2v, workflow_i2v_ref  # noqa: E402
from gencommon import comfy, enhancer, settings, store  # noqa: E402
from prismlab import engine, profile, recipes, story, story_batch, theme  # noqa: E402
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
    while time.time() < end and (sv.batch.active or (sv.batch.waiting() and not sv.batch.paused)):
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
check(dialog.result() == 1 and plan.cuts == ["歩いてくる", "振り返る", "走り去る"] and dialog.plans[0].cuts == plan.cuts,
      "足すと区切ったプロンプトが入る")
check(story_batch.Plan.load().prompts == plan.prompts, "設定を覚える (次に窓を開いたときに入れる)")
# 最初の画像を何枚も並べると、1 枚ごとに 1 件 (名前の後ろに画像の名前)
multi = story_batch.BatchDialog(story_batch.Plan.load(), None, [str(photo)])
multi.add_images([str(face), str(photo)])  # 同じものは 2 度入れない
check(multi.image_paths() == [str(photo), str(face)] and "画像 2 枚 (バッチ 2 件)" in multi.cut_count.text(),
      multi.cut_count.text())
multi.grab().save(str(OUT / "batch_dialog_multi.png"))
multi._accept()
check([x.name for x in multi.plans] == ["駅_最初", "駅_顔"] and [x.image for x in multi.plans] == [str(photo), str(face)],
      "画像ごとに 1 件 (名前は 名前_画像の名前)")

# 3. 2 本作る (英語にしてから盛る・顔は全部のシーンに)
story_batch.ask_plans = lambda *_a, **_k: [plan]
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
check(sv.batch.entries[-1].state == story_batch.DONE and sv.batch.unfinished() == 0 and not sv.batch_bar.isVisible(),
      "待ち行列の 1 件が完了")
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
story_batch.ask_plans = lambda *_a, **_k: [plan2]
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
story_batch.ask_plans = lambda *_a, **_k: [plan3]
a.polls = 6
sv._start_batch()
pump(1.0)
sv._stop_batch()
pump(0.3)
check(not sv.batch.active and "止めました" in told[-1] and sv.detail.isEnabled(), told[-1])
end = time.time() + 30
while time.time() < end and sv.busy():
    pump(0.2)
check(sorted(p.name for p in parent.iterdir() if p.name.startswith("止める")) == ["止める_01"], "次の本は作らない")
a.polls = 2

# 6. 量産ラインが動いていれば、終わるのを待ってから始める
def small_plan(name, cuts="a / b", runs=1):
    p = story_batch.Plan(name=name, parent=str(parent), image=str(photo), prompts=cuts,
                         workflow=str(wf_dir / "Wan I2V.json"), runs=runs)
    p.cuts = story_batch.split_prompts(p.prompts)
    return p


line_busy = [True]
sv.line_busy = lambda: line_busy[0]
story_batch.ask_plans = lambda *_a, **_k: [small_plan("待つ", "a")]
told.clear()
sv._start_batch()
pump(2.5)
check(not sv.batch.active and len(sv.batch.waiting()) == 1 and "量産ライン" in sv.batch_label.text()
      and sv.batch_bar.isVisible(), sv.batch_label.text())
check(sv.detail.isEnabled(), "待っているあいだは物語のタブを使える")
line_busy[0] = False
pump(2.5)
check(sv.batch.active or (parent / "待つ").exists(), "量産ラインが終わったら始める")
wait_batch()
check(told and told[-1].startswith("出来た物語 1 本"), told[-1] if told else "")

# 7. 待ち行列: 動いているあいだに足したものは、前のものが終わってから順に
story_batch.ask_plans = lambda *_a, **_k: [small_plan("順1")]
sv._start_batch()
pump(0.5)
check(sv.batch.active, "1 件目が始まる")
story_batch.ask_plans = lambda *_a, **_k: [small_plan("順2"), small_plan("順3")]
sv._start_batch()  # 動いているあいだも足せる
pump(0.2)
check(len(sv.batch.waiting()) == 2 and sv.queue_b.text() == "待ち行列 (3)", sv.queue_b.text())
qw = story_batch.QueueWindow(sv.batch, lambda: None, sv)
qw.show()
pump(0.3)
check(qw.table.rowCount() >= 3 and qw.table.item(qw.table.rowCount() - 3, 0).text().startswith("実行中"),
      "待ち行列の窓: 実行中と待機中を並べる")
qw.grab().save(str(OUT / "batch_queue.png"))
# 並べ替え: 順3 を 順2 の前へ
last = sv.batch.entries[-1]
sv.batch.move(last.id, -1)
check([e.plan.name for e in sv.batch.entries[-2:]] == ["順3", "順2"], "上へ・下へで並べ替える")
told.clear()
wait_batch(180)
names = [p.name for p in parent.iterdir()]
check(all(n in names for n in ("順1", "順2", "順3")), str(names))
order = [(parent / n).stat().st_ctime for n in ("順1", "順3", "順2")]  # フォルダを作った時刻
check(order == sorted(order), "上から順に作る")
check(told and told[-1].startswith("出来た物語 3 本 (バッチ 3 件)"), told[-1] if told else "")
qw.close()

# 8. 一時停止: 今のシーンが終わったら止まり、再開するとその物語の続きから
story_batch.ask_plans = lambda *_a, **_k: [small_plan("休む", "a / b / c")]
a.polls = 4
sv._start_batch()
pump(0.5)
sv.batch.set_paused(True)
check("一時停止" in sv.batch_label.text(), sv.batch_label.text())
end = time.time() + 60
while time.time() < end and sv.batch.active:
    pump(0.2)
entry = sv.batch.entries[-1]
st = story.Story.load(str(parent / "休む"))
check(entry.state == story_batch.WAITING and entry.folder and Path(entry.folder).name == "休む"
      and st.scenes[0].adopted and not st.scenes[1].adopted, "一時停止: シーン 1 を採用して止まる (待機に戻る)")
check(sv.detail.isEnabled() and sv.batch_bar.isVisible() and sv.batch_pause.text() == "再開", "止まると物語のタブを使える")
saved = json.loads((Path(os.environ["APPDATA"]) / "PrismLab" / story_batch.QUEUE_FILE).read_text(encoding="utf-8"))
check(saved["paused"] and saved["entries"][-1]["folder"] == entry.folder, "待ち行列をファイルに残す")
window.grab().save(str(OUT / "batch_paused.png"))

# 9. 前回の残りから続ける (アプリを閉じて開き直したとき。一時停止のまま閉じたら聞かずに一時停止で戻す)
sv.batch.entries = []
sv.batch.paused = False
sv.offer_batch_resume()
check(len(sv.batch.waiting()) == 1 and sv.batch.paused, "前回の残りを一時停止のまま戻す")
a.polls = 2
told.clear()
sv.batch.set_paused(False)
pump(0.5)
wait_batch()
st = story.Story.load(str(parent / "休む"))
check(all(s.adopted for s in st.scenes) and not (parent / "休む (2)").exists(), "同じ物語の、まだ採用していないシーンから作る")
check(told and told[-1].startswith("出来た物語 1 本"), told[-1] if told else "")
# 実行中のまま落ちたもの: 待機から続ける。捨てると消える
dropped = story_batch.Entry(small_plan("捨てる"), state=story_batch.RUNNING)
story_batch.save_queue([dropped], False)
sv.batch.entries = []
count, paused = sv.batch.load_previous()
check(count == 1 and not paused and sv.batch.restoring[0].state == story_batch.WAITING, "実行中だったものは待機から")
sv.batch.restore(False)
check(not sv.batch.entries and not sv.batch.restoring and story_batch.load_queue()[0] == [], "捨てると待ち行列から消す")

# 10. ほかのアプリから「物語のバッチに足す」: 受け取ってから設定の窓を出す。開いていればそこに足す
seen = []
story_batch.ask_plans = lambda _p, _d, images=None, opened=None: seen.append(images) or []
check(sv.receive([str(photo), str(TMP / "clip.mp4")], "batch", "J-Rate") == "" and not seen, "送った側はすぐ返る")
pump(0.3)
check(seen == [[str(photo)]], f"送った画像で窓を出す (動画は除く) {seen}")


class FakeDialog:
    def __init__(self):
        self.added = []

    def add_images(self, paths):
        self.added += paths


sv._batch_dialog = FakeDialog()
window.receive({"cmd": "send", "from": "PWC", "target": "story", "slot": "batch", "files": [str(face)]})
check(sv._batch_dialog.added == [str(face)] and window.tabs.currentWidget() is sv, "窓を開いているときは、その窓に足す")
sv._batch_dialog = None
check("画像だけ" in sv.receive([str(TMP / "clip.mp4")], "batch"), "画像がなければ理由を返す")

# 11. 最近開いた物語をバッチの物語で埋めない。終わったら、前に開いていた物語に戻す (場所はまとめで知らせる)
batch_names = {p.name for p in parent.iterdir()}
check(not any(Path(f).name in batch_names for f in story.recent()), "バッチの物語は最近開いた物語に入れない")
mine = story.Story.create(str(TMP / "自分" / "自分の物語"), "自分の物語")
sv.open_folder(mine.folder)
story_batch.ask_plans = lambda *_a, **_k: [small_plan("戻る", "a")]
told.clear()
sv._start_batch()
pump(0.5)
check(sv.batch.active and "バッチ" in sv.story_box.currentText(), "作っている物語は上の欄に (バッチ) と出す")
wait_batch()
check(sv.story is not None and sv.story.name == "自分の物語", "終わったら前に開いていた物語に戻す")
check(told and f"作った場所: {parent}" in told[-1], told[-1] if told else "")
check(story.recent()[0] == mine.folder, "次に起動したときも自分の物語を開く")

# 12. 窓 (ファイルを選ぶ・確かめるなど) を開いているあいだは始めない (選んだものがバッチの物語に入らないように)
modal = QDialog(window)
modal.setModal(True)
modal.show()
pump(0.2)
story_batch.ask_plans = lambda *_a, **_k: [small_plan("窓", "a")]
sv._start_batch()
pump(2.5)
check(not sv.batch.active and "窓を閉じたら" in sv.batch_label.text(), sv.batch_label.text())
modal.close()
pump(2.5)
check(sv.batch.active or (parent / "窓").exists(), "窓を閉じたら始める")
wait_batch()
check(sv.story.name == "自分の物語" and not sv.scene.source, "自分の物語には何も入っていない")

# 13. 読み取る・盛る・英語に (手で) を待っているあいだは始めない (結果の入れ先がなくなるので)
sv._llm_busy = "読み取り"
story_batch.ask_plans = lambda *_a, **_k: [small_plan("読む", "a")]
sv._start_batch()
pump(2.5)
check(not sv.batch.active and "読み取り" in sv.batch_label.text(), sv.batch_label.text())
sv._llm_busy = ""
pump(2.5)
check(sv.batch.active or (parent / "読む").exists(), "終わったら始める")
wait_batch()

# 14. 量産ラインが一時停止 (実行中のものなし) なら待たない。バッチの最中は量産の再開・やり直しをさせない
sv.line_busy = lv.blocks_batch
line_recipe = recipes.Recipe(name="量産", output_dir=str(TMP / "量産"), inherit=False,
                             stages=[recipes.Stage(recipes.GEN, workflow=str(wf_dir / "Wan I2V.json"))])
lv.engine.set_machines([a.url])
lv.engine.start(engine.Run.create(line_recipe, [photo, photo, photo]))
lv.engine.set_paused(True)
end = time.time() + 30
while time.time() < end and any(i.state == engine.RUNNING for i in lv.engine.run.items):
    pump(0.2)
check(lv.running() and not lv.blocks_batch(), "一時停止して実行中のものがない量産は、バッチを待たせない")
story_batch.ask_plans = lambda *_a, **_k: [small_plan("夜中", "a / b")]
a.polls = 6
sv._start_batch()
pump(2.5)
check(sv.batch.active, "一時停止の量産があってもバッチを始める")
told.clear()
lv._toggle_pause()
check(lv.engine.run.paused and told and "同時には" in told[-1], "バッチの最中は量産を再開しない")
with lv.engine._cond:
    lv.engine.run.items[0].state, lv.engine.run.items[0].error = engine.FAILED, "テスト"
told.clear()
lv._retry()
check(lv.engine.run.items[0].state == engine.FAILED and told and "同時には" in told[-1],
      "バッチの最中は量産の失敗をやり直さない")
a.polls = 2
wait_batch()
lv.engine.cancel_all()
end = time.time() + 30
while time.time() < end and lv.running():
    pump(0.2)
sv.line_busy = lambda: False

# 15. 足したあとで最初の画像が消えたら、その件は 1 度だけ知らせて終える (空の物語のフォルダを残さない)
gone = TMP / "消える.png"
shutil.copy2(photo, gone)
p = small_plan("消えた", "a", runs=3)
p.image = str(gone)
gone.unlink()
story_batch.ask_plans = lambda *_a, **_k: [p]
told.clear()
sv._start_batch()
wait_batch()
check(told and told[-1].startswith("出来た物語 0 本") and told[-1].count("最初の画像が見つかりません") == 1,
      told[-1] if told else "")
check(not any(n.name.startswith("消えた") for n in parent.iterdir()), "空の物語のフォルダを残さない")

window.story.shutdown()
window.engine.stop_threads()
for s in (a, llm):
    s.close()
print(f"\nすべて OK ({len(ok)})  画像: {OUT}")
if OUT == TMP:
    shutil.rmtree(TMP, ignore_errors=True)
