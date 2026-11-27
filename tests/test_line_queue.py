"""量産ラインの待ち行列 (line_queue) のテスト (2026-11-27)。偽の ComfyUI を使い、APPDATA を一時フォルダに差し替える。

    python tests\\test_line_queue.py
"""

import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="prismlab_queue_"))
os.environ["APPDATA"] = str(TMP / "appdata")  # gencommon を読み込む前に差し替える (実際の設定に触れない)
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, str(ROOT.parent / "GenCommon"))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtGui import QColor, QImage  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from fakes import FakeComfy, workflow_cn  # noqa: E402
from gencommon import store  # noqa: E402
from prismlab import engine, line_queue, profile, recipes  # noqa: E402

app = QApplication(sys.argv)
profile.setup()
assert str(store.APP_DIR).startswith(str(TMP)), store.APP_DIR

WF = TMP / "wf"
WF.mkdir()
(WF / "cn.json").write_text(json.dumps(workflow_cn()), encoding="utf-8")
I2I = str(WF / "cn.json")
stars: dict = {}
engine.rating.get_stars = lambda path: stars.get(str(path), 0)  # J-Rate で付けた★のかわり

failures = []


def check(cond, message):
    print(("OK   " if cond else "NG   ") + message)
    if not cond:
        failures.append(message)


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


def pump_until(cond, seconds: float = 60) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.03)
    app.processEvents()
    return cond()


src = make_images(TMP / "in", 4)
ins = TMP / "セリフ"
ins.mkdir()
for n in range(1, 9):
    (ins / f"{n:02d}.txt").write_text(f"line{n}", encoding="utf-8")
comfy_a = FakeComfy(polls=4)

# 1. 量産中に入れたものは待ち、入れたあとにレシピを直しても写しは変わらない。差し込みは続きのファイルから
q = line_queue.LineQueue()
q.set_machines([comfy_a.url])
finished, started = [], []
q.job_finished.connect(lambda j: finished.append(j.name))
q.job_started.connect(lambda j: started.append(j.name))
r = recipes.Recipe("A", folder=str(src[0].parent), stages=[recipes.Stage(recipes.INSERT, folder=str(ins)),
                                                           recipes.Stage(recipes.GEN, workflow=I2I)])
check(not q.line_busy(), "1: 何もなければ開始はすぐ始まる")
ja = q.add(engine.Run.create(r.copy(), src[:2]), r)
check(not ja.waiting and ja.engine.active, "1: 1 本目はすぐ始める")
check(q.line_busy(), "1: 量産が動いているあいだは、開始は待ち行列に並ぶ")
r.stages.insert(1, recipes.Stage(recipes.TEXT, text="changed"))
jb = q.add(engine.Run.create(r.copy(), src[:2]), r)
check(jb.waiting and not jb.engine.active and q.place(jb) == 1, "1: 2 本目は待つ (待ち行列の 1 本目)")
r.stages[1].text = "edited after queue"
check(jb.run.stages[1]["text"] == "changed", "1: 入れたあとにレシピを直しても、入れた写しは変わらない")
plan_a = sorted(json.dumps(v, sort_keys=True) for it in ja.run.items for v in it.plan.values())
plan_b = sorted(json.dumps(v, sort_keys=True) for it in jb.run.items for v in it.plan.values())
check(not set(plan_a) & set(plan_b), f"1: 同じレシピを続けて入れても、差し込みは続きのファイルから ({plan_a} / {plan_b})")
# 試作は並ばずにすぐ動く
jt = q.add(engine.Run.create(r.copy(), src[:1], trial=True), r)
check(not jt.waiting and jt.engine.active and ja.engine.active, "1: 試作は量産中でも待たずに動く")
check(q.line_busy() and jb.waiting, "1: 試作を入れても、待っている量産はそのまま")
saved = json.loads((store.APP_DIR / line_queue.LINE_DIR / f"{jb.id}.json").read_text(encoding="utf-8"))
check(saved["recipe"]["line"]["waiting"] is True, "1: 待っているものも line\\<id>.json に書く")
ok = pump_until(lambda: not ja.unfinished and not jb.waiting)
check(ok, "1: 1 本目が終わったら 2 本目を始める")
check("A" in finished and "A" in started, f"1: 終わった・始めたを知らせる ({finished} / {started})")
pump_until(lambda: not jb.unfinished and not jt.unfinished)
check(all(i.state == engine.DONE for j in (ja, jb, jt) for i in j.run.items), "1: 全部完了")
check(not q.line_busy(), "1: 全部終われば、開始はまたすぐ始まる")

