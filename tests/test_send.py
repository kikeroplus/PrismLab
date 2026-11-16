"""ほかのアプリから送る (spec 5 章) のテスト: パイプに JSON を書いて、量産の入口・物語に入るか。

    python tests\\test_send.py (撮った画像の置き場所)
APPDATA を一時フォルダに差し替え、パイプの名前もテスト用にするので、実際の設定・動いている PrismLab には触れない。
"""

import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="prismlab_send_"))
OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else TMP
os.environ["APPDATA"] = str(TMP / "appdata")
sys.path.insert(0, str(ROOT.parent / "GenCommon"))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtCore import QMimeData, QPoint, QPointF, Qt, QUrl  # noqa: E402
from PySide6.QtGui import QColor, QDropEvent, QImage  # noqa: E402
from PySide6.QtNetwork import QLocalSocket  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from fakes import FakeComfy, workflow_i2v, workflow_t2i  # noqa: E402
from gencommon import comfy, store  # noqa: E402
from prismlab import engine, main, profile, recipes, story, theme  # noqa: E402
from prismlab.main_window import MainWindow  # noqa: E402

main._SERVER_NAME = f"PrismLab-test-{os.getpid()}"  # 動いている PrismLab に送らないように
app = QApplication(sys.argv)
theme.apply(app)
profile.setup()
assert str(store.APP_DIR).startswith(str(TMP))

warnings = []
QMessageBox.warning = staticmethod(lambda _parent, title, text, *a, **k: warnings.append(text))
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)

fake = FakeComfy(polls=2)
comfy.set_connections([comfy.Preset("メイン", fake.url)] + [comfy.Preset() for _ in range(comfy.PRESET_COUNT - 1)])
wf_dir = TMP / "wf"
wf_dir.mkdir()
(wf_dir / "T2I.json").write_text(json.dumps(workflow_t2i()), encoding="utf-8")
(wf_dir / "I2V.json").write_text(json.dumps(workflow_i2v()), encoding="utf-8")
comfy.set_workflows([comfy.Preset("T2I", str(wf_dir / "T2I.json")), comfy.Preset("I2V", str(wf_dir / "I2V.json"))]
                    + [comfy.Preset() for _ in range(comfy.WORKFLOW_COUNT - 2)])
recipes.set_machines([fake.url])

pics = TMP / "pics"
pics.mkdir()
for i in range(5):
    img = QImage(120, 160, QImage.Format.Format_RGB32)
    img.fill(QColor.fromHsv(i * 60, 160, 200))
    img.save(str(pics / f"pick_{i + 1}.png"))
prompts = TMP / "prompts"
prompts.mkdir()
(prompts / "a.txt").write_text("a girl walking in the rain", encoding="utf-8")
(prompts / "b.txt").write_text("a cat on the roof", encoding="utf-8")
folder_in = TMP / "folder_in"
folder_in.mkdir()

rs = recipes.default_recipes()
rs[0].folder, rs[0].min_stars = str(folder_in), 0
rs[0].stages = [recipes.Stage(recipes.GEN, workflow=str(wf_dir / "I2V.json"), seconds=3)]
rs[1].name = "テキストから"
rs[1].stages = [recipes.Stage(recipes.GEN, workflow=str(wf_dir / "T2I.json")),
                recipes.Stage(recipes.GEN, workflow=str(wf_dir / "I2V.json"), seconds=3)]
recipes.save(rs)

window = MainWindow()
window.resize(1280, 860)
server = main._start_server(window)
window.show()


def pump(seconds: float):
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        time.sleep(0.02)


def send(message: dict) -> None:
    """J-Rate・PWC と同じように、パイプに JSON を書いて閉じる。"""
    sock = QLocalSocket()
    sock.connectToServer(main._SERVER_NAME)
    assert sock.waitForConnected(1000), "パイプにつながらない"
    sock.write(json.dumps(message, ensure_ascii=False).encode("utf-8") + b"\n")
    sock.flush()
    # 同じプロセスなので waitForDisconnected は使えない (受け側が動かない)。回しながら、受け側が切るのを待つ
    end = time.time() + 3
    while time.time() < end and sock.state() != QLocalSocket.LocalSocketState.UnconnectedState:
        pump(0.05)
    assert sock.state() == QLocalSocket.LocalSocketState.UnconnectedState, "受け側が切らない"
    pump(0.4)


