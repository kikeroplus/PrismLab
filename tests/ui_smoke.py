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
window.line._start()
pump(7)
window.grab().save(str(OUT / "prismlab_running.png"))
end = time.time() + 90
while time.time() < end and window.engine.active:
    pump(0.3)
pump(1.5)
window.grab().save(str(OUT / "prismlab_done.png"))
run = window.engine.run
print("完了:", sum(1 for i in run.items if i.state == engine.DONE), "/", len(run.items),
      "台:", [m.done for m in window.engine.machines])
window.engine.stop_threads()
for s in (a, b, llm):
    s.close()
window.close()
shutil.rmtree(TMP, ignore_errors=True)
