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

from fakes import (FakeComfy, FakeLLM, workflow_cn, workflow_flf, workflow_i2v, workflow_i2v_ref,  # noqa: E402
                   workflow_t2i)
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
(WF / "t2i.json").write_text(json.dumps(workflow_t2i()), encoding="utf-8")
(WF / "i2v_ref.json").write_text(json.dumps(workflow_i2v_ref()), encoding="utf-8")


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

# 4. つないだステップ: I2I CN → 読み取り → I2V
src4 = make_images(TMP / "in4", 2)
r4 = recipe("クローン → 動画", TMP / "in4", [recipes.Stage(recipes.GEN, workflow=str(WF / "cn.json")),
                                         recipes.Stage(recipes.READ, reader=READER),
                                         recipes.Stage(recipes.GEN, workflow=str(WF / "i2v.json"))])
eng.start(engine.Run.create(r4, src4))
wait(eng)
last = sorted(Path(i.outputs[-1]["path"]).name for i in eng.run.items)
check(last == ["img1_cn_video.mp4", "img2_cn_video.mp4"], f"4: つないだステップの名前 ({last})")
check(all(len(i.outputs) == 2 for i in eng.run.items), "4: ステップごとの出力を残した")

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

# 10. バイパス
S = recipes.Stage
cn, rd, en, i2v = (S(recipes.GEN, workflow=str(WF / "cn.json")), S(recipes.READ, reader=READER),
                   S(recipes.ENHANCE, enhancer=ENHANCER), S(recipes.GEN, workflow=str(WF / "i2v.json")))
line = [cn, rd, en, i2v]
check([recipes.can_bypass(line, i) for i in range(4)] == [True, False, True, True],
      "10: 入れられるステップ (読み取りは、あとの盛るが困るので不可)")
line10 = [S(recipes.READ, reader=READER), S(recipes.GEN, workflow=str(WF / "i2v.json"))]
check(not recipes.can_bypass(line10, 1) and "生成" in recipes.bypass_reason(line10, 1),
      "10: ただ 1 つの生成はバイパスにできない")
en.bypass = True
check(recipes.can_bypass(line, 1), "10: 盛るをバイパスにすれば、読み取りもバイパスにできる")
src10 = make_images(TMP / "in10", 2)
cn10 = S(recipes.GEN, workflow=str(WF / "cn.json"), bypass=True)
en10 = S(recipes.ENHANCE, enhancer="(消えたエンハンサー)", bypass=True)  # バイパスなら中身は確かめない
r10 = recipe("10", TMP / "in10", [cn10, S(recipes.READ, reader=READER), en10,
                                   S(recipes.GEN, workflow=str(WF / "i2v.json"))])
run10 = engine.Run.create(r10, src10)
check(all(i.stage == 1 for i in run10.items), "10: 先頭のバイパスは飛ばして始める")
eng.start(run10)
wait(eng)
names10 = sorted(Path(i.outputs[-1]["path"]).name for i in eng.run.items)
check(all(i.state == engine.DONE for i in eng.run.items) and names10 == ["img1_video.mp4", "img2_video.mp4"],
      f"10: バイパスした生成・盛るを飛ばして完了 ({names10})")
check(all(i.enhanced is None and len(i.outputs) == 1 for i in eng.run.items), "10: 飛ばしたステップは何も作らない")
try:
    engine.Run.create(recipe("10b", TMP / "in10", [cn10]), src10)
    check(False, "10: すべてバイパスなら始めない")
except ValueError:
    check(True, "10: すべてバイパスなら始めない")

# 11. 1 枚で試す (試作)
src11 = make_images(TMP / "in11", 3)
r11 = recipe("11", TMP / "in11", [S(recipes.READ, reader=READER), S(recipes.ENHANCE, enhancer=ENHANCER),
                                   S(recipes.GEN, workflow=str(WF / "i2v.json"))])
