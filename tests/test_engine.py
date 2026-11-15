"""量産ラインの中心部分 (engine) のテスト。偽の ComfyUI・LLM を使い、APPDATA を一時フォルダに差し替える。

    python tests\\test_engine.py
"""

import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="prismlab_test_"))
os.environ["APPDATA"] = str(TMP / "appdata")  # gencommon を読み込む前に差し替える (実際の設定に触れない)
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, str(ROOT.parent / "GenCommon"))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtGui import QColor, QImage  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from fakes import FakeComfy, FakeLLM, workflow_cn, workflow_i2v  # noqa: E402
from gencommon import enhancer, reader, settings, store  # noqa: E402
from prismlab import engine, profile, recipes  # noqa: E402

app = QApplication(sys.argv)
profile.setup()
assert str(store.APP_DIR).startswith(str(TMP)), store.APP_DIR

llm = FakeLLM()
settings.set_llm_url(llm.url)
READER = next(p.name for p in reader.load_presets() if not p.is_empty)
ENHANCER = next(p.name for p in enhancer.load_presets() if not p.is_empty)

WF = TMP / "wf"
WF.mkdir()
(WF / "i2v.json").write_text(json.dumps(workflow_i2v()), encoding="utf-8")
(WF / "cn.json").write_text(json.dumps(workflow_cn()), encoding="utf-8")


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


def wait(eng: engine.Engine, seconds: float = 60) -> None:
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        run = eng.run
        if run is not None and all(i.finished for i in run.items) and not eng.active:
            return
        time.sleep(0.05)
    raise AssertionError("時間内に終わりませんでした: " + str([(i.source, i.state, i.stage, i.status)
                                                          for i in eng.run.items]))


def recipe(name, folder, stages) -> recipes.Recipe:
    return recipes.Recipe(name, folder=str(folder), stages=stages)


failures = []


def check(cond, message):
    print(("OK   " if cond else "NG   ") + message)
    if not cond:
        failures.append(message)


# 1. ★から動画: 読み取り → 盛る → I2V を 2 台で
a, b = FakeComfy(polls=2), FakeComfy(polls=2)
src = make_images(TMP / "in1", 6)
r = recipe("★から動画", TMP / "in1", [recipes.Stage(recipes.READ, reader=READER),
                                    recipes.Stage(recipes.ENHANCE, enhancer=ENHANCER),
                                    recipes.Stage(recipes.GEN, workflow=str(WF / "i2v.json"), seconds=3)])
eng = engine.Engine()
eng.set_machines([a.url, b.url])
eng.start(engine.Run.create(r, src))
wait(eng)
items = eng.run.items
check(all(i.state == engine.DONE for i in items), "1: すべて完了")
outs = [Path(i.outputs[-1]["path"]) for i in items]
check(all(p.name.endswith("_video.mp4") and p.parent == TMP / "in1" / "★から動画" for p in outs),
      "1: 出力は (入口)\\(レシピ名)\\(元の名前)_video.mp4")
check(all((TMP / "in1" / f"img{n}.txt").is_file() for n in range(1, 7)), "1: 読み取った説明を .txt に書いた")
check(len(a.received) > 0 and len(b.received) > 0, f"1: 2 台に振り分けた ({len(a.received)} / {len(b.received)})")
sent = [wf["2"]["inputs"]["text"] for wf in a.received + b.received]
check(all(t.startswith("a photo") and "enhanced" in t for t in sent), "1: 盛ったプロンプトを送った")
check(all(wf["3"]["inputs"]["value"] == 3 for wf in a.received + b.received), "1: 長さを入れた")
hist = (store.APP_DIR / "comfy_history.txt").read_text(encoding="utf-8").strip().splitlines()
check(len(hist) == 6, "1: 生成の記録を 6 件残した")
saved = engine.load_run()
check(saved is not None and saved.unfinished == 0, "1: line_run.json に終わった状態を書いた")

# 2. 読み取り済みの .txt を使う (読み直さない)
calls = len(llm.calls)
r2 = recipe("2", TMP / "in1", [recipes.Stage(recipes.READ, reader=READER),
                               recipes.Stage(recipes.GEN, workflow=str(WF / "i2v.json"))])
eng.start(engine.Run.create(r2, src[:2]))
wait(eng)
check(len(llm.calls) == calls, "2: .txt があれば読み取らない")

# 3. 台が落ちている: 繋がらない台を外して、繋がる台だけで進める
dead = FakeComfy()
dead_url = dead.url
dead.close()
src3 = make_images(TMP / "in3", 4)
r3 = recipe("実写クローン", TMP / "in3", [recipes.Stage(recipes.GEN, workflow=str(WF / "cn.json"))])
eng3 = engine.Engine()
eng3.set_machines([dead_url, a.url])
eng3.start(engine.Run.create(r3, src3))
wait(eng3)
check(all(i.state == engine.DONE for i in eng3.run.items), "3: 繋がらない台があっても、すべて完了")
check(eng3.machines[0].state == engine.DOWN, "3: 繋がらない台は「繋がりません」")
names = sorted(Path(i.outputs[-1]["path"]).name for i in eng3.run.items)
check(names == [f"img{n}_cn.png" for n in range(1, 5)], f"3: I2I CN の印は _cn ({names[0]})")