def wait_run(limit=60):
    end = time.time() + limit
    while time.time() < end and window.engine.active and window.engine.run.unfinished:
        pump(0.2)
    pump(0.5)


ok = []


def check(cond, label):
    assert cond, label
    ok.append(label)
    print("OK  ", label)


pump(1)
lv = window.line
window.tabs.setCurrentWidget(window.story)

# 1. J-Rate から画像 3 枚をレシピ「★から動画」へ
files = [str(pics / f"pick_{i}.png") for i in (1, 2, 3)]
send({"cmd": "send", "from": "J-Rate", "target": "recipe", "recipe": "★から動画", "index": 0, "files": files})
check(window.tabs.currentWidget() is lv, "量産ラインのタブに切り替わる")
r = lv.recipe
check(lv.index == 0 and r.source == recipes.PICKED and r.picked == files and r.picked_back == recipes.FOLDER,
      "入口が「選んだもの」になり、前の入口 (フォルダ) を覚える")
check(lv.entry.detail.text() == "J-Rate から 3 枚", lv.entry.detail.text())
check(lv.entry.count.text().startswith("対象 3 枚"), lv.entry.count.text())
check(lv.mode_picked.isVisibleTo(lv.entry) and lv.mode_picked.isChecked(), "[選択] が出て、押されている")
check(lv.unpick_button.isVisibleTo(lv.entry) and not lv.star_box.isVisibleTo(lv.entry), "× 外す が出て、★の絞り込みは出ない")
check(recipes.load()[0].picked == files, "レシピに保存する")
check(Path(r.resolved_output_dir()) == pics / "★から動画", "出力の既定は先頭のファイルのフォルダ\\レシピ名")
window.grab().save(str(OUT / "send_recipe.png"))

# 消したファイルは数えない
(pics / "pick_3.png").rename(pics / "gone.png")
lv.refresh()
pump(0.3)
check(lv.entry.count.text().startswith("対象 2 枚") and "見つからない 1" in lv.breakdown.text(),
      "見つからないファイルは数えず、添える")
(pics / "gone.png").rename(pics / "pick_3.png")

# 量産する: 記録に origin・recipe・step が残る
lv._start()
wait_run()
run = window.engine.run
check(all(it.state == engine.DONE for it in run.items) and len(run.items) == 3, "送られた 3 枚で量産できる")
records = [x for x in comfy.read_history() if x.request.origin]
check(len(records) == 3 and {x.request.origin for x in records} == set(files), "記録に入口のファイル (origin)")
check(all(x.request.recipe == "★から動画" and x.request.step == 1 for x in records), "記録にレシピ名とステップ")

# 2. 量産が動いているあいだは入れない
lv._start()
pump(0.3)
check(lv.running(), "(量産を動かしている)")
warnings.clear()
send({"cmd": "send", "from": "J-Rate", "target": "recipe", "recipe": "★から動画", "index": 0,
      "files": [str(pics / "pick_4.png")]})
check(warnings and "動いている" in warnings[-1] and lv.recipe.picked == files, "量産中は入れずに知らせる")
wait_run()

# 3. PWC からテキスト 2 件をレシピ「テキストから」へ (並べ替えたあと: index が違っても名前で探す)
texts = [str(prompts / "a.txt"), str(prompts / "b.txt")]
send({"cmd": "send", "from": "PromptWordCrafter", "target": "recipe", "recipe": "テキストから", "index": 5,
      "files": texts})
