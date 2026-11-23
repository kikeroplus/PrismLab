"""LLM (LM Studio) が落ちているとき (2026-11-23 利用者の希望)。

- 「英語に」は訳さずに先へ進め、知らせる (件の warning・量産のまとめ・ほかのアプリへの進み具合)。
- LLM が要るステップ (盛る) は今までどおり失敗で止める。

    python tests\\test_llm_offline.py
"""

import json
import os
import socket
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="prismlab_llmoff_"))
os.environ["APPDATA"] = str(TMP / "appdata")
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, str(ROOT.parent / "GenCommon"))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtGui import QColor, QImage  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from fakes import FakeComfy, workflow_i2v  # noqa: E402
from gencommon import enhancer, llm, recipe_link, settings, store  # noqa: E402
from prismlab import engine, external, profile, recipes, translate  # noqa: E402

app = QApplication(sys.argv)
profile.setup()
assert str(store.APP_DIR).startswith(str(TMP)), store.APP_DIR

failures = []


def check(cond, message):
    print(("OK   " if cond else "NG   ") + message)
    if not cond:
        failures.append(message)


# 誰も待っていないポート (LM Studio が動いていない)
sock = socket.socket()
sock.bind(("127.0.0.1", 0))
DEAD = f"http://127.0.0.1:{sock.getsockname()[1]}/v1"
sock.close()
settings.set_llm_url(DEAD)

# 1. 繋がらないときは LLMOffline
try:
    translate.to_english({"url": DEAD, "model": "m"}, "赤い帽子")
    check(False, "1: 繋がらないのに訳せた")
except llm.LLMOffline as exc:
    check(isinstance(exc, llm.LLMError), f"1: 繋がらないときは LLMOffline (LLMError の仲間): {exc}")

WF = TMP / "wf"
WF.mkdir()
(WF / "i2v.json").write_text(json.dumps(workflow_i2v()), encoding="utf-8")
folder = TMP / "in"
folder.mkdir()
src = []
for i in range(3):
    img = QImage(32, 24, QImage.Format.Format_RGB32)
    img.fill(QColor(30 * i, 100, 200))
    p = folder / f"img{i + 1}.png"
    img.save(str(p))
    src.append(p)


def wait(eng, seconds=60):
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        if eng.run is not None and all(i.finished for i in eng.run.items) and not eng.active:
            return
        time.sleep(0.05)
    raise AssertionError([(i.state, i.stage, i.status, i.error) for i in eng.run.items])


a = FakeComfy(polls=1)

# 2. 追加テキスト (日本語) → 英語に → 生成: 訳さずに送り、最後まで作る
r = recipes.Recipe("英語にして生成", folder=str(folder), inherit=False,
                   stages=[recipes.Stage(recipes.TEXT, text="赤い帽子の猫"), recipes.Stage(recipes.TRANSLATE),
                           recipes.Stage(recipes.GEN, workflow=str(WF / "i2v.json"), seconds=3)])
eng = engine.Engine(run_file=None)
eng.set_machines([a.url])
started = time.time()
eng.start(engine.Run.create(r, src))
wait(eng)
items = eng.run.items
check(all(i.state == engine.DONE for i in items), f"2: LLM が落ちていても最後まで作る ({[i.state for i in items]})")
check(all(i.warning == engine.ENGLISH_SKIPPED for i in items), "2: どの件にも「英語にせずに送りました」の知らせ")
sent = [json.dumps(w, ensure_ascii=False) for w in a.received]
check(len(sent) == 3 and all("赤い帽子の猫" in s for s in sent), "2: 訳さずに元の文のまま送った")
check(time.time() - started < 30, f"2: 件ごとに長く待たない ({time.time() - started:.1f} 秒)")
data = engine.Item.from_dict(items[0].to_dict())
check(data.warning == engine.ENGLISH_SKIPPED, "2: 知らせは続きのファイルにも残る")

# 3. 盛るは LLM が要るので、今までどおり失敗で止める
preset = next(p for p in enhancer.load_presets() if not p.is_empty)
r2 = recipes.Recipe("盛って生成", folder=str(folder), inherit=False,
                    stages=[recipes.Stage(recipes.TEXT, text="a cat"), recipes.Stage(recipes.ENHANCE, enhancer=preset.name),
                            recipes.Stage(recipes.GEN, workflow=str(WF / "i2v.json"), seconds=3)])
eng2 = engine.Engine(run_file=None)
eng2.set_machines([a.url])
before = len(a.received)
eng2.start(engine.Run.create(r2, src[:2]))
wait(eng2)
check(all(i.state == engine.FAILED and "接続できません" in i.error for i in eng2.run.items),
      f"3: 盛るは失敗で止める ({[i.error[:30] for i in eng2.run.items]})")
check(len(a.received) == before, "3: 生成には進まない")

# 4. ほかのアプリへの進み具合にも知らせを書く (PWC などが出す)
req = external.Request("t1", "PromptWordCrafter", "英語にして生成")
req.engine = eng
req.finished_at = time.time()
st = req.to_status()
check(st.get("untranslated") == 3 and "英語にせずに送りました (3 件)" in st.get("warning", ""), f"4: 進み具合: {st.get('warning')}")
status = recipe_link.Status(**{k: v for k, v in st.items() if k in recipe_link.Status.__dataclass_fields__})
from gencommon import recipe_panel  # noqa: E402

line = recipe_panel._line(status)
check("⚠ LLM に繋がらないので、英語にせずに送りました (3 件)" in line, f"4: 頼んだアプリの 1 行: {line}")

eng.stop_threads()
eng2.stop_threads()
a.close()
print()
if failures:
    print(f"NG {len(failures)} 件")
    sys.exit(1)
print("すべて OK")
