"""生成のステップの「値を変える」(2026-11-25) のテスト。偽の ComfyUI を使い、APPDATA を一時フォルダに差し替える。

    python tests\\test_values.py
"""

import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="prismlab_values_"))
os.environ["APPDATA"] = str(TMP / "appdata")
os.environ["QT_QPA_PLATFORM"] = os.environ.get("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(ROOT.parent / "GenCommon"))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from fakes import FakeComfy  # noqa: E402
from gencommon import comfy, store  # noqa: E402
from prismlab import dialogs, engine, profile, recipes, theme  # noqa: E402
from prismlab.value_dialog import ValueDialog, ValuesBox, is_common  # noqa: E402

app = QApplication(sys.argv)
theme.apply(app)
profile.setup()
assert str(store.APP_DIR).startswith(str(TMP))
QMessageBox.warning = staticmethod(lambda *a, **k: None)

ok = []


def check(cond, label):
    assert cond, label
    ok.append(label)
    print("OK  ", label)


def workflow() -> dict:
    """T2I: モデル・LoRA・解像度 (megapixels)・サンプラー。"""
    return {
        "2": {"class_type": "CLIPTextEncode", "inputs": {"text": "{prompt}", "clip": ["5", 0]}},
        "4": {"class_type": "KSampler", "_meta": {"title": "KSampler"},
              "inputs": {"seed": 1, "steps": 8, "cfg": 1.0, "sampler_name": "er_sde", "positive": ["2", 0],
                         "model": ["30:15", 0], "latent_image": ["6", 0]}},
        "5": {"class_type": "CLIPLoader", "inputs": {"clip_name": "c"}},
        "6": {"class_type": "EmptyLatentImage", "inputs": {"width": ["49", 0], "height": ["49", 1], "batch_size": 1}},
        "30:10": {"class_type": "UNETLoader", "_meta": {"title": "拡散モデルを読み込む"},
                  "inputs": {"unet_name": "krea2\\moody.safetensors", "weight_dtype": "default"}},
        "30:15": {"class_type": "LoraLoaderModelOnly", "_meta": {"title": "LoRA"},
                  "inputs": {"lora_name": "vgn\\VGNVV.safetensors", "strength_model": 0.64, "model": ["30:10", 0]}},
        "30:23": {"class_type": "PrimitiveBoolean", "_meta": {"title": "Boolean (Enable LoRA?)"},
                  "inputs": {"value": False}},
        "49": {"class_type": "ResolutionSelector", "_meta": {"title": "解像度セレクター"},
               "inputs": {"aspect_ratio": "1:1 (Square)", "megapixels": 2, "multiple": 8, "flag": ["30:23", 0]}},
        "9": {"class_type": "SaveImage", "inputs": {"images": ["4", 0], "filename_prefix": "x"}},
    }


fake = FakeComfy(polls=1)
WF = TMP / "wf"
WF.mkdir()
PATH = WF / "t2i.json"
PATH.write_text(json.dumps(workflow()), encoding="utf-8")

# 1. 候補: 値が直接入っている入力だけ。アプリが入れるもの・線・使われないノードは出さない
slots = comfy.value_slots(workflow())
keys = {(s["node"], s["input"]) for s in slots}
check(("30:10", "unet_name") in keys and ("30:15", "lora_name") in keys and ("49", "megapixels") in keys,
      "モデル・LoRA・megapixels は候補に出る")
check(("4", "seed") not in keys and ("2", "text") not in keys, "シード・プロンプトは出さない")
check(("6", "width") not in keys and ("4", "positive") not in keys, "線でつながった入力は出さない")
check([s["node"] for s in slots].index("30:10") < [s["node"] for s in slots].index("49"),
      "番号の順 (30:10 は 49 より前)")
common = {(s["node"], s["input"]) for s in slots if is_common(s)}
check({("30:10", "unet_name"), ("30:15", "lora_name"), ("30:15", "strength_model"), ("49", "megapixels"),
       ("4", "sampler_name"), ("4", "steps"), ("30:23", "value")} <= common, f"よく変えるもの ({sorted(common)})")
check(("30:10", "weight_dtype") not in common and ("9", "filename_prefix") not in common, "よく変えないものは隠す")

# 2. 入れる・場所がないとき
values = [{"node": "30:10", "input": "unet_name", "value": "krea2c\\turbo.safetensors", "class_type": "UNETLoader"},
          {"node": "49", "input": "megapixels", "value": 1, "class_type": "ResolutionSelector"},
          {"node": "30:23", "input": "value", "value": True, "class_type": "PrimitiveBoolean"}]
wf = workflow()
comfy.apply_values(wf, values)
check(wf["30:10"]["inputs"]["unet_name"] == "krea2c\\turbo.safetensors" and wf["49"]["inputs"]["megapixels"] == 1
      and wf["30:23"]["inputs"]["value"] is True, "値を入れる")
check(comfy.value_problems(workflow(), values) == [], "場所があれば問題なし")
check(comfy.value_problems(workflow(), [{"node": "77", "input": "x", "value": 1}]) == ["ノード 77 の x"],
      "ないノードは問題")
check(comfy.value_problems(workflow(), [{"node": "6", "input": "width", "value": 1}]), "線の入力には入れない")
check(comfy.value_problems(workflow(), [{"node": "30:10", "input": "unet_name", "value": "a",
                                         "class_type": "CheckpointLoaderSimple", "title": "別"}]),
      "番号が同じでも、種類が違えば問題 (作り直したワークフロー)")
try:
    comfy.apply_values(workflow(), [{"node": "77", "input": "x", "value": 1}])
    check(False, "場所がなければ止める")
except comfy.ComfyError as exc:
    check("値を変える場所" in str(exc), "場所がなければ止める (違う値で生成しない)")

# 3. ComfyUI から選べるものを聞く (前の形・新しい形)
fake.object_info = {
    "UNETLoader": {"input": {"required": {"unet_name": [["a.safetensors", "b.safetensors"]],
                                          "weight_dtype": [["default", "fp8"]]}}},
    "KSampler": {"input": {"required": {"sampler_name": ["COMBO", {"options": ["euler", "er_sde"]}],
                                        "steps": ["INT", {"default": 20}]}}},
}
check(comfy.input_choices(fake.url, "UNETLoader", "unet_name") == ["a.safetensors", "b.safetensors"], "選べるもの (前の形)")
check(comfy.input_choices(fake.url, "KSampler", "sampler_name") == ["euler", "er_sde"], "選べるもの (新しい形)")
check(comfy.input_choices(fake.url, "KSampler", "steps") is None, "数は選ぶ形ではない")
check(comfy.input_choices(fake.url, "Nope", "x") is None, "知らないノード")

# 4. レシピに覚える
st = recipes.Stage(recipes.GEN, workflow=str(PATH), values=[dict(v) for v in values])
back = recipes.Stage.from_dict(json.loads(json.dumps(st.to_dict())))
check(back.values == values, "レシピに覚えて読み直せる")
check(recipes.clean_values([{"node": "1", "input": "a", "value": 1}, {"node": "1", "input": "a", "value": 2},
                            {"node": "", "input": "a", "value": 1}, {"node": "2", "input": "b"},
                            {"node": "3", "input": "c", "value": [1]}]) == [{"node": "1", "input": "a", "value": 2}],
      "読めないもの・重なるものは捨てる")
d = st.to_dict()
d["values"][0]["value"] = "changed"
check(st.values[0]["value"] == "krea2c\\turbo.safetensors", "写しを直しても元は変わらない")

# 5. 量産: 送ったワークフローに入っている。ファイルはそのまま
txt = TMP / "texts"
txt.mkdir()
(txt / "a.txt").write_text("prompt a", encoding="utf-8")
r = recipes.Recipe("v", folder=str(txt), source=recipes.TEXTS, stages=[st])
eng = engine.Engine(run_file=None)
eng.set_machines([fake.url])
n0 = len(fake.received)
eng.start(engine.Run.create(r, [txt / "a.txt"]))
end = time.time() + 30
while time.time() < end and not all(i.finished for i in eng.run.items):
    app.processEvents()
    time.sleep(0.05)
check(all(i.state == engine.DONE for i in eng.run.items), f"作れる ({[i.error for i in eng.run.items]})")
sent = fake.received[n0]
check(sent["30:10"]["inputs"]["unet_name"] == "krea2c\\turbo.safetensors" and sent["49"]["inputs"]["megapixels"] == 1,
      "送ったワークフローの値が変わっている")
check(json.loads(PATH.read_text(encoding="utf-8"))["30:10"]["inputs"]["unet_name"] == "krea2\\moody.safetensors",
      "ワークフローのファイルは書き換えない")
check(eng.run.stages[0].get("values") == values, "始めたときの写しに入る")
hist = [json.loads(line) for line in store.app_file("comfy_history.txt").read_text(encoding="utf-8").splitlines() if line]
check(hist and hist[-1].get("values") == values, "生成の記録に残る")
check(comfy._to_record(hist[-1]).request.values == values, "記録から読める")

# 6. 場所がない値があれば始めない
bad = recipes.Stage(recipes.GEN, workflow=str(PATH), values=[{"node": "77", "input": "x", "value": 1}])
try:
    engine.stage_snapshot(bad)
    check(False, "始めない")
except ValueError as exc:
    check("値を変える場所" in str(exc), "場所がない値があれば始めない")

# 7. 画面: ステップの設定に一覧が出て、OK で覚える。場所がなければ OK できない
dlg = dialogs.StageDialog(recipes.Stage(recipes.GEN, workflow=str(PATH), values=[dict(values[0])]))
box = dlg.values_box
check(box.tree.topLevelItemCount() == 1 and box.problems() == [], "一覧に出る")
check(box.tree.topLevelItem(0).text(1) == "krea2\\moody.safetensors", "ワークフローの値も出す")
picker = ValueDialog(box._wf, taken=[("30:10", "unet_name")])
listed = [picker.tree.topLevelItem(i).data(0, 256)["input"] for i in range(picker.tree.topLevelItemCount())]
check("unet_name" not in listed or all(picker.tree.topLevelItem(i).data(0, 256)["node"] != "30:10"
                                       for i in range(picker.tree.topLevelItemCount())), "変えている場所は出さない")
check("weight_dtype" not in listed, "最初はよく変えるものだけ")
picker.search.setText("lora_name")
check(picker.tree.topLevelItemCount() == 1, "探す")
picker.text.setEditText("vgn\\VGNAY.safetensors")
picker._accept()
check(picker.result_value["value"] == "vgn\\VGNAY.safetensors" and picker.result_value["node"] == "30:15", "選んで決める")
picker2 = ValueDialog(box._wf)
picker2.search.setText("megapixels")
check(picker2.stack.currentWidget() is picker2.integer, "数 (整数) は数の欄")
picker2.integer.setValue(1)
picker2._accept()
check(picker2.result_value["value"] == 1, "数を決める")
box.values.append(picker2.result_value)
dlg._accept()
check([v["input"] for v in dlg.stage.values] == ["unet_name", "megapixels"], "OK で覚える")
dlg2 = dialogs.StageDialog(recipes.Stage(recipes.GEN, workflow=str(PATH), values=[{"node": "77", "input": "x",
                                                                                  "value": 1}]))
check(dlg2.values_box.problems() == ["ノード 77 の x"], "場所がない値を出す")
dlg2._accept()
check(dlg2.result() == 0 and dlg2.stage.values[0]["node"] == "77", "場所がない値があれば OK できない")
box3 = ValuesBox([])
box3.set_workflow(None)
check(not box3.add_button.isEnabled(), "ワークフローが読めなければ足せない")

# 8. 一覧を読む: 繋がらない台は飛ばし、最初に答えた台の一覧を使う
from prismlab import value_dialog  # noqa: E402

dead = "http://127.0.0.1:9"
recipes.set_machines([dead, fake.url])
urls = [u for _n, u in value_dialog.candidate_urls()]
check(urls[:2] == [dead, fake.url], f"量産ラインの台から順に聞く ({urls})")
picker3 = ValueDialog(comfy.load_workflow(str(PATH)))
picker3.text.showPopup = lambda: None
picker3.search.setText("unet_name")
picker3._load_choices()
items = [picker3.text.itemText(i) for i in range(picker3.text.count())]
check(items == ["a.safetensors", "b.safetensors"], f"繋がる台の一覧を使う ({items})")
check("127.0.0.1:9" in picker3.choice_note.text() and "飛ばしました" in picker3.choice_note.text(),
      f"飛ばした台を出す ({picker3.choice_note.text()})")
recipes.set_machines([dead])
value_dialog.candidate_urls = lambda: [(dead, dead)]
picker3._load_choices()
check("どの台の ComfyUI にも繋がりません" in picker3.choice_note.text(), "どの台も繋がらなければそう出す")

fake.close()
print(f"\nall OK ({len(ok)})")
