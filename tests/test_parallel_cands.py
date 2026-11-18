"""物語のタブで、別のシーンの候補を並べて作る (2026-11-18 利用者の希望)。
シーンチェンジなら前後に依存がないので並べて作れる。「続き」は前のシーンを作っているあいだ待つ。

    python tests\\test_parallel_cands.py
APPDATA を一時フォルダに差し替えるので、実際の設定には触れない。
"""

import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="prismlab_parallel_"))
os.environ["APPDATA"] = str(TMP / "appdata")
sys.path.insert(0, str(ROOT.parent / "GenCommon"))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtGui import QColor, QImage  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from fakes import FakeComfy, make_video, workflow_i2v  # noqa: E402
from gencommon import comfy, store  # noqa: E402
from prismlab import profile, recipes, recycle, story, story_view, theme  # noqa: E402


def fake_bin(paths, timeout=3.0):  # 本物のごみ箱には入れない
    for p in paths:
        Path(p).unlink()
    return None


recycle.send_to_recycle_bin = fake_bin
app = QApplication(sys.argv)
theme.apply(app)
profile.setup()
assert str(store.APP_DIR).startswith(str(TMP))
ok = []


def check(cond, label):
    assert cond, label
    ok.append(label)
    print("OK  ", label)


def pump(seconds: float):
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        time.sleep(0.02)


clip = TMP / "clip.mp4"
make_video(clip, [(200, 40, 40)] * 4 + [(40, 60, 220)] * 4, size=(160, 96))
a = FakeComfy(polls=6)
a.video_bytes = clip.read_bytes()
comfy.set_connections([comfy.Preset("メイン", a.url)] + [comfy.Preset() for _ in range(comfy.PRESET_COUNT - 1)])
wf = TMP / "Wan I2V.json"
wf.write_text(json.dumps(workflow_i2v()), encoding="utf-8")
comfy.set_workflows([comfy.Preset("Wan I2V", str(wf))] + [comfy.Preset() for _ in range(comfy.WORKFLOW_COUNT - 1)])
recipes.set_machines([a.url])


def photo(name, rgb):
    path = TMP / name
    img = QImage(320, 200, QImage.Format.Format_RGB32)
    img.fill(QColor(*rgb))
    img.save(str(path))
    return path


folder = TMP / "物語" / "並べる"
st = story.Story.create(str(folder), "並べる")
s1 = st.scenes[0]
st.set_source_file(s1, str(photo("駅.png", (40, 160, 90))))
s2 = st.add_scene(s1, story.CUT)
st.set_source_file(s2, str(photo("海.png", (40, 90, 200))))
s3 = st.add_scene(s2, story.CONT)
for s in st.scenes:
    s.workflow, s.count, s.original = str(wf), 2, "歩く"
st.save()

sv = story_view.StoryView()
sv.resize(1300, 860)
sv.show()
messages = []
sv.status.connect(messages.append)
sv.open_folder(str(folder))
pump(0.5)
s1, s2, s3 = sv.story.scenes


def wait_all(limit=90):
    end = time.time() + limit
    while time.time() < end and sv.candidates_busy():
        pump(0.1)
    pump(0.5)


# 1. シーン 1 とシーンチェンジのシーン 2 を並べて作る
sv.select(s1)
check(sv.make_candidates_for(s1) == "", "シーン 1 の候補を作り始める")
check("作っています" in sv.make_candidates_for(s1), "同じシーンは重ねて作らない")
sv.select(s2)
pump(0.2)
check(sv.make_b.isEnabled() and "ほかの 1 シーン" in sv.gen_state.text(),
      f"シーンチェンジのシーンは、ほかのシーンを作っていても押せる ({sv.gen_state.text()})")
check(sv.make_candidates_for(s2) == "", "シーンチェンジのシーン 2 も作り始める")
check(sv._job_for(s1.id) is not None and sv._job_for(s2.id) is not None and len(sv._running_jobs()) == 2,
      "2 つのシーンを並べて作っている")
