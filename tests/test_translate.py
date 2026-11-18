"""「英語に」ステップ (2026-11-18) のテスト。偽の ComfyUI と偽の LLM を使い、APPDATA を一時フォルダに差し替える
(実際の設定に触れない)。

    python tests\\test_translate.py
"""

import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="prismlab_translate_"))
os.environ["APPDATA"] = str(TMP / "appdata")
os.environ["QT_QPA_PLATFORM"] = os.environ.get("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(ROOT.parent / "GenCommon"))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtGui import QColor, QImage  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from fakes import FakeComfy, FakeLLM, workflow_cn  # noqa: E402
from gencommon import enhancer, settings, store  # noqa: E402
from prismlab import engine, profile, recipes, theme  # noqa: E402
from prismlab.dialogs import StageDialog  # noqa: E402

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


def wait(eng, limit=60.0):
    end = time.time() + limit
    while time.time() < end:
        app.processEvents()
        if eng.run is not None and all(i.finished for i in eng.run.items):
            return
        time.sleep(0.05)
    raise AssertionError("時間内に終わりません: " + str([(i.state, i.stage, i.error) for i in eng.run.items]))


llm = FakeLLM()
settings.set_llm_url(llm.url)
settings.set_enhance_model("test-model")
ENHANCER = next(p.name for p in enhancer.load_presets() if not p.is_empty)
a = FakeComfy(polls=1)
I2I = TMP / "Clone I2I CN.json"
I2I.write_text(json.dumps(workflow_cn()), encoding="utf-8")
src = []
for i in range(3):
    img = QImage(48, 32, QImage.Format.Format_RGB32)
    img.fill(QColor(40 * i, 120, 200))
    p = TMP / f"img{i}.png"
    img.save(str(p))
    src.append(p)


def run(stages, sources):
    eng = engine.Engine(run_file=None)
    eng.set_machines([a.url])
    eng.start(engine.Run.create(recipes.Recipe("t", folder=str(TMP), stages=stages), sources))
    wait(eng)
    return eng


def sent(since):
    return [wf["2"]["inputs"]["text"] for wf in a.received[since:]]


def translate_calls(since):
    return [c for c in llm.calls[since:] if "英語に翻訳" in c["messages"][0]["content"]]


# 1. 訳すかどうかの見分け
check(engine.needs_english("赤い帽子") and engine.needs_english("a cat, 赤い帽子")
      and not engine.needs_english("a red hat, (smile:1.2), <lora:x:0.8>") and not engine.needs_english(None)
      and not engine.needs_english(""), "日本語などが入っている文だけを訳す")

# 2. 追加テキスト (日本語) → 英語に → 生成: 3 件でも同じ文は 1 回だけ訳す
n0, c0 = len(a.received), len(llm.calls)
eng = run([recipes.Stage(recipes.TEXT, text="赤い帽子の猫"), recipes.Stage(recipes.TRANSLATE),
           recipes.Stage(recipes.GEN, workflow=str(I2I))], src)
texts = sent(n0)
check(all(it.state == engine.DONE for it in eng.run.items), "全部できる")
check(texts == ["赤い帽子の猫 in English"] * 3, f"英語にした文を送る ({texts})")
calls = translate_calls(c0)
check(len(calls) == 1, f"同じ文は 1 回だけ訳す ({len(calls)} 回)")
check(calls[0]["model"] == "test-model" and calls[0]["messages"][-1]["content"] == "赤い帽子の猫",
      "エンハンサーと同じモデルに、その文だけを送る")
check(eng.run.items[0].outputs[0]["adds"][0]["text"] == "赤い帽子の猫 in English", "結果の欄の足した文も訳したあとの文")

# 3. 英語だけの文は送らない / 盛ったあとの文も訳す
c0 = len(llm.calls)
run([recipes.Stage(recipes.TEXT, text="a red hat"), recipes.Stage(recipes.TRANSLATE),
     recipes.Stage(recipes.GEN, workflow=str(I2I))], src[:1])
check(not translate_calls(c0), "英語だけなら LLM に送らない")
n0, c0 = len(a.received), len(llm.calls)
run([recipes.Stage(recipes.TEXT, text="a cat"), recipes.Stage(recipes.ENHANCE, enhancer=ENHANCER),
     recipes.Stage(recipes.TEXT, text="笑顔"), recipes.Stage(recipes.TRANSLATE),
     recipes.Stage(recipes.GEN, workflow=str(I2I))], src[:1])
text = sent(n0)[0]
check("笑顔 in English" in text and "a cat enhanced" in text and "笑顔\n" not in text,
      f"盛った文はそのまま、あとに足した日本語だけ訳す ({text!r})")
check([c["messages"][-1]["content"] for c in translate_calls(c0)] == ["笑顔"], "訳すのは日本語の文だけ")

# 4. ラインの見立て・設定の窓・バイパス
P = recipes.line_problems
check(("notext", 0) in P([recipes.Stage(recipes.TRANSLATE), recipes.Stage(recipes.GEN, workflow=str(I2I))]),
      "文がないところの英語には赤")
check(not P([recipes.Stage(recipes.READ, reader="x"), recipes.Stage(recipes.TRANSLATE),
             recipes.Stage(recipes.GEN, workflow=str(I2I))]), "読み取りのあとなら問題なし")
check(recipes.TRANSLATE in recipes.STAGE_ORDER and recipes.STAGE_TITLES[recipes.TRANSLATE] == "英語に",
      "＋ ステップのメニューに「英語に」")
st = recipes.Stage(recipes.TRANSLATE)
d = StageDialog(st)
check("test-model" in d.model_label.text(), f"設定の窓に使うモデル ({d.model_label.text()})")
d._accept()
check(d.result() == 1, "設定の窓は OK で閉じる")
snap = engine.stage_snapshot(st)
check(snap["model"] == "test-model" and snap["url"] == llm.url, "始めたときのサーバー・モデルを写す")
bst = recipes.Stage(recipes.TRANSLATE, bypass=True)
check(engine.stage_snapshot(bst).get("bypass"), "バイパスにもできる")
saved = recipes.Recipe("r", stages=[recipes.Stage(recipes.TRANSLATE)])
recipes.save([saved])
check(recipes.load()[0].stages[0].type == recipes.TRANSLATE, "保存して読み直せる")

print(f"\nすべて OK ({len(ok)})")
