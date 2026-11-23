"""台の空きの分け合い (engine.SlotPool) のテスト (2026-11-23 利用者の希望)。

- 量産ラインが台を埋めているところに、ほかのアプリの頼み (別のエンジン) が来ても、ずっと待たせない
  (次に空いた空きは、使っている数の少ない側へ)。
- 量産ラインを止めて台を空けたら、待っている側がすぐに使う。
- 物語 (先にする側) は今までどおり先。

    python tests\\test_fair_pool.py
"""

import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="prismlab_pool_"))
os.environ["APPDATA"] = str(TMP / "appdata")
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, str(ROOT.parent / "GenCommon"))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtGui import QColor, QImage  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from fakes import FakeComfy, workflow_i2v  # noqa: E402
from gencommon import store  # noqa: E402
from prismlab import engine, profile, recipes  # noqa: E402

app = QApplication(sys.argv)
profile.setup()
assert str(store.APP_DIR).startswith(str(TMP)), store.APP_DIR

failures = []


def check(cond, message):
    print(("OK   " if cond else "NG   ") + message)
    if not cond:
        failures.append(message)


# 1. 置き場だけで (スレッドなし)
P = engine.SlotPool()
LINE, PWC, STORY = 1, 2, 3
A, B = "http://a", "http://b"
for k in (LINE, PWC):
    P.alive(k, A)
    P.alive(k, B)
P.set_demand(LINE, False, 10, {})
check(P.take(LINE, A) and P.take(LINE, B) and P.take(LINE, A) and P.take(LINE, B), "1: 量産ラインが 2 台 × 2 件")
check(not P.take(LINE, A), "1: 1 台に 3 件目は送らない")
P.set_demand(PWC, False, 3, {})
P.put_back(LINE, A)
check(not P.take(LINE, A), "1: 空いた空きは、使っている数の少ない側 (PWC) の番 (量産ラインは取れない)")
check(P.take(PWC, A), "1: PWC が取る")
P.put_back(LINE, B)
check(not P.take(LINE, B) and P.take(PWC, B), "1: 次の空きも PWC (量産 3 件・PWC 1 件)")
P.put_back(LINE, A)
check(P.take(LINE, A) and not P.take(PWC, A), "1: 量産 1 件・PWC 2 件になったら、量産ラインの番")
P.set_demand(STORY, True, 1, {})
P.alive(STORY, A)
P.put_back(PWC, A)
check(not P.take(LINE, A) and not P.take(PWC, A) and P.take(STORY, A), "1: 物語が待っていれば物語が先")
P.set_demand(PWC, False, 0, {}, None)
P.set_demand(STORY, True, 0, {})
P.alive(PWC, A)
P.put_back(LINE, A)
check(P.take(LINE, A), "1: 待っていない側は数えない")
P.set_demand(LINE, False, 0, {A: 1})
P.put_back(LINE, A)
P.set_demand(PWC, False, 3, {})
check(P.take(LINE, A, pinned=True), "1: 頼み済み (その台の待ち行列にある) の件は、空きがあれば順番を見ない")
# 譲る先は、そのエンジンが使える台だけ (ほかのエンジンだけが使う空いた台があっても、自分の台を使う)
Q = engine.SlotPool()
Q.alive(LINE, B)
Q.set_demand(LINE, False, 0, {})  # 量産ラインは止めている (台 B は量産ラインだけの台)
Q.alive(PWC, A)
Q.set_demand(PWC, False, 5, {})
check(Q.take(PWC, A) and Q.take(PWC, A), "1: ほかのエンジンだけの空いた台があっても、自分の台に 2 件送る")

# 2. 本物のエンジンで: 量産ラインが 2 台を埋めているところに、ほかのアプリの頼み
WF = TMP / "wf"
WF.mkdir()
(WF / "i2v.json").write_text(json.dumps(workflow_i2v()), encoding="utf-8")


def make_images(folder: Path, n: int) -> list[Path]:
    folder.mkdir(parents=True, exist_ok=True)
    paths = []
    for i in range(n):
        img = QImage(32, 24, QImage.Format.Format_RGB32)
        img.fill(QColor(10 * i, 100, 200))
        p = folder / f"img{i + 1}.png"
        img.save(str(p))
        paths.append(p)
    return paths


def gen_recipe(name, folder) -> recipes.Recipe:
    return recipes.Recipe(name, folder=str(folder), inherit=False,
                          stages=[recipes.Stage(recipes.GEN, workflow=str(WF / "i2v.json"), seconds=3)])


taken = []  # 空きをもらったエンジンの番号 (もらった順に。台の空きの置き場で数える)
_take = engine.POOL.take


def _logged_take(key, url, pinned=False):
    ok = _take(key, url, pinned)
    if ok:
        taken.append(key)
    return ok


engine.POOL.take = _logged_take


def pump(seconds, _engines=None):
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        time.sleep(0.05)


def running(eng):
    return sum(1 for it in eng.run.items if it.state == engine.RUNNING)


a, b = FakeComfy(polls=3), FakeComfy(polls=3)
line = engine.Engine(run_file=None)
line.set_machines([a.url, b.url])
line.start(engine.Run.create(gen_recipe("量産", TMP / "l"), make_images(TMP / "l", 16)))
engines = {"量産": line}
pump(3, engines)
check(running(line) == 4, f"2: 量産ラインが 2 台 × 2 件 ({running(line)})")
ext = engine.Engine(run_file=None)
ext.set_machines([a.url, b.url])
ext.start(engine.Run.create(gen_recipe("PWC", TMP / "p"), make_images(TMP / "p", 6)))
engines["PWC"] = ext
before = len(taken)
end = time.time() + 40
while time.time() < end and running(ext) < 2:
    pump(0.2, engines)
order = ["PWC" if k == ext._key else "量産" for k in taken[before:]]
check(order[:2] == ["PWC", "PWC"], f"2: 空いた空きは、あとから頼んだ側に先に回す: {order}")
pump(1, engines)
check(running(ext) == 2 and running(line) == 2, f"2: 2 件ずつに分ける (量産 {running(line)} / PWC {running(ext)})")
waiting_line = sum(1 for it in line.run.items if it.state == engine.WAITING)
check(waiting_line > 0, "2: 量産ラインにはまだ待っている件がある (分け合っている)")

# 3. 量産ラインを一時停止して台を空けたら、PWC がすぐに 4 件 (2 台 × 2) 使う
line.set_paused(True)
end = time.time() + 40
peak = 0
while time.time() < end and peak < 4:
    pump(0.2, engines)
    peak = max(peak, running(ext))
check(peak == 4, f"3: 空けた台を PWC が使う (同時に {peak} 件)")
check(max(engine.POOL.peak.values()) <= 2, f"3: どの台も同時に 2 件まで ({engine.POOL.peak})")
ext.cancel_all()
line.cancel_all()
pump(8, engines)
line.stop_threads()
ext.stop_threads()
a.close()
b.close()

print()
if failures:
    print(f"NG {len(failures)} 件")
    sys.exit(1)
print("すべて OK")