# 2. ★待ちだけが残ったら次を始める / 一時停止しているあいだは次を始めない
gate = recipes.Recipe("G", folder=str(src[0].parent), stages=[recipes.Stage(recipes.GEN, workflow=I2I),
                                                              recipes.Stage(recipes.GATE, stars=3),
                                                              recipes.Stage(recipes.GEN, workflow=I2I)])
plain = recipes.Recipe("P", folder=str(src[0].parent), stages=[recipes.Stage(recipes.GEN, workflow=I2I)])
jg = q.add(engine.Run.create(gate.copy(), src[:2]), gate)
jp = q.add(engine.Run.create(plain.copy(), src[:2]), plain)
check(jp.waiting, "2: ★を待つ量産のうしろで待つ")
ok = pump_until(lambda: not jp.waiting)
check(ok and jg.engine.gated() == 2, f"2: ★待ちだけになったら次を始める (★待ち {jg.engine.gated()})")
pump_until(lambda: not jp.unfinished)
check(jg.unfinished and not jg.sendable(), "2: ★待ちの量産は終わらずに残る")
jg.engine.end_gates()
pump_until(lambda: not jg.unfinished)
check(jg.finished, "2: ★待ちを終えれば終わる")

jx = q.add(engine.Run.create(plain.copy(), src), plain)
jx.engine.set_paused(True)
jy = q.add(engine.Run.create(plain.copy(), src[:1]), plain)
pump_until(lambda: False, 1.5)
check(jy.waiting, "2: 一時停止しているあいだは、次を始めない")

# 3. 順番を変える・外す
jz = q.add(engine.Run.create(gate.copy(), src[:1]), gate)
check([j.name for j in q.waiting_jobs()] == ["P", "G"], "3: 入れた順に待つ")
check(q.move(jz, -1) and [j.name for j in q.waiting_jobs()] == ["G", "P"], "3: 先にできる")
check(q.place(jy) == 2, "3: 順番が変わる")

# 4. 閉じて開き直す: 書いたものを読み、聞くまでは動かさない
q.stop_threads()
pump_until(lambda: not jx.engine.active, 10)
q2 = line_queue.LineQueue()
q2.set_machines([comfy_a.url])
q2.load()
names = [(j.name, j.waiting) for j in q2.jobs]
check(q2.held, "4: 終わっていないものがあれば、聞くまで止めておく")
check([j.name for j in q2.waiting_jobs()] == ["G", "P"], f"4: 待ち行列の順番を戻す ({names})")
check(len(q2.running_jobs()) == 1 and q2.running_jobs()[0].run.paused, "4: 一時停止していた量産を戻す")
check(len(q2.finished_jobs()) == len(q.finished_jobs()), "4: 終わったものも一覧に残す")
pump_until(lambda: False, 0.5)
check(all(j.waiting for j in q2.waiting_jobs()) and not q2.active(), "4: 聞く前は何も始めない")
q2.resume()
moving = q2.running_jobs()[0]
moving.engine.set_paused(False)
ok = pump_until(lambda: not q2.waiting_jobs() and not any(j.sendable() for j in q2.jobs), 90)
check(ok, "4: 続きから始めると、待ち行列も順に動く (★待ちの G は★待ちのまま残る)")
for j in q2.running_jobs():
    j.engine.end_gates()
pump_until(lambda: not q2.running_jobs(), 30)

# 5. 中断する: 待っているものを外し、動いていたものは取り消す
q2.add(engine.Run.create(plain.copy(), src), plain).engine.set_paused(True)
q2.add(engine.Run.create(plain.copy(), src[:1]), plain)
q2.stop_threads()
q3 = line_queue.LineQueue()
q3.load()
check(q3.held and len(q3.waiting_jobs()) == 1, "5: 読み直す")
q3.discard_unfinished()
check(not q3.waiting_jobs() and not q3.running_jobs() and not q3.held, "5: 中断すると、待ち行列は空・残りは取り消し")
check(not any(j.waiting for j in line_queue.LineQueue().jobs), "5: ファイルからも消える")

