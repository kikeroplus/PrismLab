"""ステップのコピー・貼り付け (レシピをまたぐ) と、台の休み (「使う」のチェック) のテスト。
偽の ComfyUI 2 台で、休みの台に送らないこと・量産の途中で休みにしたら新しい件はもう 1 台に回ることを見る。

    python tests\\test_clip_machines.py
APPDATA を一時フォルダに差し替えるので、実際の設定には触れない。
"""

import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="prismlab_clip_"))
os.environ["APPDATA"] = str(TMP / "appdata")
sys.path.insert(0, str(ROOT.parent / "GenCommon"))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtGui import QColor, QImage  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from fakes import FakeComfy, FakeLLM, workflow_i2v  # noqa: E402
from gencommon import comfy, settings, store  # noqa: E402
from prismlab import engine, profile, recipes, theme  # noqa: E402
from prismlab.dialogs import MachinesDialog  # noqa: E402
from prismlab.main_window import MainWindow  # noqa: E402

app = QApplication(sys.argv)
theme.apply(app)
profile.setup()
assert str(store.APP_DIR).startswith(str(TMP))
QMessageBox.warning = staticmethod(lambda *a, **k: None)
QMessageBox.information = staticmethod(lambda *a, **k: None)

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


a, b, llm = FakeComfy(polls=3), FakeComfy(polls=3), FakeLLM()
settings.set_llm_url(llm.url)
comfy.set_connections([comfy.Preset("FALCON", a.url), comfy.Preset("CONDOR", b.url)]
                      + [comfy.Preset() for _ in range(comfy.PRESET_COUNT - 2)])
wf = TMP / "Wan I2V.json"
wf.write_text(json.dumps(workflow_i2v()), encoding="utf-8")
comfy.set_workflows([comfy.Preset("Wan I2V", str(wf))] + [comfy.Preset() for _ in range(comfy.WORKFLOW_COUNT - 1)])
recipes.set_machines([a.url, b.url])

photo = TMP / "元.png"
img = QImage(160, 100, QImage.Format.Format_RGB32)
img.fill(QColor(40, 160, 90))
img.save(str(photo))

recipes.save([
    recipes.Recipe("レシピ1", folder=str(TMP), stages=[
        recipes.Stage(recipes.TEXT, text="red hat"),
        recipes.Stage(recipes.SAVE, memo="メモ A"),
        recipes.Stage(recipes.GEN, workflow=str(wf), seconds=3.0)]),
    recipes.Recipe("レシピ2", folder=str(TMP), stages=[recipes.Stage(recipes.GEN, workflow=str(wf))]),
])
recipes.set_last_index(0)

window = MainWindow()
window.resize(1360, 900)
window.show()
lv = window.line
pump(0.5)
said = []
lv.status.connect(said.append)

# 1. コピー・貼り付け (レシピをまたぐ)
menu = lv._type_menu(lv, lambda t: None, paste_at=lambda: 0)
menu.aboutToShow.emit()
check(not any(act.text().startswith("貼り付け") for act in menu.actions()), "コピーする前は貼り付けを出さない")
check(menu.toolTipsVisible() and len(menu.actions()) == len(recipes.STAGE_ORDER)
      and all(act.toolTip() and act.toolTip() != act.text().replace("&", "") for act in menu.actions()),
      "＋ ステップのメニューの項目に、重ねると出る説明")
lv._copy_stages(0, 1)
check(len(lv.clipboard) == 1 and lv.clipboard[0]["type"] == recipes.TEXT, "1 つコピー")
menu.aboutToShow.emit()
check(menu.actions()[0].text() == "貼り付け: 追加テキスト", "コピーしたら → の挿入のメニューに貼り付けが出る")

lv._select_recipe(1)
lv._paste_stages(0)
r2 = lv.recipes[1]
check([s.type for s in r2.stages] == [recipes.TEXT, recipes.GEN] and r2.stages[0].text == "red hat",
      "ほかのレシピの左端に貼り付け (中身もそのまま)")
check("「レシピ1」から" in said[-1], f"どこから貼ったか知らせる ({said[-1]})")
r2.stages[0].text = "changed"
check(lv.recipes[0].stages[0].text == "red hat", "貼ったものを直しても、コピー元は変わらない")

