"""PC1 の PrismLab で作った結果を、PC2 の PromptWordCrafter の生成結果に出す (2026-11-18 利用者の希望)。

PrismLab は生成の記録を、この PC の記録に加えて共有の置き場所 (shared_dir.txt) の
history\\PrismLab.(PC名).txt にも書く。PromptWordCrafter はほかの PC のものも読む。
PC ごとに APPDATA と COMPUTERNAME を変えた別のプロセスで動かす (実際の設定には触れない)。

    python tests\\test_shared_history.py
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="prismlab_sharedhist_"))
SHARE = TMP / "共有" / "GenCommon"  # ネットワークの共有フォルダの代わり
WORK = TMP / "共有" / "作業"  # 両方の PC が同じパスで見るフォルダ (テキストと出力)
ok = []


def check(cond, label):
    assert cond, label
    ok.append(label)
    print("OK  ", label)


def run(pc: str, code: str, shared: bool = True) -> dict:
    appdata = TMP / pc
    (appdata / "GenCommon").mkdir(parents=True, exist_ok=True)
    pointer = appdata / "GenCommon" / "shared_dir.txt"
    if shared:
        pointer.write_text(str(SHARE), encoding="utf-8")
    elif pointer.exists():
        pointer.unlink()
    env = {**os.environ, "APPDATA": str(appdata), "COMPUTERNAME": pc, "PYTHONIOENCODING": "utf-8"}
    head = (f"import sys, json\nsys.path[:0] = [{str(ROOT.parent / 'GenCommon')!r}, {str(ROOT)!r}, "
            f"{str(ROOT.parent / 'PromptWordCrafter')!r}]\nfrom pathlib import Path\nWORK = Path({str(WORK)!r})\n")
    out = subprocess.run([sys.executable, "-c", head + code], env=env, capture_output=True, text=True,
                         encoding="utf-8", timeout=120)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


SHARE.mkdir(parents=True)
WORK.mkdir(parents=True)
text = WORK / "雨の駅.txt"
text.write_text("雨の駅で待つ", encoding="utf-8")
(WORK / "出力").mkdir()

# PC1: PrismLab で作る (量産の記録と同じく、入口のテキストを origin に)
WRITE = '''
from gencommon import comfy, store
from prismlab import profile
profile.setup()
def made(name, origin_text):
    out = WORK / "出力" / name
    out.write_bytes(b"png")
    req = comfy.GenRequest(prompt="rain", origin=str(WORK / "雨の駅.txt"), origin_text=origin_text, recipe="駅", step=1)
    comfy.add_history(str(out), str(WORK / "雨の駅.txt"), req)
made("雨の駅_t2i.png", "雨の駅で待つ")
made("雨の駅_t2i_video.mp4", "雨の駅で待つ")
print(json.dumps({"local": len(comfy.read_history()), "dir": store.APP_DIR.name}))
'''
r = run("PC1", WRITE)
check(r == {"local": 2, "dir": "PrismLab"}, f"PC1: この PC の記録にも書く ({r})")
shared_file = SHARE / "history" / "PrismLab.PC1.txt"
check(shared_file.is_file() and len(shared_file.read_text(encoding="utf-8").splitlines()) == 2,
      "PC1: 共有の置き場所に PrismLab.PC1.txt として書く")

# PC2: PromptWordCrafter の生成結果に出る
READ = '''
from promptwordcrafter import profile, results
profile.setup()
found = results.find_results(WORK / "雨の駅.txt", "雨の駅で待つ")
print(json.dumps([[r.path.name, r.prismlab, r.old] for r in found]))
'''
r = run("PC2", READ)
names = sorted(x[0] for x in r)
check(names == ["雨の駅_t2i.png", "雨の駅_t2i_video.mp4"] and all(x[1] for x in r if x[0].endswith(".mp4")),
      f"PC2: PC1 の PrismLab の結果が、PromptWordCrafter の生成結果に出る ({r})")
check(not any(x[2] for x in r), "本文が同じなら「旧」にしない")

# PC2 でも PrismLab で作る → PC1 からは PC2 の分が見え、自分の分は重ならない
WRITE2 = WRITE.replace('made("雨の駅_t2i.png", "雨の駅で待つ")\n', "").replace(
    'made("雨の駅_t2i_video.mp4", "雨の駅で待つ")', 'made("雨の駅_i2i.png", "前の文")')
run("PC2", WRITE2)
check((SHARE / "history" / "PrismLab.PC2.txt").is_file(), "PC2 の分は PrismLab.PC2.txt (PC ごとに別のファイル)")
r = run("PC1", READ)
by = {x[0]: x for x in r}
check("雨の駅_i2i.png" in by and by["雨の駅_i2i.png"][1], f"PC1: PC2 で作った結果も出る ({r})")
check(by["雨の駅_i2i.png"][2], "PC2 の記録の本文が今と違えば「旧」")
check(len(r) == len(by), "同じファイルを 2 度出さない")

# テキストの名前を変えると、ほかの PC の記録の入口も付け替える
RENAME = '''
from promptwordcrafter import profile
from gencommon import comfy
profile.setup()
n = comfy.rename_in_history({str(WORK / "雨の駅.txt"): str(WORK / "夜の駅.txt")}, "PrismLab")
print(json.dumps(n))
'''
(WORK / "雨の駅.txt").rename(WORK / "夜の駅.txt")
n = run("PC2", RENAME)
check(n == 3, f"名前を変えたら、どの PC の記録の入口も付け替える ({n})")
r = run("PC2", READ.replace("雨の駅.txt", "夜の駅.txt"))
check(len(r) == 3, f"付け替えたあとも出る ({[x[0] for x in r]})")

# 共有の置き場所を決めていない PC は、今までどおり (共有に書かない・読まない)
run("PC3", WRITE.replace('"雨の駅_t2i.png"', '"PC3だけ.png"').replace("雨の駅.txt", "夜の駅.txt"), shared=False)
check(not (SHARE / "history" / "PrismLab.PC3.txt").exists(), "共有の置き場所がなければ、共有に書かない")
check(not any(x[0] == "PC3だけ.png" for x in run("PC2", READ.replace("雨の駅.txt", "夜の駅.txt"))),
      "その PC の結果は、ほかの PC には出ない (今までどおり)")
# 共有の置き場所を決める前に作った分も、次に PrismLab を起動したときに写す (1 回だけ)
START = '''
from prismlab import profile
profile.setup()
print(json.dumps(0))
'''
run("PC3", START)
seeded = SHARE / "history" / "PrismLab.PC3.txt"
check(seeded.is_file() and "PC3だけ.png" in seeded.read_text(encoding="utf-8"),
      "共有の置き場所を決めたあと起動したら、今までの記録を写す")
check(any(x[0] == "PC3だけ.png" for x in run("PC2", READ.replace("雨の駅.txt", "夜の駅.txt"))),
      "写した分も、ほかの PC に出る")
before = seeded.read_text(encoding="utf-8")
run("PC3", START)
check(seeded.read_text(encoding="utf-8") == before, "2 回目の起動では写さない (重ならない)")
print(f"すべて OK ({len(ok)})")
