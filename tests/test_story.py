"""物語のデータ (story.py) のテスト。APPDATA を一時フォルダに差し替え、本物の短い mp4 を OpenCV で作って使う。

    python tests\\test_story.py
"""

import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="prismlab_story_"))
os.environ["APPDATA"] = str(TMP / "appdata")  # gencommon を読み込む前に差し替える (実際の設定に触れない)
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, str(ROOT.parent / "GenCommon"))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtGui import QColor, QImage  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from fakes import make_video  # noqa: E402
from gencommon import store  # noqa: E402
from prismlab import profile, story  # noqa: E402

app = QApplication(sys.argv)
profile.setup()
assert str(store.APP_DIR).startswith(str(TMP)), store.APP_DIR

failures = []


def check(ok: bool, label: str) -> None:
    print(("OK   " if ok else "NG   ") + label)
    if not ok:
        failures.append(label)


def color_at(path: Path) -> tuple:
    img = QImage(str(path))
    c = img.pixelColor(img.width() // 2, img.height() // 2)
    return c.red(), c.green(), c.blue()


def near(a: tuple, b: tuple, tol: int = 40) -> bool:
    return all(abs(x - y) <= tol for x, y in zip(a, b))


# 1. 作る・開く
folder = TMP / "雨の駅"
st = story.Story.create(str(folder), "雨の駅")
check((folder / story.STORY_FILE).exists() and len(st.scenes) == 1 and st.scenes[0].link == story.CUT,
      "1: 物語を作ると、シーン 1 つ (シーンチェンジ) で始まる")
try:
    story.Story.create(str(folder), "x")
    check(False, "1: 同じフォルダに 2 つ目は作らない")
except story.StoryError:
    check(True, "1: 同じフォルダに 2 つ目は作らない")
story.remember(str(folder))
check(story.recent() == [str(folder)], "1: 最近開いた物語に残る")

# 2. 元画像 (手元の画像) は、シーンのフォルダに PNG で写す
src = TMP / "手元.jpg"
img = QImage(320, 200, QImage.Format.Format_RGB32)
img.fill(QColor(30, 200, 40))
img.save(str(src))
s1 = st.scenes[0]
s1.name = "ホーム"
st.set_source_file(s1, str(src))
check(Path(s1.source) == st.scene_dir(s1) / story.SOURCE_NAME and Path(s1.source).is_file(),
      "2: 手元の画像はシーンのフォルダの source.png に写す")

# 3. 続き: 前のシーンの採用の最後のコマが元画像になる
v1 = TMP / "cand1.mp4"
make_video(v1, [(255, 0, 0), (0, 255, 0), (0, 0, 255)])  # 赤 → 緑 → 青 (RGB)
v1b = TMP / "cand1b.mp4"
make_video(v1b, [(0, 0, 255), (255, 255, 0)])  # 青 → 黄
s2 = st.add_scene(s1)
s2.name = "振り返る"
check(s2.link == story.CONT and s2.source == "", "3: 足したシーンは「続き」。前に採用がなければ元画像は空")
st.adopt(s1, str(v1))
check(Path(s2.source).is_file() and near(color_at(Path(s2.source)), (0, 0, 255)),
      f"3: 採用すると、続きのシーンの元画像は最後のコマ ({color_at(Path(s2.source)) if s2.source else None})")
st.adopt(s1, str(v1b))
check(near(color_at(Path(s2.source)), (255, 255, 0)), "3: 採用を変えると、続きの元画像も作り直す")
count = story.frame_count(str(v1))
check(count == 3, f"3: コマの数 ({count})")

# 4. シーンチェンジにすると選び直し、前のシーンのコマも選べる
s3 = st.add_scene(s2, link=story.CUT)
s3.name = "改札"
check(s3.link == story.CUT and s3.source == "", "4: シーンチェンジのシーンは元画像を選ぶまで空")
st.set_source_frame(s3, str(v1), 1)
check(near(color_at(Path(s3.source)), (0, 255, 0)) and s3.source_kind == story.SOURCE_FRAME,
      "4: 前のシーンの好きなコマ (2 コマ目 = 緑) を元画像に")
st.set_link(s2, story.CUT)
check(s2.source == "" and s2.link == story.CUT, "4: 続き → シーンチェンジに変えたら、最後のコマは外す")
st.set_link(s2, story.CONT)
check(Path(s2.source).is_file(), "4: 続きに戻すと、最後のコマを作り直す")

# 5. 採用のフォルダを番号順に揃える
v2 = TMP / "cand2.mp4"
make_video(v2, [(10, 10, 10)])
st.adopt(s2, str(v2))
names = sorted(p.name for p in st.adopted_dir().iterdir())
check(names == ["01_ホーム.mp4", "02_振り返る.mp4"], f"5: 採用は 01_名前.mp4 の番号順 ({names})")
(st.adopted_dir() / "メモ.txt").write_text("手で置いたもの", encoding="utf-8")
st.move_scene(s2, -1)
names = sorted(p.name for p in st.adopted_dir().iterdir() if p.suffix == ".mp4")
check(names == ["01_振り返る.mp4", "02_ホーム.mp4"], f"5: 並べ替えたら番号を揃え直す ({names})")
check((st.adopted_dir() / "メモ.txt").exists(), "5: 手で置いたファイルは消さない")
st.move_scene(s2, 1)
st.adopt(s2, str(v2))  # もう一度押すと採用をやめる
check(s2.adopted == "" and not any(p.name.startswith("02_") for p in st.adopted_dir().iterdir()),
      "5: 採用をやめると、その番号のファイルも消す")

# 5b. 候補を外す: 採用していたものなら、採用をやめて揃え直す
st.adopt(s2, str(v2))
s2.candidates = [story.Candidate(str(v2)), story.Candidate(str(v1b))]
st.remove_candidates(s2, [str(v1b)])
check([c.path for c in s2.candidates] == [str(v2)] and s2.adopted == str(v2), "5b: 採用していない候補を外しても採用はそのまま")
st.remove_candidates(s2, [str(v2)])
check(s2.adopted == "" and not any(p.name.startswith("02_") for p in st.adopted_dir().iterdir()),
      "5b: 採用していた候補を外したら、採用をやめて番号のファイルも消す")

# 5c. プロンプトの履歴
h = story.Scene()
h.prompt = "元の文"
h.record("読み取りの前の文", "元の文", before=True)
h.prompt = "読み取った文"
h.record("読み取った文", "読み取った文")
h.record("読み取った文", "読み取った文")
check([x["label"] for x in h.history] == ["読み取りの前の文", "読み取った文"], "5c: 同じ文は続けて残さない")
check(h.undo_target() == "元の文", "5c: 元に戻すは、直前の操作の前の文")
h.record("空", "  ")
check(len(h.history) == 2, "5c: 空の文は残さない")
for n in range(story.MAX_HISTORY + 10):
    h.record("盛った文", f"版 {n}")
check(len(h.history) == story.MAX_HISTORY and h.history[0]["text"] == "元の文",
      "5c: 多くなっても、いちばん最初の文は消さない")
h2 = story.Scene.from_dict(h.to_dict())
check(h2.history == h.history, "5c: 履歴を保存して読める")

# 5d. 元のプロンプトと送るプロンプト
o = story.Scene()
o.original = "左から女性が歩いてきて右に消える"
check(o.send_text == o.original, "5d: 送るプロンプトが空なら、元のプロンプトを送る")
o.prompt = "a woman walks"
check(o.send_text == "a woman walks", "5d: 送るプロンプトがあれば、それを送る")
old_scene = story.Scene.from_dict({"prompt": "前に書いた文"})
check(old_scene.original == "前に書いた文", "5d: 元のプロンプトがなかったころの物語は、今の文を元のプロンプトに")
check(story.Scene.from_dict(o.to_dict()).original == o.original, "5d: 元のプロンプトを保存して読める")

# 5e. リファレンス画像と、候補ごとのプロンプトの記録
st.set_reference(s3, str(src))
check(Path(s3.reference) == st.scene_dir(s3) / story.REFERENCE_NAME and Path(s3.reference).is_file(),
      "5e: リファレンス画像はシーンのフォルダの reference.png に写す")
c = story.Candidate(str(v1), "盛った文", 2.0, "元の文", s3.reference)
c2 = story.Candidate.from_dict(c.to_dict())
check((c2.original, c2.prompt, c2.reference) == ("元の文", "盛った文", s3.reference),
      "5e: 候補に、そのときの元のプロンプト・盛ったあとのプロンプト・リファレンスを残す")
check(story.Candidate.from_dict({"path": "x.mp4", "prompt": "p"}).original == "", "5e: 前の形の候補も読める")

# 6. 保存して開き直す
s1.prompt = "濡れた改札を抜ける女性"
s1.candidates.append(story.Candidate(str(v1), "濡れた改札", 1.0))
st.save()
st2 = story.Story.load(str(folder))
check([s.name for s in st2.scenes] == ["ホーム", "振り返る", "改札"] and st2.scenes[0].prompt == "濡れた改札を抜ける女性"
      and st2.scenes[0].candidates[0].path == str(v1) and st2.scenes[2].source_kind == story.SOURCE_FRAME,
      "6: 保存して開き直しても同じ")
st2.remove_scene(st2.scenes[0])
check(st2.scenes[0].link == story.CUT, "6: 先頭を外したら、次のシーンが先頭 (シーンチェンジ) になる")

# 7. 候補の好きなコマから次のシーンを始める
fs = story.Story.create(str(TMP / "コマから"), "コマから")
f1 = fs.scenes[0]
fs.set_source_file(f1, str(src))
n = fs.start_next_from_frame(f1, str(v1), 1)
check(len(fs.scenes) == 2 and fs.scenes[1] is n and f1.adopted == str(v1),
      "7: 次のシーンがなければ足し、候補を採用する")
check(n.link == story.CUT and n.source_kind == story.SOURCE_FRAME and n.source_from == str(v1)
      and near(color_at(Path(n.source)), (0, 255, 0)), "7: 途中のコマ (2 コマ目 = 緑) はシーンチェンジ (前のシーンのコマ)")
check((fs.adopted_dir() / "01_scene.mp4").is_file(), "7: 採用のフォルダも揃える")
fs.adopt(f1, str(v1b))
check(near(color_at(Path(n.source)), (0, 255, 0)), "7: あとで採用を変えても、選んだコマのまま")
check(not fs.next_source_taken(f1, str(v1)), "7: 同じ動画のコマなら、置き換えるときに確かめない")
check(fs.next_source_taken(f1, str(v1b)), "7: ほかの動画のコマが入っていたら、置き換える前に確かめる")
n2 = fs.start_next_from_frame(f1, str(v1b), 1, last=True)
check(n2 is n and n.link == story.CONT and n.source_kind == story.SOURCE_LAST and f1.adopted == str(v1b)
      and near(color_at(Path(n.source)), (255, 255, 0)), "7: 最後のコマを選んだら「続き」(次のシーンはそのまま使う)")
check(not fs.next_source_taken(f1, str(v1)), "7: 続きの最後のコマは確かめずに置き換える")
fs.set_source_file(n, str(src))
check(fs.next_source_taken(f1, str(v1b)), "7: 手元の画像が入っていたら確かめる")
check(not fs.next_source_taken(n, str(v1b)), "7: 次のシーンがなければ確かめない")

# 8. ラストフレーム: 手元の画像 (last.png に写す) / 次のシーンの元画像を追いかける
ls = story.Story.create(str(TMP / "ラスト"), "ラスト")
l1 = ls.scenes[0]
check(ls.last_frame_of(l1) == "", "8: 初めはラストフレームなし")
ls.set_last_frame(l1, str(src))
check(l1.last_kind == story.LAST_FILE and Path(l1.last_frame) == ls.scene_dir(l1) / story.LAST_NAME
      and ls.last_frame_of(l1) == l1.last_frame, "8: 手元の画像はシーンのフォルダの last.png に写す")
ls.set_last_frame(l1, follow_next=True)
check(l1.last_kind == story.LAST_NEXT and ls.last_frame_of(l1) == "", "8: 次のシーンがなければ空")
l2 = ls.add_scene(l1, story.CUT)
check(ls.last_frame_of(l1) == "", "8: 次のシーンに元画像がなければ空")
ls.set_source_file(l2, str(src))
check(ls.last_frame_of(l1) == l2.source, "8: 次のシーンの元画像を使う")
ls.set_source_frame(l2, str(v1), 2)
check(ls.last_frame_of(l1) == l2.source and near(color_at(Path(ls.last_frame_of(l1))), (0, 0, 255)),
      "8: 次のシーンの元画像が変わったら、それを追いかける")
frozen = ls.freeze_last_frame(l1, ls.last_frame_of(l1))
check(Path(frozen).parent == ls.scene_dir(l1) and near(color_at(Path(frozen)), (0, 0, 255)),
      "8: 候補を作る時点の絵を写して使う (あとで次のシーンの元画像が変わっても残る)")
ls.save()
again8 = story.Story.load(str(TMP / "ラスト"))
check(again8.scenes[0].last_kind == story.LAST_NEXT and again8.last_frame_of(again8.scenes[0]) == l2.source,
      "8: 保存して開き直しても同じ")
c8 = story.Candidate("x.mp4", last_frame=frozen)
check(story.Candidate.from_dict(c8.to_dict()).last_frame == frozen, "8: 候補に使ったラストフレームを残す")
ls.set_last_frame(l1)
check(l1.last_kind == "" and ls.last_frame_of(l1) == "", "8: 外す")

shutil.rmtree(TMP, ignore_errors=True)
print()
print("失敗: " + ", ".join(failures) if failures else "すべて OK")
sys.exit(1 if failures else 0)