run11 = engine.Run.create(r11, [src11[1]], trial=True)
check(run11.trial and Path(run11.output_dir) == TMP / "in11" / "11" / engine.TRIAL_DIR, "11: 試作の出力は 試作 のフォルダ")
eng.start(run11)
wait(eng)
it11 = eng.run.items[0]
o11 = it11.outputs[-1] if it11.outputs else {}
check(it11.state == engine.DONE and Path(o11.get("path", "")).parent == TMP / "in11" / "11" / engine.TRIAL_DIR,
      "11: 1 枚だけ通して、試作のフォルダに出来た")
check(bool(o11.get("prompt")) and bool(o11.get("enhanced")) and Path(o11.get("from", "")).name == "img2.png",
      "11: 出来たものに、元・読み取った文・盛った文を残した")
check(engine.Run.from_dict(run11.to_dict()).trial, "11: 試作の印を保存する")

# 12. 追加テキスト (盛る前) と 差し込み (盛ったあと)
from gencommon import inserts  # noqa: E402
lines = TMP / "lines"
lines.mkdir()
for n, word in ((1, "hello"), (2, "goodbye"), (10, "again")):
    (lines / f"{n}.txt").write_text(f"says {word}", encoding="utf-8")
src12 = make_images(TMP / "in12", 4)
r12 = recipe("12", TMP / "in12", [S(recipes.READ, reader=READER), S(recipes.TEXT, text="slow pan"),
                                   S(recipes.ENHANCE, enhancer=ENHANCER), S(recipes.INSERT, folder=str(lines)),
                                   S(recipes.GEN, workflow=str(WF / "i2v.json"))])
check(recipes.can_bypass(r12.stages, 0), "12: 追加テキストがあれば、読み取りをバイパスにしても盛れる")
trial12 = engine.Run.create(r12, src12[:1], trial=True)
check(trial12.items[0].plan["3"]["file"] == "1.txt" and not trial12.pickers, "12: 試作は 1 つ目を使い、位置を進めない")
run12 = engine.Run.create(r12, src12)
check([i.plan["3"]["file"] for i in run12.items] == ["1.txt", "2.txt", "10.txt", "1.txt"],
      "12: 差し込みは 1 枚ごとに名前順 (自然順) で、一周したら最初へ")
calls_before = len(llm.calls)
eng.start(run12)
check(inserts.last_used(str(lines)) == "1.txt", "12: 始めたときに差し込みの位置を書く (共有)")
wait(eng)
o12 = eng.run.items[1].outputs[-1] if eng.run.items[1].outputs else {}
check(all(i.state == engine.DONE for i in eng.run.items), "12: 追加テキスト・差し込みを通して完了")
check(any("slow pan" in json.dumps(c, ensure_ascii=False) for c in llm.calls[calls_before:]),
      "12: 盛る前の追加テキストはエンハンサーに渡す")
sent = o12.get("sent", "")
check("says goodbye" in sent and sent.index(o12.get("enhanced", "?")) < sent.index("says goodbye"),
      f"12: 盛ったあとの差し込みは、盛った文のあとにそのまま付ける ({sent!r})")
check([(a["name"], a["before"]) for a in o12.get("adds", [])] == [("追加テキスト", True), ("差し込み", False)],
      "12: 足した文を記録に残す (盛る前か)")
check(eng.run.items[1].adds == [], "12: 生成を通ったら足した文は空に戻す")

