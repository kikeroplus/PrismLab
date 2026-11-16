"""画面の確認: 偽の ComfyUI 2 台と偽の LLM で「★から動画」を回し、途中と終わったときの画面を PNG に撮る。

    python tests\\ui_smoke.py (撮った画像の置き場所)
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
TMP = Path(tempfile.mkdtemp(prefix="prismlab_ui_"))
OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else TMP
os.environ["APPDATA"] = str(TMP / "appdata")
sys.path.insert(0, str(ROOT.parent / "GenCommon"))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtGui import QColor, QImage  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from fakes import FakeComfy, FakeLLM, workflow_cn, workflow_i2v  # noqa: E402
from gencommon import comfy, enhancer, reader, settings, store  # noqa: E402
from prismlab import engine, profile, recipes, theme  # noqa: E402
from prismlab.main import make_icon  # noqa: E402
from prismlab.main_window import MainWindow  # noqa: E402

app = QApplication(sys.argv)
app.setWindowIcon(make_icon())
theme.apply(app)
profile.setup()
assert str(store.APP_DIR).startswith(str(TMP))

a, b, llm = FakeComfy(polls=4), FakeComfy(polls=6), FakeLLM()
# 動画は本物の短い mp4 (赤 → 最後のコマだけ青): ステップのサムネールに最後のコマを重ねるのを確かめる
from fakes import make_video  # noqa: E402
clip = TMP / "clip.mp4"
make_video(clip, [(200, 40, 40)] * 7 + [(40, 60, 220)], size=(160, 160))
a.video_bytes = b.video_bytes = clip.read_bytes()
settings.set_llm_url(llm.url)
comfy.set_connections([comfy.Preset("メイン", a.url), comfy.Preset("サブ", b.url)]
                      + [comfy.Preset() for _ in range(comfy.PRESET_COUNT - 2)])
wf_dir = TMP / "wf"
wf_dir.mkdir()
(wf_dir / "Wan I2V.json").write_text(json.dumps(workflow_i2v()), encoding="utf-8")
(wf_dir / "Depth Clone.json").write_text(json.dumps(workflow_cn()), encoding="utf-8")
comfy.set_workflows([comfy.Preset("Wan I2V", str(wf_dir / "Wan I2V.json")),
                     comfy.Preset("Depth Clone", str(wf_dir / "Depth Clone.json"))]
                    + [comfy.Preset() for _ in range(comfy.WORKFLOW_COUNT - 2)])
recipes.set_machines([a.url, b.url])

src = TMP / "T2I_1114"
src.mkdir()
for i in range(8):
    img = QImage(300, 400, QImage.Format.Format_RGB32)
    img.fill(QColor.fromHsv(i * 40 % 360, 160, 200))
    img.save(str(src / f"girl_{i + 1:03}.png"))

READER = next(p.name for p in reader.load_presets() if not p.is_empty)
ENHANCER = next(p.name for p in enhancer.load_presets() if not p.is_empty)
rs = recipes.default_recipes()
rs[0].folder, rs[0].min_stars = str(src), 0
rs[0].stages = [recipes.Stage(recipes.READ, reader=READER), recipes.Stage(recipes.ENHANCE, enhancer=ENHANCER),
                recipes.Stage(recipes.GEN, workflow=str(wf_dir / "Wan I2V.json"), seconds=5)]
rs[1].folder = str(src)
rs[1].stages = [recipes.Stage(recipes.GEN, workflow=str(wf_dir / "Depth Clone.json"))]
recipes.save(rs)

window = MainWindow()
window.resize(1280, 860)
window.show()


def pump(seconds: float):
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        time.sleep(0.03)


pump(1.5)
window.grab().save(str(OUT / "prismlab_idle.png"))

# 入口の★: J-Rate で★を付けて戻ったときの読み直し (★の読み取りは差し替える)
from prismlab import rating
fake_stars = {}
real_get_stars = rating.get_stars
rating.get_stars = lambda p: fake_stars.get(Path(p).name, 0)
window.line.rescan()
pump(1)
assert window.line.breakdown.text() == "なし 8", window.line.breakdown.text()
assert window.line.rate_button.isVisible()
fake_stars.update({"girl_001.png": 5, "girl_002.png": 4, "girl_003.png": 4})
window.line.rescan()
pump(1)
bd = window.line.breakdown.text()
assert "★5</span> 1" in bd and "★4</span> 2" in bd and bd.endswith("なし 5"), bd
window.line.star_box.setCurrentIndex(4)
pump(0.5)
assert window.line.entry.count.text().startswith("対象 3 枚"), window.line.entry.count.text()
window.grab().save(str(OUT / "prismlab_stars.png"))
window.line.star_box.setCurrentIndex(0)
rating.get_stars = real_get_stars
window.line.rescan()
pump(1)
# ステップの挿入・種類の変更 (設定の窓は OK を押したことにする)
from prismlab import line_view
from prismlab.widgets import InsertArrow
real_dialog = line_view.StageDialog
line_view.StageDialog = type("OkDialog", (), {"__init__": lambda self, *a: None, "exec": lambda self: 1})
lv = window.line
saved = [st.to_dict() for st in lv.recipe.stages]
assert len(lv.flow_host.findChildren(InsertArrow)) == len(saved) + 1
lv._add_stage(recipes.ENHANCE, 1)
assert [st.type for st in lv.recipe.stages] == ["read", "enhance", "enhance", "gen"], [st.type for st in lv.recipe.stages]
lv._change_stage_type(1, recipes.READ)
assert [st.type for st in lv.recipe.stages] == ["read", "read", "enhance", "gen"]
pump(0.5)
assert len(lv.stage_cards) == 4 and lv.stage_cards[1].title.text() == "読み取り"
# バイパス: 盛るは入れられる。読み取り (盛るの元) とただ 1 つの生成は入れられない (スイッチを出さない)
lv.recipe.stages = [recipes.Stage.from_dict(d) for d in saved]
lv.refresh(rebuild=True)
pump(0.3)
assert [c.bypass.isVisibleTo(c) for c in lv.stage_cards] == [False, True, False],     [c.bypass.isVisibleTo(c) for c in lv.stage_cards]
lv.stage_cards[1].bypass.click()
pump(0.5)
assert lv.recipe.stages[1].bypass and lv.stage_cards[1]._bypassed
assert lv.stage_cards[1].count.text() == "バイパス中"
window.grab().save(str(OUT / "prismlab_bypass.png"))
lv.stage_cards[1].bypass.click()
pump(0.5)
assert not lv.recipe.stages[1].bypass
# 追加テキスト・差し込みのステップ
lines = TMP / "lines"
lines.mkdir()
for n in (1, 2, 3):
    (lines / f"{n:03}.txt").write_text(f"line {n}", encoding="utf-8")
lv.recipe.stages = [recipes.Stage.from_dict(d) for d in saved]
lv.recipe.stages.insert(2, recipes.Stage(recipes.INSERT, folder=str(lines)))
lv.recipe.stages.insert(1, recipes.Stage(recipes.TEXT, text="slow camera pan, cinematic lighting"))
lv.refresh(rebuild=True)
pump(0.5)
titles = [c.title.text() for c in lv.stage_cards]
assert titles == ["読み取り", "追加テキスト", "盛る", "差し込み", "生成"], titles
assert "次: 001.txt (1 / 3)" in lv.stage_cards[3].detail.text(), lv.stage_cards[3].detail.text()
window.grab().save(str(OUT / "prismlab_add_steps.png"))
lv.recipe.stages = [recipes.Stage.from_dict(d) for d in saved]
lv._save_recipes()
lv.refresh(rebuild=True)
line_view.StageDialog = real_dialog
pump(0.5)

# 1 枚で試す: 入口の 2 枚目をクリックで選んで、試作する
lv.refresh()
pump(1)
lv.entry.thumbs.clicked.emit(1)
pump(0.3)
assert "girl_002.png" in lv.entry.count.text(), lv.entry.count.text()
lv.trial_button.click()
end = time.time() + 60
while time.time() < end and window.engine.active and window.engine.run.unfinished:
    pump(0.3)
pump(1)
trial_run = window.engine.run
assert trial_run.trial and len(trial_run.items) == 1 and trial_run.items[0].state == engine.DONE
assert Path(trial_run.items[0].outputs[-1]["path"]).parent.name == "試作"
assert "girl_002.png" in lv.prompt_view.toPlainText() and "盛った文" in lv.prompt_view.toPlainText(),     lv.prompt_view.toPlainText()
window.grab().save(str(OUT / "prismlab_trial.png"))
lv.results.setCurrentRow(0)
pump(0.3)
assert "読み取った文" in lv.prompt_view.toPlainText()

window.line._start()
pump(7)
window.grab().save(str(OUT / "prismlab_running.png"))
end = time.time() + 90
while time.time() < end and window.engine.active:
    pump(0.3)
pump(1.5)
# 生成のステップのサムネール: いつもの絵の右下に、最後のコマ (青) を小さく重ねる
gen_card = lv.stage_cards[-1]
end = time.time() + 10
while time.time() < end:
    pm = gen_card.thumbs.labels[0].pixmap()
    if pm is not None and not pm.isNull():
        corner = pm.toImage().pixelColor(pm.width() - 8, pm.height() - 8)
        if corner.blue() > 150 and corner.red() < 120:
            break
    pump(0.3)
pm = gen_card.thumbs.labels[0].pixmap()
assert pm is not None and not pm.isNull(), "生成のステップにサムネールがない"
corner = pm.toImage().pixelColor(pm.width() - 8, pm.height() - 8)
middle = pm.toImage().pixelColor(pm.width() // 4, pm.height() // 4)
assert corner.blue() > 150 and corner.red() < 120, ("右下に最後のコマ (青)", corner.getRgb())
assert middle.red() > 150 and middle.blue() < 120, ("いつもの絵 (赤) はそのまま", middle.getRgb())
window.grab().save(str(OUT / "prismlab_done.png"))
run = window.engine.run
print("完了:", sum(1 for i in run.items if i.state == engine.DONE), "/", len(run.items),
      "台:", [m.done for m in window.engine.machines])
# 入口を 1 枚に: 回数ぶん同じ画像を通す
lv = window.line
lv.mode_single.click()
pump(0.5)
assert lv.recipe.single and lv.repeat_box.isVisibleTo(lv.entry) and not lv.star_box.isVisibleTo(lv.entry)
assert lv.entry.detail.text().startswith("クリックして画像を選ぶ"), lv.entry.detail.text()
lv._set_image(str(src / "girl_005.png"))
lv.repeat_box.setValue(2)
pump(0.5)
assert lv.entry.detail.text() == "画像: girl_005.png" and lv.entry.count.text() == "2 回通す", lv.entry.count.text()
lv._start()
end = time.time() + 90
while time.time() < end and window.engine.active and window.engine.run.unfinished:
    pump(0.3)
pump(1)
single_run = window.engine.run
assert len(single_run.items) == 2 and all(i.state == engine.DONE for i in single_run.items)
assert all(Path(i.source).name == "girl_005.png" for i in single_run.items)
window.grab().save(str(OUT / "prismlab_single.png"))
lv.mode_folder.click()
pump(0.5)
assert not lv.recipe.single and lv.star_box.isVisibleTo(lv.entry)

# テキストの入口と回数
txt = TMP / "prompts"
txt.mkdir()
(txt / "001.txt").write_text("a red fox walking in deep snow, morning light", encoding="utf-8")
(txt / "002.txt").write_text("a lighthouse at dusk", encoding="utf-8")
lv.mode_texts.click()
pump(0.5)
lv.recipe.folder = str(txt)
lv.repeat_box.setValue(3)
pump(0.5)
assert lv.recipe.texts and not lv.star_box.isVisibleTo(lv.entry) and not lv.entry.thumbs.isVisibleTo(lv.entry)
assert lv.entry.count.text().startswith("対象 2 件 × 3 回"), lv.entry.count.text()
assert "a red fox" in lv.breakdown.text(), lv.breakdown.text()
assert not lv.inherit_box.isEnabled()
lv.refresh(rebuild=True)
pump(0.3)
assert "画像がまだありません" in lv.stage_cards[0].io.text(), lv.stage_cards[0].io.text()  # 読み取りが先頭
window.grab().save(str(OUT / "prismlab_texts.png"))
lv.repeat_box.setValue(1)
lv.mode_folder.click()
pump(0.5)
assert lv.inherit_box.isEnabled() and lv.inherit_box.isChecked()

# 結果のサムネールに乗せると、横に大きく出す
from PySide6.QtCore import QEvent, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QMouseEvent  # noqa: E402

window.activateWindow()
pump(0.3)
vp = lv.results.viewport()


def hover(row):
    center = QPointF(lv.results.visualItemRect(lv.results.item(row)).center())
    QApplication.sendEvent(vp, QMouseEvent(QEvent.Type.MouseMove, center, vp.mapToGlobal(center),
                                           Qt.MouseButton.NoButton, Qt.MouseButton.NoButton,
                                           Qt.KeyboardModifier.NoModifier))


assert lv.results.count() >= 2 and window.isActiveWindow()
hover(0)
pump(0.1)
assert not lv.hover.popup.isVisible(), "すぐには出さない"
pump(1.0)
popup = lv.hover.popup
first = lv.results.item(0).data(Qt.ItemDataRole.UserRole)
assert popup.isVisible() and Path(first).name in popup.caption.text(), popup.caption.text()
assert popup.image.pixmap().width() > 200 or popup.image.pixmap().height() > 200, popup.image.pixmap().size()
assert lv.results.item(0).toolTip() == "", "成功したものはツールチップを出さない (拡大と重ねない)"
popup.grab().save(str(OUT / "prismlab_hover.png"))
hover(1)
pump(0.2)
assert Path(lv.results.item(1).data(Qt.ItemDataRole.UserRole)).name in popup.caption.text(), "出ているあいだは隣へすぐ移る"
QApplication.sendEvent(vp, QEvent(QEvent.Type.Leave))
pump(0.1)
assert not popup.isVisible(), "離れたら消す"

window.engine.stop_threads()
for s in (a, b, llm):
    s.close()
window.close()
shutil.rmtree(TMP, ignore_errors=True)