check(lv.index == 1 and lv.recipe.is_picked and lv.recipe.texts, "テキストはテキストの入口として入る (名前で探す)")
check(lv.entry.detail.text() == "PromptWordCrafter から 2 件", lv.entry.detail.text())
check("a girl walking" in lv.breakdown.text(), "先頭の文を出す")
lv._start()
wait_run()
recs = [x for x in comfy.read_history() if x.request.recipe == "テキストから"]
check(len(recs) == 4, f"2 件 × 2 ステップの記録 ({len(recs)})")
check({x.request.step for x in recs} == {1, 2} and {x.request.origin for x in recs} == set(texts),
      "途中のステップの出力も、origin はテキストのまま")
check(all(x.request.origin_text in ("a girl walking in the rain", "a cat on the roof") for x in recs),
      "originText に始めたときの中身")

# 混ぜたもの・知らないレシピは入れない
warnings.clear()
send({"cmd": "send", "from": "X", "target": "recipe", "recipe": "テキストから", "index": 1,
      "files": [texts[0], files[0]]})
check(warnings and "だけ" in warnings[-1], "画像とテキストを混ぜたら知らせる")
send({"cmd": "send", "from": "X", "target": "recipe", "recipe": "ない", "index": 0, "files": files})
check("見つかりません" in warnings[-1], "知らないレシピは知らせる")

# × 外す で前の入口に戻る
lv._unpick()
pump(0.3)
check(lv.recipe.source == recipes.FOLDER and not lv.recipe.picked and not lv.mode_picked.isVisibleTo(lv.entry),
      "× 外す で前の入口に戻る")

# 4. 物語: 開いていなければ知らせる
warnings.clear()
send({"cmd": "send", "from": "J-Rate", "target": "story", "slot": "scenes", "files": files[:2]})
check(warnings and "開いていません" in warnings[-1], "物語を開いていなければ知らせる")
sv = window.story
st_dir = TMP / "stories" / "テスト"
story.Story.create(str(st_dir), "テスト")
sv.open_folder(str(st_dir))
pump(0.3)
first = sv.story.scenes[0]
send({"cmd": "send", "from": "J-Rate", "target": "story", "slot": "scenes", "files": files[:2]})
check(window.tabs.currentWidget() is sv, "物語のタブに切り替わる")
check(len(sv.story.scenes) == 3 and all(s.link == story.CUT and s.source_kind == story.SOURCE_FILE
                                        and Path(s.source).is_file() for s in sv.story.scenes[1:]),
      "新しいシーン (シーンチェンジ・手元の画像) を 1 枚ずつ足す")
check(sv.scene is sv.story.scenes[-1], "最後に足したシーンを選ぶ")
check(story.pick_folder() == str(pics), "選んだフォルダを覚える (J-Rate で選ぶ)")
check(json.loads((st_dir / story.STORY_FILE).read_text(encoding="utf-8-sig")) is not None, "(story.json を読める)")

# 選んでいるシーンの元画像・リファレンス・ラストフレーム
sv.select(first)
pump(0.2)
send({"cmd": "send", "from": "J-Rate", "target": "story", "slot": "source", "files": [files[2]]})
check(first.source_kind == story.SOURCE_FILE and Path(first.source).name == story.SOURCE_NAME, "元画像に入れる")
send({"cmd": "send", "from": "J-Rate", "target": "story", "slot": "reference", "files": [files[1]]})
check(Path(first.reference).name == story.REFERENCE_NAME, "リファレンスに入れる")
send({"cmd": "send", "from": "J-Rate", "target": "story", "slot": "last", "files": [files[0]]})
check(first.last_kind == story.LAST_FILE and Path(first.last_frame).is_file(), "ラストフレームに入れる")

# PWC: 元のプロンプトに (前の文は履歴に、Ctrl+Z で戻せる)
sv.original.setPlainText("前の元の文")
pump(0.2)
send({"cmd": "send", "from": "PromptWordCrafter", "target": "story", "slot": "original", "files": [texts[1]]})
check(first.original == "a cat on the roof" and sv.original.toPlainText() == "a cat on the roof", "元のプロンプトに入れる")
check(any(h["text"] == "前の元の文" for h in first.history), "入れる前の文を履歴に残す")
sv.original.undo()
check(sv.original.toPlainText() == "前の元の文", "Ctrl+Z で戻せる")
window.grab().save(str(OUT / "send_story.png"))