# 13. 入口を 1 枚に (回数ぶん同じ画像を通す)
src13 = make_images(TMP / "in13", 1)[0]
r13 = recipe("13", "", [S(recipes.INSERT, folder=str(lines)), S(recipes.GEN, workflow=str(WF / "i2v.json"))])
r13.source, r13.image, r13.repeat = recipes.SINGLE, str(src13), 3
check(Path(r13.resolved_output_dir()) == TMP / "in13" / "13", "13: 1 枚のときの出力は 画像のフォルダ\レシピ名")
r13b = recipes.Recipe.from_dict(r13.to_dict())
check(r13b.single and r13b.image == str(src13) and r13b.repeat == 3, "13: 1 枚・回数を保存する")
eng.start(engine.Run.create(r13, [src13] * r13.repeat))
wait(eng)
names13 = sorted(Path(i.outputs[-1]["path"]).name for i in eng.run.items)
files13 = [i.plan["0"]["file"] for i in eng.run.items]
check(all(i.state == engine.DONE for i in eng.run.items) and len(set(names13)) == 3,
      f"13: 同じ画像から 3 本 (名前は重ならない: {names13})")
check(len(set(files13)) == 3, f"13: 1 本ずつ別の差し込み ({files13})")

# 14. テキストの入口 (T2I の量産)
txt = TMP / "txt14"
txt.mkdir()
(txt / "p2.txt").write_text("a red fox in snow", encoding="utf-8")
(txt / "p10.txt").write_text("a lighthouse at dusk", encoding="utf-8")
(txt / "empty.txt").write_text("  ", encoding="utf-8")
check([p.name for p in recipes.list_texts(str(txt))] == ["p2.txt", "p10.txt"], "14: 入口の .txt (自然順、空は除く)")
t2i, i2v = S(recipes.GEN, workflow=str(WF / "t2i.json")), S(recipes.GEN, workflow=str(WF / "i2v.json"))
check(recipes.entry_problem([S(recipes.READ, reader=READER), t2i], True) != "", "14: テキストの入口で、画像の前の読み取りは置けない")
check(recipes.entry_problem([i2v], True) != "" and recipes.entry_problem([i2v], False) == "",
      "14: テキストの入口で、最初の生成が I2V なら始めない")
check(not recipes.can_bypass([t2i, S(recipes.READ, reader=READER), i2v], 0, text_entry=True),
      "14: テキストの入口で T2I をバイパスにすると画像がなくなるので不可")
r14 = recipe("14", txt, [t2i, S(recipes.READ, reader=READER), i2v])
r14.source = recipes.TEXTS
run14 = engine.Run.create(r14, recipes.list_texts(str(txt)))
check([i.text for i in run14.items] == ["a red fox in snow", "a lighthouse at dusk"], "14: ファイルの中身が元の文")
eng.start(run14)
wait(eng)
o14 = eng.run.items[0].outputs
check(all(i.state == engine.DONE for i in eng.run.items) and len(o14) == 2, "14: T2I → 読み取り → I2V で完了")
check([Path(o["path"]).name for o in o14] == ["p2_t2i.png", "p2_t2i_video.mp4"], f"14: 名前は テキストの名前_t2i ({[Path(o['path']).name for o in o14]})")
check("a red fox in snow" in o14[0].get("sent", ""), "14: テキストの中身を T2I に送った")

# 15. 元の★を引き継ぐ (書き込みは差し替えて記録する)
from prismlab import rating as rating_mod  # noqa: E402
written = []
real_get, real_set = rating_mod.get_stars, rating_mod.set_stars
rating_mod.get_stars = lambda p: 4 if Path(p).name == "img1.png" else 0
rating_mod.set_stars = lambda p, n, tries=3: written.append((Path(p).name, n)) or True
src15 = make_images(TMP / "in15", 2)
r15 = recipe("15", TMP / "in15", [S(recipes.GEN, workflow=str(WF / "cn.json")), S(recipes.GEN, workflow=str(WF / "i2v.json"))])
eng.start(engine.Run.create(r15, src15))
wait(eng)
check(sorted(written) == [("img1_cn.png", 4), ("img1_cn_video.mp4", 4)], f"15: ★のある元だけ、出来たファイルすべてに引き継ぐ ({written})")
written.clear()
r15.inherit = False
eng.start(engine.Run.create(r15, src15))
wait(eng)
check(written == [], "15: 引き継がない設定なら書かない")
rating_mod.get_stars, rating_mod.set_stars = real_get, real_set
check(recipes.Recipe.from_dict({"name": "x"}).inherit, "15: 引き継ぎの既定はオン")

