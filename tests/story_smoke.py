"""物語の画面の確認: 偽の ComfyUI (本物の短い mp4 を返す) と偽の LLM で、シーン 2 つを作ってつなぎ、画面を PNG に撮る。

    python tests\\story_smoke.py (撮った画像の置き場所)
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
TMP = Path(tempfile.mkdtemp(prefix="prismlab_story_ui_"))
OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else TMP
os.environ["APPDATA"] = str(TMP / "appdata")
sys.path.insert(0, str(ROOT.parent / "GenCommon"))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtGui import QColor, QImage  # noqa: E402
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QFileDialog  # noqa: E402

from fakes import FakeComfy, FakeLLM, make_video, workflow_i2v, workflow_i2v_ref, workflow_t2i  # noqa: E402
from gencommon import comfy, settings, store  # noqa: E402
from prismlab import profile, recipes, story, story_view, theme  # noqa: E402
from prismlab.main import make_icon  # noqa: E402
from prismlab.main_window import MainWindow  # noqa: E402

app = QApplication(sys.argv)
app.setWindowIcon(make_icon())
theme.apply(app)
profile.setup()
assert str(store.APP_DIR).startswith(str(TMP))

clip = TMP / "clip.mp4"
make_video(clip, [(200, 40, 40)] * 4 + [(40, 60, 220)] * 4, size=(160, 96))  # 赤 4 コマ → 青 4 コマ
a, llm = FakeComfy(polls=2), FakeLLM()
a.video_bytes = clip.read_bytes()
settings.set_llm_url(llm.url)
comfy.set_connections([comfy.Preset("メイン", a.url)] + [comfy.Preset() for _ in range(comfy.PRESET_COUNT - 1)])
wf_dir = TMP / "wf"
wf_dir.mkdir()
(wf_dir / "Wan I2V.json").write_text(json.dumps(workflow_i2v()), encoding="utf-8")
(wf_dir / "Flux T2I.json").write_text(json.dumps(workflow_t2i()), encoding="utf-8")
(wf_dir / "Wan I2V Face.json").write_text(json.dumps(workflow_i2v_ref()), encoding="utf-8")
comfy.set_workflows([comfy.Preset("Wan I2V", str(wf_dir / "Wan I2V.json")),
                     comfy.Preset("Flux T2I", str(wf_dir / "Flux T2I.json")),
                     comfy.Preset("Wan I2V Face", str(wf_dir / "Wan I2V Face.json"))]
                    + [comfy.Preset() for _ in range(comfy.WORKFLOW_COUNT - 3)])
recipes.set_machines([a.url])

photo = TMP / "ホーム.png"
img = QImage(480, 300, QImage.Format.Format_RGB32)
img.fill(QColor(40, 160, 90))
img.save(str(photo))
lines = TMP / "セリフ"
lines.mkdir()
(lines / "001.txt").write_text("[セリフ] 「もう来ないと思ってた」", encoding="utf-8")

folder = TMP / "物語" / "雨の駅"
story.Story.create(str(folder), "雨の駅")
story.remember(str(folder))

window = MainWindow()
window.resize(1360, 900)
window.show()
window.tabs.setCurrentWidget(window.story)
sv = window.story


def pump(seconds: float):
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        time.sleep(0.03)


def wait_engine(limit: float = 60):
    end = time.time() + limit
    while time.time() < end and sv.busy():
        pump(0.2)
    pump(1)


pump(1)
assert sv.story is not None and sv.story.name == "雨の駅", "最近開いた物語を開いて始まる"
s1 = sv.scene
assert len(sv.scene_cards) == 1 and "元画像がまだ" in sv.scene_cards[0].state.text()
# 開いた直後から、選ぶ欄が埋まっている (ここが空だと、読み取る・盛る・候補を作るが動かない)
assert sv.workflow_box.count() == 3 and sv.reader_box.count() > 0 and sv.enhancer_box.count() > 0,     (sv.workflow_box.count(), sv.reader_box.count(), sv.enhancer_box.count())
assert Path(s1.workflow).name == "Wan I2V.json", s1.workflow  # 初めは I2V を選んでおく

# 1 シーン目: 手元の画像 → 読み取る → 差し込み → 盛る → 候補 2 本
sv.story.set_source_file(s1, str(photo))
s1.name = "ホーム"
sv.refresh(rebuild=True, load=True)
ORIGINAL = "左から女性が歩いてきて右に消える"
sv.original.setPlainText(ORIGINAL)
pump(0.2)
assert s1.original == ORIGINAL
sv.read_b.click()
end = time.time() + 20
while time.time() < end and sv._llm_busy:
    pump(0.2)
pump(0.3)
assert sv.prompt.toPlainText().startswith("a photo"), sv.prompt.toPlainText()
story_view.QFileDialog.getExistingDirectory = staticmethod(lambda *a, **k: str(lines))
sv.insert_b.click()
pump(0.3)
assert "もう来ないと思ってた" in sv.prompt.toPlainText(), sv.prompt.toPlainText()
before = sv.prompt.toPlainText()
sv.enhance_b.click()
end = time.time() + 20
while time.time() < end and sv._llm_busy:
    pump(0.2)
pump(0.3)
assert sv.prompt.toPlainText().endswith("enhanced"), sv.prompt.toPlainText()
enhanced = sv.prompt.toPlainText()
from PySide6.QtCore import QEvent  # noqa: E402
from PySide6.QtGui import QKeyEvent  # noqa: E402
sv.prompt.setFocus()
QApplication.sendEvent(sv.prompt, QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier))
pump(0.2)
assert sv.prompt.toPlainText() == before, "Ctrl+Z で盛る前の文に"
assert sv.original.toPlainText() == ORIGINAL and s1.original == ORIGINAL, "元のプロンプトは読み取る・盛るで変わらない"
sv.prompt.redo()
pump(0.1)
assert sv.prompt.toPlainText() == enhanced
sv.undo_b.click()
pump(0.2)
assert sv.prompt.toPlainText() == before, "元に戻すで盛る前の文に"
sv.add_original_b.click()
pump(0.1)
assert sv.prompt.toPlainText() == before + "\n" + ORIGINAL, "↓ 元の文を足す"
sv.undo_b.click()
pump(0.1)
assert sv.prompt.toPlainText() == before
labels = [h["label"] for h in s1.history]
# 同じ文は続けて残さない (差し込みの前 = 読み取った文、戻す前 = 盛った文 は 1 つにまとまる)
assert labels == ["読み取った文", "差し込んだ文 (001.txt)", "盛った文", "元の文を足す前の文"], labels
assert any(h["text"] == enhanced for h in s1.history), "盛った文は戻したあとも履歴に残る"
dialog = story_view.HistoryDialog(s1, window)
assert dialog.list.count() == len(s1.history)
dialog.list.setCurrentRow(dialog.list.count() - 1)  # いちばん最初の文
dialog._use()
first = s1.history[0]["text"]
assert dialog.chosen == first
sv._restore(dialog.chosen, "履歴の文に戻しました")
pump(0.2)
assert sv.prompt.toPlainText() == first
sv._restore(before, "戻しました")
pump(0.2)
sv.count_box.setValue(2)
sv.make_b.click()
pump(1)
window.grab().save(str(OUT / "story_making.png"))
wait_engine()
assert len(s1.candidates) == 2, s1.candidates
assert all(Path(c.path).parent == sv.story.scene_dir(s1) for c in s1.candidates)
assert sv.cands.count() == 2

# 採用して次へ: 「続き」でシーン 2 が出来て、元画像は最後のコマ (青)
sv.cands.setCurrentRow(0)
sv.adopt_b.click()
pump(0.8)
s2 = sv.scene
assert s2 is not s1 and s2.link == story.CONT and Path(s2.source).is_file(), (s2.link, s2.source)
c = QImage(s2.source).pixelColor(80, 48)
assert c.blue() > 150 and c.red() < 120, (c.red(), c.green(), c.blue())
assert (sv.story.adopted_dir() / "01_ホーム.mp4").is_file()


def shown_color(label):
    pm = label.pixmap()
    assert pm is not None and not pm.isNull(), "元画像が出ていない"
    return pm.toImage().pixelColor(pm.width() // 2, pm.height() // 2)


pump(0.8)
c = shown_color(sv.source_view)
assert c.blue() > 150 and c.red() < 120, ("続きの元画像に最後のコマ (青) が出る", c.red(), c.green(), c.blue())
# 前のシーンの採用を、最後のコマが緑の動画に変える → 続きのシーンの元画像 (同じ source.png) の表示も緑になる
make_video(Path(s1.candidates[1].path), [(200, 40, 40)] * 4 + [(40, 200, 60)] * 4, size=(160, 96))
sv.select(s1)
pump(0.3)
sv.cands.setCurrentRow(next(i for i in range(sv.cands.count())
                            if sv.cands.item(i).data(256) == s1.candidates[1].path))
sv.adopt_only.click()
pump(0.3)
sv.select(s2)
pump(0.8)
c = shown_color(sv.source_view)
assert c.green() > 150 and c.blue() < 120, ("採用を変えたら、続きの元画像の表示も新しいコマに", c.red(), c.green(), c.blue())
s2.name = "振り返る"
sv.prompt.setPlainText("she turns around slowly")
sv.make_b.click()
wait_engine()
sv.cands.setCurrentRow(1)
sv.adopt_only.click()
pump(0.5)
names = sorted(p.name for p in sv.story.adopted_dir().iterdir())
assert names == ["01_ホーム.mp4", "02_振り返る.mp4"], names

# 候補の削除 (ごみ箱は差し替えて、消したものを覚える)
from prismlab import recycle  # noqa: E402
binned = []


def fake_bin(paths, timeout=3.0):
    for p in paths:
        binned.append(Path(p).name)
        Path(p).unlink()
    return None


recycle.send_to_recycle_bin = fake_bin
sv.select(s2)
pump(0.3)
cands2 = [Path(c.path) for c in s2.candidates]
adopted2 = Path(s2.adopted)
other = next(p for p in cands2 if p != adopted2)
(other.with_suffix(".txt")).write_text("説明", encoding="utf-8")  # 説明の .txt も一緒に消える
row = next(i for i in range(sv.cands.count()) if Path(sv.cands.item(i).data(256)) == other)
sv.cands.setCurrentRow(row)
sv._delete_candidates()  # 1 つなら確かめない
pump(0.3)
assert not other.exists() and other.with_suffix(".txt").name in binned, binned
assert len(s2.candidates) == 1 and s2.adopted == str(adopted2) and sv.cands.count() == 1
from PySide6.QtCore import QEvent  # noqa: E402
from PySide6.QtGui import QKeyEvent  # noqa: E402
sv.cands.setCurrentRow(0)
QApplication.sendEvent(sv.cands, QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Delete, Qt.KeyboardModifier.NoModifier))
pump(0.5)
assert not adopted2.exists() and s2.adopted == "" and s2.candidates == [], "Del で採用中の候補を消すと採用もやめる"
names = sorted(p.name for p in sv.story.adopted_dir().iterdir())
assert names == ["01_ホーム.mp4"], names
assert "ごみ箱へ移動しました" in window.statusBar().currentMessage(), window.statusBar().currentMessage()
sv.cands.setCurrentRow(-1)

# リファレンス画像 (顔): 対応するワークフローのときだけ選べる
sv.select(s1)
pump(0.3)
assert not sv.ref_pick.isEnabled() and "対応していません" in sv.ref_note.text(), sv.ref_note.text()
sv.workflow_box.setCurrentIndex(sv.workflow_box.findData(str(wf_dir / "Wan I2V Face.json")))
pump(0.5)
assert sv.ref_pick.isEnabled(), "リファレンス対応のワークフローなら選べる"
face = TMP / "顔.png"
fimg = QImage(200, 200, QImage.Format.Format_RGB32)
fimg.fill(QColor(230, 190, 160))
fimg.save(str(face))
sv._set_reference(str(face))
pump(0.3)
assert Path(s1.reference).name == "reference.png" and sv.ref_clear.isEnabled()
window.grab().save(str(OUT / "story_reference.png"))
sv.original.setPlainText("顔を残して振り向く")
sv.count_box.setValue(1)
before_sent = len(a.received)
sv.make_b.click()
wait_engine()
wf_sent = a.received[before_sent]
assert wf_sent["1"]["inputs"]["image"] != wf_sent["6"]["inputs"]["image"], "リファレンス用の LoadImage に別の画像を送る"
newest = max(s1.candidates, key=lambda c: c.time)
assert newest.original == "顔を残して振り向く" and newest.prompt == s1.send_text and newest.reference == s1.reference
# 候補を作ったときのプロンプトに戻す
sv.original.setPlainText("あとで書き換えた文")
sv.prompt.setPlainText("あとで盛った文")
pump(0.2)
sv._back_to_candidate(newest)
pump(0.2)
assert sv.original.toPlainText() == "顔を残して振り向く" and sv.prompt.toPlainText() == newest.prompt
sv.workflow_box.setCurrentIndex(sv.workflow_box.findData(str(wf_dir / "Wan I2V.json")))
pump(0.3)
assert not sv.ref_pick.isEnabled() and "設定済みの画像は残っています" in sv.ref_note.text()

# シーン 3: シーンチェンジ (T2I のワークフローなら元画像なしでも作れる)
s3 = sv._add_scene(s2, story.CUT)
pump(0.3)
assert s3.link == story.CUT and s3.source == "" and "元画像がまだ" in sv.scene_cards[2].state.text()
sv.select(s1)
pump(0.5)
window.grab().save(str(OUT / "story_board.png"))

# 保存して開き直す
sv._save()
again = story.Story.load(str(folder))
assert [s.name for s in again.scenes] == ["ホーム", "振り返る", ""] and not again.scenes[1].adopted
assert again.scenes[0].original == "顔を残して振り向く" and again.scenes[0].prompt == newest.prompt  # 候補のときに戻したもの
assert again.scenes[0].reference.endswith("reference.png")
assert any(h["label"] == "候補に送った文" for h in again.scenes[0].history), "候補に送った文も履歴に残す"
assert any(h["text"] == enhanced for h in again.scenes[0].history), "履歴は開き直しても残る"

# 通して再生の窓 (開くだけ)
dialog = story_view.PlayDialog([(s.label(i), s.adopted) for i, s in enumerate(again.scenes) if s.adopted], window)
dialog.show()
pump(1)
dialog.done(0)
print("物語の画面: OK", [s.name for s in again.scenes])
window.close()
pump(0.3)
a.close()
llm.close()
shutil.rmtree(TMP, ignore_errors=True)