# 6. 前の作り (line_run.json) を待ち行列の 1 本にする
old = engine.Run.create(plain.copy(), src[:1])
store.write_text(store.app_file(engine.RUN_FILE), json.dumps(old.to_dict(), ensure_ascii=False))
q4 = line_queue.LineQueue()
q4.set_machines([comfy_a.url])
q4.load()
check(not store.app_file(engine.RUN_FILE).exists(), "6: line_run.json を移した")
check(any(j.unfinished and j.name == "P" and len(j.run.items) == 1 for j in q4.jobs) and q4.held,
      "6: 前の量産が終わっていなければ、続きから始めるかを聞く")

# 7. 終わったものは KEEP_FINISHED 本まで
q4.discard_unfinished()
for _n in range(line_queue.KEEP_FINISHED + 3):
    j = q4.add(engine.Run.create(plain.copy(), src[:1]), plain)
    pump_until(lambda: not j.unfinished and not j.waiting, 30)
pump_until(lambda: len(q4.finished_jobs()) <= line_queue.KEEP_FINISHED, 3)
check(len(q4.finished_jobs()) == line_queue.KEEP_FINISHED, f"7: 終わったものは {line_queue.KEEP_FINISHED} 本まで"
      f" ({len(q4.finished_jobs())})")
q4.stop_threads()

# 8. 画面: 量産中も「開始」を押せて、待ち行列に入る。量産の札で進み具合を見て、レシピの札でレシピに戻る
from prismlab.main_window import MainWindow  # noqa: E402

for f in (store.APP_DIR / line_queue.LINE_DIR).glob("*.json"):
    f.unlink()
recipes.save([recipes.Recipe("W", folder=str(src[0].parent), stages=[recipes.Stage(recipes.GEN, workflow=I2I)])])
recipes.set_machines([comfy_a.url], [])
recipes.set_last_index(0)
slow = FakeComfy(polls=40)
recipes.set_machines([slow.url], [])
window = MainWindow()
window.resize(1500, 900)
window.show()
window.offer_resume()
lv = window.line
pump_until(lambda: False, 0.5)
check(not lv.jobs_host.isVisible() and lv.start_button.text() == "▶ 開始", "8: 何もなければ量産の一覧は出さず、「▶ 開始」")
lv._start()
pump_until(lambda: False, 0.6)
first = lv.job
check(first is not None and lv.locked() and lv.jobs_host.isVisible(), "8: 始めたら、その量産の進み具合を見せる")
check(lv.start_button.isEnabled() and lv.start_button.text() == "＋ 待ち行列に入れる",
      f"8: 量産中も押せて、「＋ 待ち行列に入れる」 ({lv.start_button.text()})")
lv._select_recipe(0)
check(not lv.locked(), "8: レシピの札を押すとレシピに戻る (直せる)")
lv.recipe.stages.append(recipes.Stage(recipes.TEXT, text="second"))
lv._save_recipes()
lv._start()
pump_until(lambda: False, 0.6)
waiting = window.queue.waiting_jobs()
check(len(waiting) == 1 and lv.job is first and not lv.locked(), "8: 2 回目は待ち行列に入り、見ているものは変えない")
check(len(waiting[0].run.stages) == 2 and len(first.run.stages) == 1, "8: 待っているものは直したあとの写し")
lv.trial_button.click()
pump_until(lambda: False, 0.6)
check(lv.job is not None and lv.job.trial and lv.job.engine.active and not lv.locked(),
      "8: 1 枚で試すは量産中でもすぐ動き、レシピは見せたまま")
chips = [lv.jobs_row.itemAt(i).widget().text() for i in range(lv.jobs_row.count()) if lv.jobs_row.itemAt(i).widget()]
check(len(chips) == 3 and chips[0].startswith("▶ W") and any(c.startswith("⏳1 W") for c in chips),
      f"8: 量産の札 ({chips})")
if len(sys.argv) > 1:
    window.grab().save(str(Path(sys.argv[1]) / "prismlab_queue.png"))
lv._view_job(waiting[0])
check(lv.locked() and "待ち行列の 1 本目" in lv.summary.text() and lv.cancel_button.text() == "待ち行列から外す",
      f"8: 待っているものを見る ({lv.summary.text()})")
window.queue.discard_unfinished()
window.queue.stop_threads()

print()
print("失敗:", failures if failures else "なし")
sys.exit(1 if failures else 0)