# 16. 回数
check([p for p in recipes.repeat_sources(["a", "b"], 3)] == ["a", "b", "a", "b", "a", "b"],
      "16: 回数は 全部を 1 回ずつ → 2 回目 … の順")

# 17. リファレンス画像 (顔): リファレンス用の LoadImage にだけ別の画像を入れる
from prismlab import kinds  # noqa: E402
check(kinds.has_reference(str(WF / "i2v_ref.json")) and not kinds.has_reference(str(WF / "i2v.json")),
      "17: リファレンス用の LoadImage (タイトルに Face Reference) があるワークフローを見分ける")
src17 = make_images(TMP / "in17", 2)
r17 = recipe("17", TMP / "in17", [S(recipes.GEN, workflow=str(WF / "i2v_ref.json"))])
eng17 = engine.Engine()
eng17.set_machines([a.url])
run17 = engine.Run.create(r17, src17)
run17.items[0].reference = str(src17[1])
before = len(a.received)
eng17.start(run17)
wait(eng17)
sent = a.received[before:]
pairs = sorted((w["1"]["inputs"]["image"] != w["6"]["inputs"]["image"]) for w in sent)
check(all(i.state == engine.DONE for i in run17.items) and pairs == [False, True],
      "17: リファレンスを渡した件だけ、リファレンス用の LoadImage に別の画像を送る (渡さなければ元画像)")
check(engine.Item.from_dict(run17.items[0].to_dict()).reference == str(src17[1]), "17: リファレンスを保存する")
eng17.stop_threads()

# 18. ラストフレーム: ラストフレーム用の LoadImage にだけ別の画像を入れる (渡さなければ元画像)
from gencommon import comfy as comfy18  # noqa: E402
(WF / "flf.json").write_text(json.dumps(workflow_flf()), encoding="utf-8")


def titled(title):
    return {"class_type": "LoadImage", "_meta": {"title": title}, "inputs": {"image": "x.png"}}


check(kinds.has_last_frame(str(WF / "flf.json")) and not kinds.has_last_frame(str(WF / "i2v.json"))
      and not kinds.has_last_frame(str(WF / "i2v_ref.json")),
      "18: ラストフレーム用の LoadImage (タイトルに Last Frame) があるワークフローを見分ける")
check(all(comfy18.is_last_frame_node(titled(t)) for t in ("Last Frame", "End Image", "final", "ラストフレーム", "最後のコマ"))
      and not any(comfy18.is_last_frame_node(titled(t)) for t in ("First Frame", "Legend", "Blender", "Start", "")),
      "18: タイトルの見分け (英語は単語として。Legend・Blender などは拾わない)")
check(not comfy18.is_reference_node(titled("End Face")) and not kinds.has_reference(str(WF / "flf.json")),
      "18: ラストフレーム用のものはリファレンスにしない")
src18 = make_images(TMP / "in18", 2)
(TMP / "in18" / "last.png").write_bytes(b"\x89PNG-LAST-FRAME-MARK")
r18 = recipe("18", TMP / "in18", [S(recipes.GEN, workflow=str(WF / "flf.json"))])
eng18 = engine.Engine()
eng18.set_machines([a.url])
run18 = engine.Run.create(r18, src18)
run18.items[0].last_frame = str(TMP / "in18" / "last.png")
before = len(a.received)
eng18.start(run18)
wait(eng18)
sent = a.received[before:]
with_last = [w for w in sent if b"LAST-FRAME-MARK" in a.uploads.get(w["7"]["inputs"]["image"], b"")]
check(all(i.state == engine.DONE for i in run18.items) and len(sent) == 2 and len(with_last) == 1
      and b"LAST-FRAME-MARK" not in a.uploads[with_last[0]["1"]["inputs"]["image"]],
      "18: ラストフレームを渡した件だけ、ラストフレーム用の LoadImage にその画像を送る (始めは元画像)")