pump(0.3)
check(sv.stop_b.isVisible() and sv.gen_state.text().startswith("作っています"), "選んでいるシーンの進みを出す")
# 2. 「続き」のシーン 3 は、前のシーン 2 を作っているあいだは待つ
sv.select(s3)
pump(0.3)
check(not sv.make_b.isEnabled() and "前のシーン" in sv.gen_state.text() and not sv.stop_b.isVisible(),
      f"続きのシーンは、前のシーンを作っているあいだ押せない ({sv.gen_state.text()})")
check("前のシーンの候補を作っています" in sv.make_candidates_for(s3), "続きのシーンは前のシーンを待つ (理由を返す)")
cards = [c.state.text() for c in sv.scene_cards]
check(cards[0] == cards[1] == "候補を作っています…", f"絵コンテの札も両方 ({cards})")
wait_all()
check(len(s1.candidates) == 2 and len(s2.candidates) == 2, (len(s1.candidates), len(s2.candidates)))
check(all(Path(c.path).parent == sv.story.scene_dir(s) for s in (s1, s2) for c in s.candidates),
      "出来た候補は、それぞれのシーンに入る")
check(all(c.prompt and c.original == "歩く" for c in s1.candidates + s2.candidates), "候補に送った文も残す")
saved = json.loads((folder / story.STORY_FILE).read_text(encoding="utf-8"))
check(sum(len(x.get("candidates", [])) for x in saved["scenes"]) == 4, "物語のファイルにも書く")

# 3. シーン 2 を採用 → シーン 3 (続き) が作れる。シーン 1 と並べて作る
sv.select(s2)
pump(0.2)
sv.cands.setCurrentRow(0)
sv.adopt_only.click()
pump(0.5)
check(s2.adopted and Path(s3.source).is_file(), "シーン 2 を採用すると、シーン 3 の元画像が入る")
sv.select(s3)
pump(0.2)
check(sv.make_b.isEnabled(), "前のシーンが終わったら、続きのシーンも押せる")
check(sv.make_candidates_for(s3) == "" and sv.make_candidates_for(s1) == "", "シーン 3 とシーン 1 を並べて作る")
# 4. 作っている「続き」のシーンの元画像を変えない: 前のシーンの採用は変えられない
sv.select(s2)
pump(0.2)
adopted = s2.adopted
other = next(i for i in range(sv.cands.count()) if sv.cands.item(i).data(256) != adopted)
sv.cands.setCurrentRow(other)
sv.adopt_only.click()
pump(0.3)
check(s2.adopted == adopted and "次のシーン" in messages[-1], f"次の続きのシーンを作っているあいだ、採用を変えない ({messages[-1]})")
sv.cands.setCurrentRow(next(i for i in range(sv.cands.count()) if sv.cands.item(i).data(256) == adopted))
sv._delete_candidates()
pump(0.3)
check(Path(adopted).is_file() and s2.adopted == adopted, "採用している候補も消さない")
# 5. 作っているシーンの元画像は変えない
before = Path(s3.source).stat().st_mtime_ns
sv.select(s3)
sv._set_source(lambda: sv.story.set_source_file(sv.scene, str(TMP / "駅.png")))
check(Path(s3.source).stat().st_mtime_ns == before, "作っているシーンの元画像は変えない")
# 6. 中止は選んでいるシーンだけ
sv.select(s1)
pump(0.1)
sv._stop_job()
end = time.time() + 30
while time.time() < end and sv._job_for(s1.id) is not None:
    pump(0.1)
check(sv._job_for(s1.id) is None and sv._job_for(s3.id) is not None, "中止は選んでいるシーンだけ止める")
wait_all()
check(len(s3.candidates) == 2, f"シーン 3 の候補も出来る ({len(s3.candidates)})")
sv.select(s2)
pump(0.2)
sv.cands.setCurrentRow(other)
sv.adopt_only.click()
pump(0.3)
check(s2.adopted != adopted, "作り終わったら、採用を変えられる")
# 7. 終わった候補づくりは片付ける
check(sv.make_candidates_for(s1) == "", "もう一度作る")
check(len(sv._jobs) == 1, f"終わったものは片付けて残さない ({len(sv._jobs)})")
wait_all()
sv.shutdown()
print(f"すべて OK ({len(ok)})")
