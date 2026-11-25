"""続きのファイルをまとめて書く (engine.SAVE_EVERY) のテスト。ComfyUI は使わない。

    python tests\\test_save_throttle.py
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

from PySide6.QtWidgets import QApplication  # noqa: E402

from gencommon import store  # noqa: E402
from prismlab import engine, external, profile  # noqa: E402

app = QApplication(sys.argv)
profile.setup()
assert str(store.APP_DIR).startswith(str(TMP)), store.APP_DIR

failures = []


def check(name, ok):
    print(("OK  " if ok else "NG  ") + name)
    if not ok:
        failures.append(name)


class StubRun:
    """_save が使うところだけ (件は数だけ数える)。"""

    def __init__(self):
        self.paused = False
        self.items = []
        self.stages = []
        self.n = 0

    def to_dict(self):
        return {"n": self.n}


def written(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))["n"]
    except (OSError, ValueError, KeyError):
        return None


engine.SAVE_EVERY = 0.6
eng = engine.Engine(None, run_file="t/run.json")
path = store.app_file("t/run.json")
run = StubRun()
eng.run = run

# 1. 初めの 1 回はすぐ書く
run.n = 1
eng._save()
check("初めはすぐ書く", written(path) == 1)

# 2. 続けて変わっても、まとめる (すぐには書かない)
for i in range(2, 30):
    run.n = i
    eng._save()
check("間隔の中ではまとめる", written(path) == 1)
check("まとめて書くのを待っている", eng._dirty)

# 3. 間隔が過ぎれば、最後の中身を書く
time.sleep(1.2)
check("間隔が過ぎたら最後を書く", written(path) == 29)
check("書いたら待ちなし", not eng._dirty and eng._save_timer is None)

# 4. now はすぐ書く (頼んだ番号・止める・始める)
run.n = 40
eng._save()
run.n = 41
eng._save(now=True)
check("now はすぐ書く", written(path) == 41)
check("now で待ちも片付く", not eng._dirty)
time.sleep(1.0)
check("あとで古い中身に戻らない", written(path) == 41)

# 5. 閉じる (stop_threads) ときは待っているものを書く
run.n = 50
eng._save()
run.n = 51
eng._save()
check("閉じる前は待っている", written(path) == 50 and eng._dirty)
eng.stop_threads()
check("閉じるときに書く", written(path) == 51)

# 6. 書かない (run_file を外した: 取り消し) なら、待っていたものも書かない
eng2 = engine.Engine(None, run_file="t/run2.json")
path2 = store.app_file("t/run2.json")
run2 = StubRun()
eng2.run = run2
eng2._save()
path2.unlink()
run2.n = 5
eng2._save()
eng2.run_file = None
time.sleep(1.0)
eng2.flush()
check("取り消したら作り直さない", not path2.exists())

# 7. 起動したとき、古い書きかけ (*.tmp) を消す。新しいものは残す
folder = store.app_file(external.EXT_DIR)
folder.mkdir(parents=True, exist_ok=True)
old = folder / "Pro-1.json.PC.123.tmp"
new = folder / "Pro-2.json.PC.456.tmp"
old.write_text("{", encoding="utf-8")
new.write_text("{", encoding="utf-8")
t = time.time() - external.STALE_TMP - 60
os.utime(old, (t, t))
ext = external.ExternalRuns(None)
ext.resume()
check("古い書きかけを消す", not old.exists())
check("新しい書きかけは残す", new.exists())

shutil.rmtree(TMP, ignore_errors=True)
print("NG: " + ", ".join(failures) if failures else "all OK")
sys.exit(1 if failures else 0)