plain = [w for w in sent if w is not with_last[0]]
check(plain and plain[0]["1"]["inputs"]["image"] == plain[0]["7"]["inputs"]["image"],
      "18: 渡さなければ、ラストフレーム用の LoadImage にも元画像 (今までどおり)")
check(engine.Item.from_dict(run18.items[0].to_dict()).last_frame == str(TMP / "in18" / "last.png"),
      "18: ラストフレームを保存する")
eng18.stop_threads()

# 19. 覚える・思い出す: その時点のプロンプトをメモに覚え、生成を通ったあとで戻す (2026-11-18)
save_m = S(recipes.SAVE, memo="T2I の文")
load_m = S(recipes.LOAD, memo="T2I の文")
check(("norecall", 0) in recipes.line_problems([load_m, i2v]) and "覚えていません" in recipes.entry_problem([load_m, i2v], False),
      "19: 覚えていないメモを思い出すステップは、理由を出して始めない")
check(("memoempty", 0) in recipes.line_problems([save_m, i2v]), "19: 文がないところで覚えると知らせる")
check(not recipes.can_bypass([S(recipes.ENHANCE, enhancer=ENHANCER), save_m, t2i, load_m, i2v], 1, text_entry=True),
      "19: あとで思い出すメモの「覚える」はバイパスにできない")
eng19 = engine.Engine()
eng19.set_machines([a.url])
# 置き換える: 盛る → 覚える → T2I → 思い出す → I2V で、I2V にも T2I と同じ文を送る
r19 = recipe("19", txt, [S(recipes.ENHANCE, enhancer=ENHANCER), save_m, t2i, load_m, i2v])
r19.source = recipes.TEXTS
check(recipes.line_problems(r19.stages, text_entry=True) == set() and
      recipes.entry_problem(r19.stages, True) == "", "19: 覚えてから思い出す並びは問題なし")
eng19.start(engine.Run.create(r19, recipes.list_texts(str(txt))[:1]))
wait(eng19)
o19 = eng19.run.items[0].outputs
check(eng19.run.items[0].state == engine.DONE and len(o19) == 2, "19: 完了")
check(o19[0]["sent"].startswith("a red fox in snow enhanced") and o19[1]["sent"].startswith("a red fox in snow enhanced"),
      f"19: T2I と I2V に同じ盛った文を送る ({o19[0]['sent']!r} / {o19[1]['sent']!r})")
check(engine.Item.from_dict(eng19.run.items[0].to_dict()).memos.get("T2I の文", {}).get("enhanced", "").startswith(
    "a red fox in snow enhanced"), "19: 覚えたメモも保存する (続きから始めても使える)")
# 後ろに足す: 覚える → T2I → 読み取り → 思い出す (後ろに足す) → I2V で、読み取った説明 + 覚えた文
load_add = S(recipes.LOAD, memo="T2I の文", mode=recipes.APPEND)
r19b = recipe("19b", txt, [save_m, t2i, S(recipes.READ, reader=READER, reread=True), load_add, i2v])
r19b.source = recipes.TEXTS
eng19.start(engine.Run.create(r19b, recipes.list_texts(str(txt))[:1]))
wait(eng19)
sent_b = eng19.run.items[0].outputs[1]["sent"]
check(eng19.run.items[0].state == engine.DONE and sent_b.startswith("a photo") and "a red fox in snow" in sent_b
      and sent_b.index("a photo") < sent_b.index("a red fox"), f"19: 後ろに足す: 読み取った説明のあとに覚えた文 ({sent_b!r})")
eng19.stop_threads()

for s in (a, b, slow):
    s.close()
llm.close()
shutil.rmtree(TMP, ignore_errors=True)
print()
print("失敗: " + ", ".join(failures) if failures else "すべて OK")
sys.exit(1 if failures else 0)