lv._select_recipe(0)
lv._copy_stages(1, len(lv.recipe.stages))  # 覚える + 生成
lv._select_recipe(1)
lv._paste_stages(len(lv.recipe.stages))
check([s.type for s in lv.recipes[1].stages] == [recipes.TEXT, recipes.GEN, recipes.SAVE, recipes.GEN]
      and lv.recipes[1].stages[2].memo == "メモ A" and lv.recipes[1].stages[3].seconds == 3.0,
      "ここから右をまとめてコピーし、右端に貼り付け")
saved = recipes.load()
check([s.type for s in saved[1].stages] == [recipes.TEXT, recipes.GEN, recipes.SAVE, recipes.GEN], "貼り付けたら保存")
lv._paste_stages(1)
lv._paste_stages(1)
check(lv.recipes[1].stages[1].memo == lv.recipes[1].stages[3].memo == "メモ A"
      and lv.recipes[1].stages[1] is not lv.recipes[1].stages[3], "何度でも貼れる (別のものとして)")

# 2. 台の休み
lv._select_recipe(1)
lv.recipes[1].stages = [recipes.Stage(recipes.GEN, workflow=str(wf))]
lv._save_recipes()
lv.refresh(rebuild=True)
card_b = lv.machine_cards[1]
check(card_b.use.isVisible() and card_b.use.isChecked(), "台の札に「使う」のチェック (初めは入り)")
card_b.use.click()  # 人が外す
pump(0.4)
check(recipes.machines() == [a.url] and recipes.all_machines() == [a.url, b.url], "休みにしても台の設定には残す")
check((store.app_file(recipes.MACHINES_OFF_FILE)).read_text(encoding="utf-8").strip() == b.url, "休みはファイルに残す")
check([m.url for m in lv.engine.machines] == [a.url], "エンジンは休みの台を使わない")
check(not card_b.use.isChecked() and "休み" in card_b.status.text() and card_b.isVisible(),
      f"休みの台の札も出して、休みと書く ({card_b.status.text()})")
check("休み: CONDOR" in window.story.machines_b.text(), f"物語のタブの台の札にも休み ({window.story.machines_b.text()})")

lv._start()
end = time.time() + 30
while time.time() < end and lv.running():
    pump(0.2)
pump(0.5)
check(len(a.received) == 1 and not b.received, "休みの台には送らない")

# 台の設定の窓: 休みのチェックが出て、そのまま保存できる
dialog = MachinesDialog(recipes.all_machines(), lv, off=recipes.machines_off())
check([u.isChecked() for u in dialog.uses] == [True, False, True], "台の設定の窓にも「使う」(休みは外れている)")
check({u.lower() for u in dialog.off()} == {b.url.lower()}, "窓の休みを読む")
dialog.uses[1].setChecked(True)
recipes.set_machines(dialog.urls(), dialog.off())
lv._apply_machines()
check(recipes.machines() == [a.url, b.url] and card_b.use.isChecked(), "窓でチェックを入れたら戻る")

# 3. 量産の途中で休みにする: 動いている件は最後まで、新しい件はもう 1 台へ
a.polls = b.polls = 8
lv.recipes[1].folder = str(TMP)
for n in range(5):
    img.save(str(TMP / f"元{n}.png"))
before_a, before_b = len(a.received), len(b.received)
lv._start()
end = time.time() + 15
while time.time() < end and len(a.received) - before_a < 1:
    pump(0.1)
lv.machine_cards[0].use.click()  # FALCON を休みに
pump(0.3)
sent_a = len(a.received)
end = time.time() + 60
while time.time() < end and lv.running():
    pump(0.2)
pump(0.5)
run, items, _m = lv.engine.snapshot()
check(all(it.state == engine.DONE for it in items), f"全部できる ({[it.state for it in items]})")
check(len(a.received) == sent_a, "休みにしたあとは、その台に新しく送らない")
check(len(b.received) - before_b == len(items) - (sent_a - before_a), "残りはもう 1 台で作る")
check(not any(t.name.startswith(f"prismlab-gen-{a.url}") for t in __import__("threading").enumerate()),
      "休みの台のスレッドは、動いている件が終わったら止まる")

lv.machine_cards[0].use.click()
pump(0.2)
check(recipes.machines() == [a.url, b.url], "チェックを入れ直せば戻る")
recipes.set_machine_on(a.url, False)
recipes.set_machine_on(b.url, False)
check(recipes.machines() == [] and "休み" in recipes.no_machines_reason(), "全部休みなら、そう知らせる")

window.close()
print(f"\nすべて OK ({len(ok)})")
