"""ほかのアプリで台の設定を変えたら、動いている頼みの台も入れ替える (2026-11-23 利用者の希望)。

PWC で Falcon・Condor を使って頼んだあと、PWC の台の設定を Kiwi だけにしたのに、頼みは Falcon・Condor のまま動いていた。
台の設定の OK で {"cmd": "machines", "from", "machines"} が届き、そのアプリの動いている頼みの台を入れ替える。

    python tests\\test_external_machines.py
"""

import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="prismlab_extmach_"))
os.environ["APPDATA"] = str(TMP / "appdata")
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, str(ROOT.parent / "GenCommon"))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtGui import QColor, QImage  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from fakes import FakeComfy, workflow_i2v  # noqa: E402
from gencommon import store  # noqa: E402
from prismlab import engine, external, profile, recipes  # noqa: E402

app = QApplication(sys.argv)
profile.setup()
assert str(store.APP_DIR).startswith(str(TMP)), store.APP_DIR

failures = []


def check(cond, message):
    print(("OK   " if cond else "NG   ") + message)
    if not cond:
        failures.append(message)


WF = TMP / "wf"
WF.mkdir()
(WF / "i2v.json").write_text(json.dumps(workflow_i2v()), encoding="utf-8")
folder = TMP / "in"
folder.mkdir()
src = []
for i in range(8):
    img = QImage(32, 24, QImage.Format.Format_RGB32)
    img.fill(QColor(30 * i, 100, 200))
    p = folder / f"img{i + 1}.png"
    img.save(str(p))
    src.append(p)

a = FakeComfy(polls=4)  # 1 件に少し時間がかかる (途中で台を変えられるように)
b = FakeComfy(polls=1)
c = FakeComfy(polls=1)

r = recipes.Recipe("台の入れ替え", folder=str(folder), inherit=False,
                   stages=[recipes.Stage(recipes.GEN, workflow=str(WF / "i2v.json"), seconds=3)])
runs = external.ExternalRuns()
req = external.Request("t1", "PromptWordCrafter", r.name)
runs.requests[req.id] = req
run = engine.Run.create(r, src)
run.recipe["external"] = {"id": req.id, "app": req.app, "machines": [a.url], "created": req.created}
runs._launch(req, run, [a.url])  # noqa: SLF001

# A で 1 件目が動きだすまで待つ
end = time.time() + 20
while time.time() < end and not a.received:
    app.processEvents()
    time.sleep(0.05)
check(bool(a.received), "1: はじめは A で作る")

# ほかのアプリ (MuseFactory) の台の設定は、PWC の頼みには効かない
check(runs.set_machines({"cmd": "machines", "from": "MuseFactory", "machines": [c.url]}) == 0,
      "2: ほかのアプリの台の設定では入れ替えない")

# PWC の台の設定を B だけにした
n = runs.set_machines({"cmd": "machines", "from": "PromptWordCrafter", "machines": [b.url + "/"]})
check(n == 1, f"3: PWC の動いている頼みの台を入れ替えた ({n})")
check([m.url for m in req.engine.machines] == [b.url], f"3: 台は B だけ ({[m.url for m in req.engine.machines]})")
check(run.recipe["external"]["machines"] == [b.url], "3: 続きのファイルにも新しい台")
saved = json.loads(store.read_text(store.app_file(f"{external.EXT_DIR}/{req.id}.json")))
check(saved["recipe"]["external"]["machines"] == [b.url], "3: 続きのファイルに書いた")

end = time.time() + 60
while time.time() < end and req.finished_at is None:
    app.processEvents()
    time.sleep(0.05)
states = [i.state for i in run.items]
check(req.finished_at is not None and all(s == engine.DONE for s in states), f"4: 最後まで作った ({states})")
check(len(a.received) <= 2, f"4: A では動いていた件だけ ({len(a.received)} 件)")
check(len(b.received) >= len(src) - 2, f"4: 残りは B で作った ({len(b.received)} 件)")
check(not c.received, "4: C (MuseFactory の台) には送らない")
st = req.to_status()
check([m["url"] for m in st["machines"]] == [b.url], "4: 進み具合の台も B")

runs.stop_threads()
for f in (a, b, c):
    f.close()
print()
if failures:
    print(f"NG {len(failures)} 件")
    sys.exit(1)
print("すべて OK")