# 5. 絵コンテへのドロップ: 1 つ目のカードの上なら、そのあとに
pump(0.5)
card = sv.scene_cards[0]
mime = QMimeData()
mime.setUrls([QUrl.fromLocalFile(str(pics / "pick_4.png")), QUrl.fromLocalFile(str(pics / "pick_5.png"))])
pos = QPointF(card.geometry().center())
drop = QDropEvent(pos, Qt.DropAction.CopyAction, mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
sv._board_drop(drop)
pump(0.5)
check(len(sv.story.scenes) == 5 and sv.story.index_of(sv.scene) == 2, "カードの上に落とすと、そのあとに足す")
check(sv.story.scenes[1].link == story.CUT, "足したシーンはシーンチェンジ")
mime2 = QMimeData()
mime2.setUrls([QUrl.fromLocalFile(str(pics / "pick_1.png"))])
drop2 = QDropEvent(QPointF(sv.board_host.width() - 5, 5), Qt.DropAction.CopyAction, mime2,
                   Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
sv._board_drop(drop2)
pump(0.5)
check(len(sv.story.scenes) == 6 and sv.scene is sv.story.scenes[-1], "カードのないところなら最後に足す")
window.grab().save(str(OUT / "send_drop.png"))

# 6. --send: 動いていないときに起動する形 (JSON のファイルを読んで消す)
arg = TMP / "send.json"
arg.write_text(json.dumps({"cmd": "send", "target": "story", "slot": "scenes", "files": [files[0]]}), encoding="utf-8")
sys.argv = ["PrismLab.exe", main.SEND_ARG, str(arg)]
payload = main._send_arg()
check(json.loads(payload)["slot"] == "scenes" and not arg.exists(), "--send の JSON を読んで消す")
main.handle(window, payload)
pump(0.5)
check(len(sv.story.scenes) == 7, "--send で送られたものも入る")
main.handle(window, b"show")
check(len(sv.story.scenes) == 7, "show は前に出すだけ")

# 7. 別のプロセスから: PrismLab.exe --send で起動した 2 つ目が、動いているほうに渡して終わる (本番と同じ流れ)
import subprocess  # noqa: E402
arg.write_text(json.dumps({"cmd": "send", "target": "story", "slot": "scenes", "files": [files[1], files[2]]}),
               encoding="utf-8")
code = ("import sys; sys.path[:0] = [r'%s', r'%s']; from PySide6.QtWidgets import QApplication; "
        "from prismlab import main; main._SERVER_NAME = %r; app = QApplication([]); "
        "sys.argv = ['PrismLab.exe', '--send', r'%s']; "
        "sys.exit(0 if main._notify_running_instance(main._send_arg()) else 3)"
        % (ROOT.parent / "GenCommon", ROOT, main._SERVER_NAME, arg))
proc = subprocess.Popen([sys.executable, "-c", code], env={**os.environ, "QT_QPA_PLATFORM": "offscreen"})
end = time.time() + 20
while time.time() < end and proc.poll() is None:
    pump(0.05)
pump(0.5)
check(proc.returncode == 0, f"2 つ目は動いているほうに渡して終わる ({proc.returncode})")
check(len(sv.story.scenes) == 9 and not arg.exists(), "別のプロセスから送ったものが入る")
window.show_status("")
# 古い形 (改行なしの show) も今までどおり
proc = subprocess.Popen([sys.executable, "-c", code.replace("main._send_arg()", "b''")],
                        env={**os.environ, "QT_QPA_PLATFORM": "offscreen"})
end = time.time() + 20
while time.time() < end and proc.poll() is None:
    pump(0.05)
pump(0.3)
check(proc.returncode == 0 and len(sv.story.scenes) == 9, "2 重起動 (show) は前に出すだけ")

window.story.shutdown()
window.engine.stop_threads()
print(f"\nすべて OK ({len(ok)})  画像: {OUT}")