# 4. つないだ段: I2I CN → 読み取り → I2V
src4 = make_images(TMP / "in4", 2)
r4 = recipe("クローン → 動画", TMP / "in4", [recipes.Stage(recipes.GEN, workflow=str(WF / "cn.json")),
                                         recipes.Stage(recipes.READ, reader=READER),
                                         recipes.Stage(recipes.GEN, workflow=str(WF / "i2v.json"))])
eng.start(engine.Run.create(r4, src4))
wait(eng)
last = sorted(Path(i.outputs[-1]["path"]).name for i in eng.run.items)
check(last == ["img1_cn_video.mp4", "img2_cn_video.mp4"], f"4: つないだ段の名前 ({last})")
check(all(len(i.outputs) == 2 for i in eng.run.items), "4: 段ごとの出力を残した")

# 5. 失敗とやり直し
a.fail = True
src5 = make_images(TMP / "in5", 2)
r5 = recipe("5", TMP / "in5", [recipes.Stage(recipes.GEN, workflow=str(WF / "cn.json"))])
eng5 = engine.Engine()
eng5.set_machines([a.url])
eng5.start(engine.Run.create(r5, src5))
wait(eng5)
check(all(i.state == engine.FAILED and "out of memory" in i.error for i in eng5.run.items),
      "5: ComfyUI のエラーは失敗 (理由つき)")
check(eng5.machines[0].state == engine.UP, "5: 台が 1 つだけなら、失敗が続いても外さない")
a.fail = False
check(eng5.retry_failed() == 2, "5: 失敗したものをやり直す (2 件)")
wait(eng5)
check(all(i.state == engine.DONE for i in eng5.run.items), "5: やり直して完了")

# 6. 続きから始める: 頼み済み (prompt_id あり) のものは、その台で結果を受け取る
slow = FakeComfy(polls=3)
src6 = make_images(TMP / "in6", 1)
r6 = recipe("6", TMP / "in6", [recipes.Stage(recipes.GEN, workflow=str(WF / "cn.json"))])
run6 = engine.Run.create(r6, src6)
eng6 = engine.Engine()
eng6.set_machines([slow.url])
eng6.start(run6)
end = time.time() + 20
while not run6.items[0].prompt_id and time.time() < end:
    time.sleep(0.05)
pid = run6.items[0].prompt_id
eng6.stop_threads()  # 「残して閉じる」のかわり
time.sleep(1.5)
data = json.loads((store.APP_DIR / engine.RUN_FILE).read_text(encoding="utf-8"))
check(data["items"][0]["prompt_id"] == pid and data["items"][0]["machine"] == slow.url,
      "6: 頼み済みの台と prompt_id を書いた")
resumed = engine.load_run()
eng6b = engine.Engine()
eng6b.set_machines([slow.url])
eng6b.start(resumed)
wait(eng6b)
check(resumed.items[0].state == engine.DONE, "6: 続きから始めて完了")
check(len(slow.prompts) == 1, f"6: 頼み直さずに受け取った (頼んだ数 {len(slow.prompts)})")

# 7. 中止
slow.polls = 50
src7 = make_images(TMP / "in7", 5)
r7 = recipe("7", TMP / "in7", [recipes.Stage(recipes.GEN, workflow=str(WF / "cn.json"))])
eng7 = engine.Engine()
eng7.set_machines([slow.url])
eng7.start(engine.Run.create(r7, src7))
time.sleep(3)
eng7.cancel_all()
wait(eng7, 30)
check(all(i.state == engine.CANCELED for i in eng7.run.items), "7: 中止ですべて取り消し")

# 8. 一時停止: 新しく頼まない
slow.polls = 1
src8 = make_images(TMP / "in8", 3)
r8 = recipe("8", TMP / "in8", [recipes.Stage(recipes.GEN, workflow=str(WF / "cn.json"))])
eng8 = engine.Engine()
eng8.set_machines([slow.url])
run8 = engine.Run.create(r8, src8)
run8.paused = True
before = len(slow.prompts)
eng8.start(run8)
time.sleep(2.5)
check(len(slow.prompts) == before, "8: 一時停止中は頼まない")
eng8.set_paused(False)
wait(eng8)
check(all(i.state == engine.DONE for i in run8.items), "8: 再開して完了")

# 9. 入口と★
check([p.name for p in recipes.list_images(str(TMP / "in1"))][:3] == ["img1.png", "img2.png", "img3.png"],
      "9: 入口は画像だけを名前順で")
from prismlab import rating  # noqa: E402
check([rating.stars(v) for v in (0, 1, 25, 50, 75, 99)] == [0, 1, 2, 3, 4, 5], "9: ★の区切り (J-Rate と同じ)")

for s in (a, b, slow):
    s.close()
llm.close()
shutil.rmtree(TMP, ignore_errors=True)
print()
print("失敗: " + ", ".join(failures) if failures else "すべて OK")
sys.exit(1 if failures else 0)
