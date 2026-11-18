"""物語のバッチ (story_batch.py) のテスト: 偽の ComfyUI (本物の短い mp4 を返す) と偽の LLM で、
物語を 2 本まとめて作る・失敗したら次の本へ・途中で止める・台の数まで並べて作る・量産ラインと台を分け合う・
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
QMessageBox.warning = staticmethod(lambda _p, title, text, *a, **k: told.append(text))  # 窓で止まらないように
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

# 3. 2 本作る (英語にしてから盛る・顔は全部のシーンに)。台が 1 台なら 1 本ずつ
def small_plan(name, cuts="a / b", runs=1):
    p = story_batch.Plan(name=name, parent=str(parent), image=str(photo), prompts=cuts,
                         workflow=str(wf_dir / "Wan I2V.json"), runs=runs)
    p.cuts = story_batch.split_prompts(p.prompts)
    return p


def most_busy(seconds):
    """seconds のあいだに、同時に作っていた物語の本数の最大。"""
    peak = 0
    end = time.time() + seconds
    while time.time() < end:
        peak = max(peak, len(sv.batch.busy_workers()))
        pump(0.1)
    return peak


story_batch.ask_plans = lambda *_a, **_k: [plan]
before_sent = len(a.received)
told.clear()
sv._start_batch()
pump(0.3)
check(sv.batch.active and sv.batch_bar.isVisible() and "物語のバッチ: 1 本を作っています" in sv.batch_label.text(),
      sv.batch_label.text())
check(sv.detail.isEnabled() and sv.story_box.isEnabled() and sv.new_b.isEnabled(), "バッチのあいだも物語のタブを使える")
check(most_busy(1.5) == 1, "台が 1 台なら、物語は 1 本ずつ")
window.grab().save(str(OUT / "batch_running.png"))
wait_batch()
check(not sv.batch.active and not sv.batch_bar.isVisible(), "終わったら帯を消す")
check(told and told[-1].startswith("出来た物語 2 本") and "途中" not in told[-1] and f"作った場所: {parent}" in told[-1],
      told[-1] if told else "")
check(sv.batch.entries[-1].state == story_batch.DONE and sv.batch.unfinished() == 0, "待ち行列の 1 件が完了")
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
    check(all(len(s.candidates) == 1 and s.candidates[0].prompt == s.prompt for s in st.scenes)
          and len(list(st.adopted_dir().glob("0*.mp4"))) == 3, f"{name}: 候補の記録と採用のフォルダに 3 本")
    check(st.reference_of(st.scenes[2]) == st.scenes[0].reference != "", f"{name}: 顔は最初のシーンのもの")
sent = a.received[before_sent:]
check(len(sent) == 6 and all(w["6"]["inputs"]["image"] != w["1"]["inputs"]["image"] for w in sent),
      "6 本とも、顔の LoadImage に元画像とは別の画像を送る")
check(sv.story is not None and Path(sv.story.folder) == parent / "駅_02" and not sv.watch_folder
      and not any(Path(f).parent == parent for f in story.recent()),
      "終わったら、最後に作っていた物語を直せるように開く (最近開いた物語には入れない)")
window.grab().save(str(OUT / "batch_done.png"))

# 4. 失敗したらその物語は止め、次の本へ
plan2 = small_plan("失敗", "a / b", runs=2)
story_batch.ask_plans = lambda *_a, **_k: [plan2]
a.fail = True
sv._start_batch()
wait_batch()
a.fail = False
check(told[-1].startswith("出来た物語 0 本") and told[-1].count("候補が出来ませんでした") == 2, told[-1])
st = story.Story.load(str(parent / "失敗_02"))
check(not st.scenes[0].adopted and not st.scenes[1].source, "失敗したシーンで止まる (次のシーンは元画像なし)")

# 5. 作っているものを止める
story_batch.ask_plans = lambda *_a, **_k: [small_plan("止める", "a / b / c", runs=3)]
a.polls = 6
sv._start_batch()
pump(1.0)
sv._stop_batch()
pump(0.3)
check(not sv.batch.active and "止めました" in told[-1], told[-1])
end = time.time() + 30
while time.time() < end and any(w.winding_down for w in sv.batch.workers):
    pump(0.2)
check(sorted(p.name for p in parent.iterdir() if p.name.startswith("止める")) == ["止める_01"], "次の本は作らない")
a.polls = 2

# 6. バッチが作っている物語は、物語のタブで開かない。作りかけの続きを作るときは、開いていれば閉じる
story_batch.ask_plans = lambda *_a, **_k: [small_plan("開かない", "a / b")]
a.polls = 4
sv._start_batch()
pump(0.5)
working = sv.batch.busy_workers()[0].folder
told.clear()
check(not sv.open_folder(working) and told and "バッチで作っています" in told[-1], "作っている物語は開けない")
sv.batch.set_paused(True)
end = time.time() + 60
while time.time() < end and sv.batch.active:
    pump(0.2)
check(sv.batch.is_working(working) and not sv.open_folder(working, quiet=True), "一時停止中の作りかけも開けない")
sv.batch.entries[-1].folders = []  # (確かめるため) 作りかけの印を外して開き、また印を付ける
sv.open_folder(working)
sv.batch.entries[-1].folders = [working]
check(sv.story is not None and Path(sv.story.folder) == Path(working), "(物語のタブで開いた)")
a.polls = 2
sv.batch.set_paused(False)
pump(0.5)
check(sv.watch_folder and Path(sv.watch_folder) == Path(working) and sv.story is not None
      and sv.batch.busy_workers()[0].story is not sv.story,
      "続きを作る前に、物語のタブで開いていたものを閉じ、見るだけで出し直す")
wait_batch()
check(all(s.adopted for s in story.Story.load(working).scenes), "続きから最後まで作る")

# 7. 待ち行列: 動いているあいだに足したものは、前のものが終わってから順に (台 1 台)
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
check(entry.state == story_batch.WAITING and [Path(f).name for f in entry.folders] == ["休む"]
      and st.scenes[0].adopted and not st.scenes[1].adopted, "一時停止: シーン 1 を採用して止まる (待機に戻る)")
check(sv.batch_bar.isVisible() and sv.batch_pause.text() == "再開", "止まっても帯を出す (再開)")
saved = json.loads((Path(os.environ["APPDATA"]) / "PrismLab" / story_batch.QUEUE_FILE).read_text(encoding="utf-8"))
check(saved["paused"] and saved["entries"][-1]["folders"] == entry.folders, "待ち行列をファイルに残す")
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
# 前の形 (作りかけ 1 本の folder) も読める
old = story_batch.Entry.from_dict({"plan": json.loads(small_plan("前の形").to_json()), "state": "running",
                                   "run_index": 1, "folder": str(parent / "前の形_02")})
check(old.folders == [str(parent / "前の形_02")] and old.run_index == 2, "前の形の待ち行列も続きから読める")
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

# 11. バッチのあいだも、物語のタブで自分の物語を直せる・送られたものを入れられる
mine = story.Story.create(str(TMP / "自分" / "自分の物語"), "自分の物語")
sv.open_folder(mine.folder)
story_batch.ask_plans = lambda *_a, **_k: [small_plan("裏で", "a / b")]
sv._start_batch()
pump(0.5)
check(sv.batch.active and sv.receive([str(face)], "scenes") == "" and len(sv.story.scenes) == 2,
      "バッチのあいだも、送られた画像をシーンにできる")
modal = QDialog(window)
modal.setModal(True)
modal.show()  # 窓を開いていても、バッチは裏で進む (物語のタブを使わないので)
wait_batch()
modal.close()
check(sv.story.name == "自分の物語" and len(sv.story.scenes) == 2 and Path(sv.story.scenes[1].source).is_file(),
      "自分の物語はそのまま")
check(all(s.adopted for s in story.Story.load(str(parent / "裏で")).scenes), "窓を開いていてもバッチは最後まで")

# 15. バッチで作っている物語を、物語のタブに見るだけで出す (2026-11-18 利用者の希望)
hand = story.Story.create(str(TMP / "自分" / "手の物語"), "手の物語")
sv.open_folder(hand.folder)
story_batch.ask_plans = lambda *_a, **_k: [small_plan("見る", "a / b / c", runs=2)]
a.polls = 6
sv._start_batch()
end = time.time() + 10
while time.time() < end and not sv.watch_folder:
    pump(0.1)
w1 = sv.batch.busy_workers()[0]
check(Path(sv.watch_folder) == Path(w1.folder) == parent / "見る_01" and sv.story.name == "見る 01",
      "バッチが動き出したら、作っている物語を出す")
check(sv.story is not w1.story and sv.watch_note.isVisible() and "見るだけ" in sv.watch_note.text(),
      "出すのは写しで、見るだけと知らせる")
check(sv.story_box.currentText().startswith("▶ 作成中: 見る 01"), f"物語を選ぶ欄にも作成中と出す ({sv.story_box.currentText()})")
check(not sv.make_b.isEnabled() and not sv.adopt_b.isEnabled() and sv.original.isReadOnly() and sv.prompt.isReadOnly()
      and not sv.pick_file.isEnabled() and not sv.workflow_box.isEnabled(), "見ているあいだは直せない")
check(sv.scene_cards[0].state.text() == "バッチで作っています…", f"作っているシーンの札 ({sv.scene_cards[0].state.text()})")
before_json = (Path(w1.folder) / story.STORY_FILE).read_text(encoding="utf-8")
sv.story.scenes[0].name = "いたずら"
sv._save()
check((Path(w1.folder) / story.STORY_FILE).read_text(encoding="utf-8") == before_json, "画面からはバッチの物語を書かない")
end = time.time() + 60
while time.time() < end and w1.active and w1.scene_index < 1:
    pump(0.1)
pump(0.6)
check(sv.story.scenes[0].adopted and sv.scene is sv.story.scenes[1] and sv.scene_cards[1].state.text() == "バッチで作っています…",
      "進んだら描き直して、作っているシーンを追いかける")
sv.select(sv.story.scenes[0])
pump(0.8)
check(sv.scene is sv.story.scenes[0], "人が選んだシーンには留まる")
end = time.time() + 120
while time.time() < end and not (sv.watch_folder and Path(sv.watch_folder) == parent / "見る_02"):
    pump(0.1)
check(Path(sv.watch_folder) == parent / "見る_02", "1 本出来たら、次に作っている物語へ")
wait_batch()
check(not sv.watch_folder and sv.story is not None and Path(sv.story.folder) == parent / "見る_02"
      and sv.make_b.isEnabled() and not sv.original.isReadOnly() and not sv.watch_note.isVisible(),
      "終わったら、出来た物語を直せるように開く")
# 人がほかの物語を開いたら、バッチの途中でも勝手に切り替えない
story_batch.ask_plans = lambda *_a, **_k: [small_plan("見ない", "a / b", runs=2)]
sv._start_batch()
end = time.time() + 10
while time.time() < end and not sv.watch_folder:
    pump(0.1)
sv.story_box.setCurrentIndex(sv.story_box.findData(hand.folder))
sv._story_chosen(sv.story_box.currentIndex())
check(not sv.watch_folder and sv.story.name == "手の物語" and sv.make_b.isEnabled(), "物語を選べば、手の物語に戻る")
pump(1.0)
end = time.time() + 120
while time.time() < end and not (parent / "見ない_02").exists():
    pump(0.1)
pump(1.0)
check(not sv.watch_folder and sv.story.name == "手の物語", "次の本が始まっても、勝手に切り替えない")
i = sv.story_box.findData(sv.batch.busy_workers()[0].folder)
check(i >= 0, "作成中の物語は、選ぶ欄から選べる")
sv._story_chosen(i)
check(sv.watch_folder and sv.story.name == "見ない 02", "選べば、また見るだけで出す")
sv.receive([str(face)], "scenes")
check(not sv.watch_folder and sv.story.name == "手の物語" and len(sv.story.scenes) == 2,
      "見ているときに送られたものは、前に開いていた物語に入れる")
wait_batch()
a.polls = 2

# 12. 量産ラインとバッチは同時に動く。台の空きは分け合い (1 台に合わせて 2 件まで)、両方が待っていれば物語が先
def is_story(wf):
    return "STORY" in str(wf["2"]["inputs"]["text"])


line_recipe = recipes.Recipe(name="量産", output_dir=str(TMP / "量産"), inherit=False,
                             stages=[recipes.Stage(recipes.GEN, workflow=str(wf_dir / "Wan I2V.json"))])
engine.POOL.peak.clear()
a.polls = 6
sent = len(a.received)
lv.engine.set_machines([a.url])
lv.engine.start(engine.Run.create(line_recipe, [photo] * 5))
end = time.time() + 20
while time.time() < end and len(a.received) - sent < 2:
    pump(0.2)
pump(1.0)
check(len(a.received) - sent == 2 and engine.POOL.used(a.url) == 2, "量産ラインが 1 台に 2 件 (残りは待機)")
story_batch.ask_plans = lambda *_a, **_k: [small_plan("夜中", "STORY1 / STORY2")]
told.clear()
sv._start_batch()
pump(2.5)
check(sv.batch.active and lv.running() and not told, "量産ラインが動いていても、バッチはすぐ始まる (台の空きを待つ)")
check(engine.POOL.used(a.url) == 2, "台が埋まっていれば、物語も空きを待つ (1 台に 3 件目は送らない)")
wait_batch(180)
end = time.time() + 120
while time.time() < end and lv.running():
    pump(0.2)
order = ["物語" if is_story(w) else "量産" for w in a.received[sent:]]
check(order[:2] == ["量産", "量産"] and "物語" in order[2:4], f"空きが出たら、物語のぶんを残す (物語が待たされない): {order}")
check(order.count("物語") == 2 and order.count("量産") == 5, f"どちらも最後まで: {order}")
check(engine.POOL.peak.get(a.url.lower(), 0) == 2, f"1 台に同時に送ったのは 2 件まで ({engine.POOL.peak})")
a.polls = 2

# 13. 足したあとで最初の画像が消えたら、その件は 1 度だけ知らせて終える (空の物語のフォルダを残さない)
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

# 14. 台が 2 台なら、別々の物語を 2 本並べて作る (同じ件の何本目も・別の件も)。量産ラインとも分け合う
b = FakeComfy(polls=6)
b.video_bytes = clip.read_bytes()
comfy.set_connections([comfy.Preset("FALCON", a.url), comfy.Preset("CONDOR", b.url)]
                      + [comfy.Preset() for _ in range(comfy.PRESET_COUNT - 2)])
recipes.set_machines([a.url, b.url])
lv.engine.set_machines([a.url, b.url])
sv.machines_changed()
check(sv.machines_b.text() == "台: FALCON · CONDOR", sv.machines_b.text())
check(sv.batch.capacity() == 2, "台が 2 台なら 2 本まで並べる")
engine.POOL.peak.clear()
a.polls = 6
sent_a, sent_b = len(a.received), len(b.received)
story_batch.ask_plans = lambda *_a, **_k: [small_plan("並べる", "STORY1 / STORY2", runs=3)]
sv._start_batch()
check(most_busy(4) == 2, "同じ件の 2 本を並べて作る")
wait_batch(240)
check(all(all(s.adopted for s in story.Story.load(str(parent / f"並べる_0{i}")).scenes) for i in (1, 2, 3)),
      "3 本とも最後まで")
check(told and told[-1].startswith("出来た物語 3 本 (バッチ 1 件)"), told[-1] if told else "")
check(len(a.received) > sent_a and len(b.received) > sent_b, "両方の台を使う")
# 別の件どうしも並べる。量産ラインと一緒でも、どちらの台も 2 件まで
lv.engine.start(engine.Run.create(line_recipe, [photo] * 6))
story_batch.ask_plans = lambda *_a, **_k: [small_plan("件A", "STORY1 / STORY2"), small_plan("件B", "STORY1 / STORY2")]
sv._start_batch()
check(most_busy(4) == 2 and {e.plan.name for e in sv.batch.entries if e.id in sv.batch.working_ids()} <= {"件A", "件B"},
      "別の件の物語も並べて作る")
wait_batch(240)
end = time.time() + 120
while time.time() < end and lv.running():
    pump(0.2)
check((parent / "件A").exists() and (parent / "件B").exists() and not lv.running(), "量産も物語も最後まで")
check(max(engine.POOL.peak.values()) <= 2, f"どちらの台も同時に 2 件まで ({engine.POOL.peak})")
a.polls = 2
b.close()

window.story.shutdown()
window.engine.stop_threads()
for s in (a, llm):
    s.close()
print(f"\nすべて OK ({len(ok)})  画像: {OUT}")
if OUT == TMP:
    shutil.rmtree(TMP, ignore_errors=True)
