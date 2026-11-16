"""物語のデータ: シーンを横に並べた絵コンテ。物語 1 つにフォルダ 1 つ。

    (物語のフォルダ)\\story.json          … 物語 (シーンの並び・つなぎ・元画像・プロンプト・候補・採用)
    (物語のフォルダ)\\scenes\\(id)\\       … シーンの元画像 (source.png) と候補
    (物語のフォルダ)\\採用\\01_名前.mp4     … 採用した動画を番号順に揃えたもの (動画編集ソフトに渡す)

つなぎ (link) はそのシーンの前とのつなぎ方:
    CONT (続き)        … 前のシーンで採用した動画の最後のコマを、このシーンの元画像にする
    CUT (シーンチェンジ) … 元画像を選び直す (手元の画像 / T2I で作る / 前のシーンの好きなコマ)
最初のシーンはいつも CUT。シーンのフォルダは id で決まるので、名前を変えたり並べ替えたりしても動かさない。
"""

import json
import re
import shutil
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from gencommon import store

STORY_FILE = "story.json"
SCENES_DIR = "scenes"
ADOPTED_DIR = "採用"
SOURCE_NAME = "source.png"
REFERENCE_NAME = "reference.png"
LAST_NAME = "last.png"  # ラストフレーム (手元の画像を写したもの)
LAST_FILE, LAST_NEXT = "file", "next"  # ラストフレームの決め方: 手元の画像 / 次のシーンの元画像 (追いかける)
RECENT_FILE = "stories.txt"  # 最近開いた物語のフォルダ (1 行に 1 つ、新しい順)
PICK_FOLDER_FILE = "story_pick_folder.txt"  # 前に元画像などを選んだフォルダ (「J-Rate で選ぶ」で開く)
MAX_RECENT = 10
MAX_HISTORY = 60  # シーンごとのプロンプトの履歴 (いちばん最初のものは消さない)
CONT, CUT = "cont", "cut"
LINK_TITLES = {CONT: "続き", CUT: "シーンチェンジ"}
SOURCE_FILE, SOURCE_T2I, SOURCE_FRAME, SOURCE_LAST = "file", "t2i", "frame", "last"
SOURCE_TITLES = {SOURCE_FILE: "手元の画像", SOURCE_T2I: "T2I で作った画像", SOURCE_FRAME: "前のシーンのコマ",
                 SOURCE_LAST: "前のシーンの最後のコマ"}


class StoryError(Exception):
    """物語のフォルダ・動画が使えない。"""


@dataclass
class Candidate:
    path: str
    prompt: str = ""  # 送った文 = そのときの盛ったあとのプロンプト (ベースプロンプトは入れない)
    time: float = 0.0
    original: str = ""  # そのときの元のプロンプト
    reference: str = ""  # そのとき使ったリファレンス画像 (使わなければ空)
    last_frame: str = ""  # そのとき使ったラストフレーム (使わなければ空)

    def to_dict(self) -> dict:
        return {"path": self.path, "prompt": self.prompt, "time": self.time, "original": self.original,
                "reference": self.reference, "last_frame": self.last_frame}

    @staticmethod
    def from_dict(data) -> Optional["Candidate"]:
        if not isinstance(data, dict) or not data.get("path"):
            return None
        try:
            t = float(data.get("time") or 0)
        except (TypeError, ValueError):
            t = 0.0
        return Candidate(str(data["path"]), str(data.get("prompt") or ""), t, str(data.get("original") or ""),
                         str(data.get("reference") or ""), str(data.get("last_frame") or ""))


@dataclass
class Scene:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    name: str = ""
    link: str = CUT  # 前のシーンとのつなぎ方
    source: str = ""  # 元画像 (シーンのフォルダの source.png、または手元の画像のパス)
    source_kind: str = ""  # SOURCE_*
    source_from: str = ""  # 元画像を取った動画 (続き・コマのとき。採用が変わったら作り直すため)
    reference: str = ""  # リファレンス画像 (主に顔。シーンのフォルダの reference.png)。ワークフローが対応するときだけ使う
    # 前のシーンのリファレンスを使う (自分の reference は残すが使わない)。動画を通して同じ顔にするため。
    # 前のシーンも引き継いでいれば、さらに前へたどる。新しく足すシーンは初めから引き継ぐ
    ref_inherit: bool = False
    # ラストフレーム (ファーストフレーム・ラストフレームを指定するワークフローのとき)。
    # last_kind が LAST_FILE なら last_frame (シーンのフォルダの last.png)、LAST_NEXT なら次のシーンの元画像を使う
    last_kind: str = ""
    last_frame: str = ""
    original: str = ""  # 元のプロンプト (自分で書く文。読み取る・盛る・差し込みでは書き換えない)
    prompt: str = ""  # 送るプロンプト (読み取る・差し込み・盛るの結果。候補を作るで送る)
    workflow: str = ""
    seconds: Optional[float] = None
    base_prompt: bool = True
    count: int = 3  # 一度に作る候補の数
    candidates: list = field(default_factory=list)
    adopted: str = ""  # 採用した候補のパス
    # プロンプトの履歴 (古い順): {"time": 時刻, "label": 何の文か, "text": 文, "before": 操作の前の文か}
    # 読み取る・盛る・差し込み・戻すの前とあと、候補に送った文を残す。元のプロンプトがどこかに残るように
    history: list = field(default_factory=list)

    def record(self, label: str, text: str, before: bool = False) -> None:
        """履歴に残す (空・直前と同じ文は残さない)。多くなったら、最初のものを残して古いものから消す。"""
        import time
        if not text or not text.strip():
            return
        if self.history and self.history[-1].get("text") == text:
            if before and not self.history[-1].get("before"):
                self.history[-1]["before"] = True  # 同じ文を「前の文」としても使えるように
            return
        self.history.append({"time": time.time(), "label": label, "text": text, "before": before})
        while len(self.history) > MAX_HISTORY:
            del self.history[1]

    def undo_target(self) -> Optional[str]:
        """「元に戻す」で戻る文: いちばん新しい「操作の前の文」(今の文と違うもの)。"""
        for h in reversed(self.history):
            if h.get("before") and h.get("text") != self.prompt:
                return str(h.get("text"))
        return None

    def to_dict(self) -> dict:
        data = dict(self.__dict__)
        data["candidates"] = [c.to_dict() for c in self.candidates]
        return data

    @staticmethod
    def from_dict(data) -> "Scene":
        s = Scene()
        if not isinstance(data, dict):
            return s
        s.id = str(data.get("id") or s.id)
        for key in ("name", "source", "source_kind", "source_from", "reference", "original", "prompt", "workflow",
                    "adopted", "last_frame"):
            setattr(s, key, str(data.get(key) or ""))
        s.last_kind = str(data.get("last_kind") or "")
        if s.last_kind not in (LAST_FILE, LAST_NEXT):
            s.last_kind, s.last_frame = "", ""
        if "original" not in data and s.prompt and not data.get("history"):
            s.original = s.prompt  # 元のプロンプトの欄がなかったころの物語: 手で書いた文を元のプロンプトに
        s.link = CONT if data.get("link") == CONT else CUT
        sec = data.get("seconds")
        s.seconds = float(sec) if isinstance(sec, (int, float)) and not isinstance(sec, bool) and sec > 0 else None
        s.base_prompt = bool(data.get("base_prompt", True))
        s.ref_inherit = bool(data.get("ref_inherit", False))
        try:
            s.count = max(1, min(9, int(data.get("count") or 3)))
        except (TypeError, ValueError):
            s.count = 3
        cands = data.get("candidates")
        s.candidates = [c for c in (Candidate.from_dict(d) for d in cands) if c] if isinstance(cands, list) else []
        hist = data.get("history")
        s.history = [dict(h) for h in hist if isinstance(h, dict) and isinstance(h.get("text"), str)] \
            if isinstance(hist, list) else []
        return s

    @property
    def send_text(self) -> str:
        """候補を作るで送る文: 送るプロンプト。空なら元のプロンプト。"""
        return (self.prompt.strip() or self.original.strip())

    def label(self, index: int) -> str:
        return f"{index + 1} {self.name}".strip() if self.name else f"{index + 1}"


@dataclass
class Story:
    folder: str
    name: str = ""
    scenes: list = field(default_factory=list)

    # 保存 ------------------------------------------------------------------
    @staticmethod
    def create(folder: str, name: str) -> "Story":
        path = Path(folder)
        if (path / STORY_FILE).exists():
            raise StoryError(f"このフォルダにはもう物語があります ({path})。「開く...」で開いてください。")
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise StoryError(f"フォルダを作れません ({exc})") from exc
        st = Story(str(path), name or path.name, [Scene()])
        st.save()
        return st

    @staticmethod
    def load(folder: str) -> "Story":
        path = Path(folder) / STORY_FILE
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise StoryError(f"物語がありません ({path})") from exc
        except (OSError, ValueError) as exc:
            raise StoryError(f"物語を読めません ({exc})") from exc
        scenes = [Scene.from_dict(d) for d in data.get("scenes") or [] if isinstance(d, dict)]
        st = Story(str(Path(folder)), str(data.get("name") or Path(folder).name), scenes or [Scene()])
        st.scenes[0].link = CUT
        return st

    def save(self) -> None:
        if self.scenes:
            self.scenes[0].link = CUT  # 最初のシーンはいつもシーンチェンジ
        data = {"name": self.name, "scenes": [s.to_dict() for s in self.scenes]}
        path = Path(self.folder) / STORY_FILE
        temp = path.with_name(path.name + ".part")
        try:
            temp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            temp.replace(path)
        except OSError as exc:
            raise StoryError(f"物語を保存できません ({exc})") from exc

    # 場所 ------------------------------------------------------------------
    def scene_dir(self, scene: Scene) -> Path:
        return Path(self.folder) / SCENES_DIR / scene.id

    def adopted_dir(self) -> Path:
        return Path(self.folder) / ADOPTED_DIR

    def index_of(self, scene: Scene) -> int:
        return next(i for i, s in enumerate(self.scenes) if s is scene)

    def previous(self, scene: Scene) -> Optional[Scene]:
        i = self.index_of(scene)
        return self.scenes[i - 1] if i > 0 else None

    # シーン ----------------------------------------------------------------
    def add_scene(self, after: Optional[Scene] = None, link: str = CONT) -> Scene:
        """シーンを足す (after のすぐあと、なければ最後)。最初のシーンでなければ、つなぎの初めは「続き」。"""
        index = len(self.scenes) if after is None else self.index_of(after) + 1
        scene = Scene(link=link if index > 0 else CUT, ref_inherit=index > 0)  # 名前は空 (番号だけで見せる)
        prev = self.scenes[index - 1] if index > 0 else None
        if prev is not None:  # 生成の設定は前のシーンのものを引き継ぐ
            scene.workflow, scene.seconds = prev.workflow, prev.seconds
            scene.base_prompt, scene.count = prev.base_prompt, prev.count
        self.scenes.insert(index, scene)
        self.refresh_links()
        return scene

    def remove_scene(self, scene: Scene) -> None:
        """シーンを外す (候補のファイルはシーンのフォルダに残す)。"""
        self.scenes.remove(scene)
        if not self.scenes:
            self.scenes.append(Scene())
        self.refresh_links()
        self.sync_adopted()

    def move_scene(self, scene: Scene, delta: int) -> None:
        i = self.index_of(scene)
        j = i + delta
        if 0 <= j < len(self.scenes):
            self.scenes[i], self.scenes[j] = self.scenes[j], self.scenes[i]
            self.refresh_links()
            self.sync_adopted()

    def refresh_links(self) -> list[Scene]:
        """「続き」のシーンの元画像を、前のシーンの採用に合わせる (採用が変わった・並びが変わったとき)。
        作り直したシーンを返す。前のシーンに採用がなければ、元画像は空にする。"""
        if self.scenes:
            self.scenes[0].link = CUT
        changed = []
        for i, scene in enumerate(self.scenes):
            if i == 0 or scene.link != CONT:
                continue
            prev = self.scenes[i - 1]
            want = prev.adopted if prev.adopted and Path(prev.adopted).is_file() else ""
            if scene.source_kind == SOURCE_LAST and scene.source_from == want and \
                    (not want or Path(scene.source).is_file()):
                continue
            if want:
                dest = self.scene_dir(scene) / SOURCE_NAME
                save_frame(want, dest, -1)
                scene.source = str(dest)
            else:
                scene.source = ""
            scene.source_kind, scene.source_from = SOURCE_LAST, want
            changed.append(scene)
        return changed

    def set_link(self, scene: Scene, link: str) -> None:
        if self.index_of(scene) == 0:
            return
        if scene.link == link:
            return
        scene.link = link
        if link == CUT and scene.source_kind == SOURCE_LAST:
            scene.source, scene.source_kind, scene.source_from = "", "", ""  # 選び直す
        self.refresh_links()

    def set_source_file(self, scene: Scene, path: str, kind: str = SOURCE_FILE, from_video: str = "") -> None:
        """シーンチェンジの元画像を決める (シーンのフォルダに写して使う。元のファイルが動いても困らないように)。"""
        dest = self.scene_dir(scene) / SOURCE_NAME
        dest.parent.mkdir(parents=True, exist_ok=True)
        if Path(path).resolve() != dest.resolve():
            _write_png(path, dest)
        scene.link = CUT if self.index_of(scene) > 0 else scene.link
        scene.source, scene.source_kind, scene.source_from = str(dest), kind, from_video

    def reference_from(self, scene: Scene) -> Scene:
        """リファレンスを取ってくるシーン (引き継がなければ自分)。引き継ぐシーンは前のシーンへたどる
        (最初のシーンは引き継げない)。"""
        i = self.index_of(scene)
        while i > 0 and self.scenes[i].ref_inherit:
            i -= 1
        return self.scenes[i]

    def reference_of(self, scene: Scene) -> str:
        """候補を作るときに使うリファレンスのパス (なければ空)。"""
        ref = self.reference_from(scene).reference
        return ref if ref and Path(ref).is_file() else ""

    def set_reference(self, scene: Scene, path: str) -> None:
        """リファレンス画像 (主に顔) を決める (シーンのフォルダに PNG で写す)。空なら外す。
        どちらでも、このシーンは前のシーンから引き継ぐのをやめる (自分で決めたので)。"""
        scene.ref_inherit = False
        if not path:
            scene.reference = ""
            return
        dest = self.scene_dir(scene) / REFERENCE_NAME
        dest.parent.mkdir(parents=True, exist_ok=True)
        if Path(path).resolve() != dest.resolve():
            _write_png(path, dest)
        scene.reference = str(dest)

    def set_last_frame(self, scene: Scene, path: str = "", follow_next: bool = False) -> None:
        """ラストフレームを決める。path なら手元の画像 (シーンのフォルダの last.png に写す)、
        follow_next なら次のシーンの元画像を使う (次のシーンの元画像が変わったら、それを使う)。どちらもなければ外す。"""
        if follow_next:
            scene.last_kind, scene.last_frame = LAST_NEXT, ""
            return
        if not path:
            scene.last_kind, scene.last_frame = "", ""
            return
        dest = self.scene_dir(scene) / LAST_NAME
        dest.parent.mkdir(parents=True, exist_ok=True)
        if Path(path).resolve() != dest.resolve():
            _write_png(path, dest)
        scene.last_kind, scene.last_frame = LAST_FILE, str(dest)

    def last_frame_of(self, scene: Scene) -> str:
        """候補を作るときに使うラストフレームのパス (なければ空)。次のシーンの元画像を追いかけるときは、その今のもの。"""
        if scene.last_kind == LAST_FILE:
            return scene.last_frame if scene.last_frame and Path(scene.last_frame).is_file() else ""
        if scene.last_kind == LAST_NEXT:
            i = self.index_of(scene)
            nxt = self.scenes[i + 1] if i + 1 < len(self.scenes) else None
            if nxt is not None and nxt.source and Path(nxt.source).is_file():
                return nxt.source
        return ""

    def freeze_last_frame(self, scene: Scene, path: str) -> str:
        """候補を作る時点のラストフレームを、シーンのフォルダの last_(時刻).png に写して、そのパスを返す
        (次のシーンの元画像を追いかけるとき。元の source.png はあとで書き直されることがあるため)。"""
        import time
        dest = self.scene_dir(scene) / f"last_{time.strftime('%Y%m%d_%H%M%S')}.png"
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(path, dest)
        except OSError as exc:
            raise StoryError(f"ラストフレームを写せません ({exc})") from exc
        return str(dest)

    def set_source_frame(self, scene: Scene, video: str, frame: int) -> None:
        """前のシーンの好きなコマを元画像にする。"""
        dest = self.scene_dir(scene) / SOURCE_NAME
        save_frame(video, dest, frame)
        scene.link = CUT if self.index_of(scene) > 0 else scene.link
        scene.source, scene.source_kind, scene.source_from = str(dest), SOURCE_FRAME, video

    def next_source_taken(self, scene: Scene, video: str) -> bool:
        """次のシーンに、自分で選んだ元画像 (手元の画像・T2I・ほかの動画のコマ) がもう入っているか。
        「コマを選んで次へ」で置き換える前に確かめるため。最後のコマ・同じ動画のコマなら置き換えてよい。"""
        i = self.index_of(scene)
        if i + 1 >= len(self.scenes):
            return False
        nxt = self.scenes[i + 1]
        if not nxt.source or nxt.source_kind == SOURCE_LAST:
            return False
        return not (nxt.source_kind == SOURCE_FRAME and nxt.source_from == video)

    def start_next_from_frame(self, scene: Scene, video: str, frame: int, last: bool = False) -> Scene:
        """候補の好きなコマから次のシーンを始める: その候補を採用し、次のシーン (なければ足す) の元画像をそのコマにする。
        last (最後のコマを選んだ) なら「続き」と同じなので、つなぎを「続き」にする (採用が変わったら作り直す)。
        ほかのコマは「シーンチェンジ」の「前のシーンのコマ」(あとで採用を変えても、選んだコマのまま)。次のシーンを返す。"""
        i = self.index_of(scene)
        nxt = self.scenes[i + 1] if i + 1 < len(self.scenes) else self.add_scene(scene, CONT if last else CUT)
        if last:
            if nxt.link != CONT:
                nxt.link = CONT
                nxt.source_kind = ""  # 作り直させる
        else:
            self.set_source_frame(nxt, video, frame)
        if scene.adopted != video:
            scene.adopted = video
            self.sync_adopted()
        self.refresh_links()  # 続きなら、ここで最後のコマを作る
        return nxt

    def adopt(self, scene: Scene, path: str) -> list[Scene]:
        """候補を採用する (同じものをもう一度押すと採用をやめる)。続きのシーンの元画像を作り直し、採用のフォルダを揃える。"""
        scene.adopted = "" if scene.adopted == path else path
        changed = self.refresh_links()
        self.sync_adopted()
        return changed

    def remove_candidates(self, scene: Scene, paths: list) -> None:
        """候補から外す (ファイルを消したあとなど)。採用していたものなら採用をやめ、続きの元画像と採用のフォルダを揃える。"""
        gone = {str(p) for p in paths}
        scene.candidates = [c for c in scene.candidates if c.path not in gone]
        if scene.adopted in gone:
            scene.adopted = ""
            self.refresh_links()
            self.sync_adopted()

    # 採用のフォルダ --------------------------------------------------------
    def adopted_files(self) -> list[tuple[Scene, str]]:
        return [(s, s.adopted) for s in self.scenes if s.adopted and Path(s.adopted).is_file()]

    def numbered_name(self, index: int, scene: Scene, path: str) -> str:
        name = safe(scene.name) or "scene"
        return f"{index + 1:02}_{name}{Path(path).suffix.lower()}"

    def sync_adopted(self) -> list[Path]:
        """採用した動画を (物語)\\採用\\01_名前.mp4 の番号順に揃える。自分が作った番号付きのファイルは作り直す
        (手で置いたほかのファイルは消さない)。戻り値は揃えたファイル。"""
        folder = self.adopted_dir()
        if not folder.exists() and not self.adopted_files():
            return []
        folder.mkdir(parents=True, exist_ok=True)
        for old in folder.iterdir():
            if old.is_file() and re.match(r"^\d{2,}_", old.name):
                try:
                    old.unlink()
                except OSError:
                    pass
        result = []
        for i, scene in enumerate(self.scenes):
            if scene.adopted and Path(scene.adopted).is_file():
                dest = folder / self.numbered_name(i, scene, scene.adopted)
                try:
                    shutil.copy2(scene.adopted, dest)
                    result.append(dest)
                except OSError:
                    pass
        return result


def safe(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|\s]+', "_", name.strip()).strip("_.")[:40]


# ----------------------------------------------------------------------
# コマ (OpenCV)
# ----------------------------------------------------------------------
def frame_count(video: str) -> int:
    import cv2
    cap = cv2.VideoCapture(str(video))
    try:
        if not cap.isOpened():
            raise StoryError(f"動画を開けません ({Path(video).name})")
        return max(0, int(cap.get(cv2.CAP_PROP_FRAME_COUNT)))
    finally:
        cap.release()


def read_frame(video: str, index: int):
    """index 番目のコマ (BGR の配列)。-1 は最後のコマ。
    最後のコマは、数えたコマ数が当てにならない動画があるので、終わりまで読んで最後に読めたものを使う。"""
    import cv2
    cap = cv2.VideoCapture(str(video))
    try:
        if not cap.isOpened():
            raise StoryError(f"動画を開けません ({Path(video).name})")
        frame = None
        if index < 0:
            while True:
                ok, img = cap.read()
                if not ok:
                    break
                frame = img
        else:
            cap.set(cv2.CAP_PROP_POS_FRAMES, index)
            ok, img = cap.read()
            frame = img if ok else None
        if frame is None:
            raise StoryError(f"動画からコマを読めません ({Path(video).name})")
        return frame
    finally:
        cap.release()


def save_frame(video: str, dest: Path, index: int) -> None:
    import cv2
    frame = read_frame(video, index)
    dest.parent.mkdir(parents=True, exist_ok=True)
    ok, buf = cv2.imencode(".png", frame)  # imwrite は日本語のパスに書けないので、自分で書く
    if not ok:
        raise StoryError("コマを画像にできません")
    try:
        dest.write_bytes(buf.tobytes())
    except OSError as exc:
        raise StoryError(f"コマを書けません ({exc})") from exc


def frame_to_qimage(frame):
    from PySide6.QtGui import QImage
    import cv2
    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    h, w, _ = rgb.shape
    return QImage(rgb.data, w, h, 3 * w, QImage.Format.Format_RGB888).copy()


def _write_png(src: str, dest: Path) -> None:
    """画像を PNG にして写す (形式をそろえる)。読めなければ StoryError。"""
    from PySide6.QtGui import QImageReader
    reader = QImageReader(str(src))
    reader.setAutoTransform(True)
    img = reader.read()
    if img.isNull():
        raise StoryError(f"画像を読めません ({Path(src).name})")
    if not img.save(str(dest), "PNG"):
        raise StoryError(f"元画像を書けません ({dest})")


# ----------------------------------------------------------------------
# 最近開いた物語
# ----------------------------------------------------------------------
def recent() -> list[str]:
    text = store.read_text(store.app_file(RECENT_FILE)) or ""
    return [line.strip() for line in text.splitlines() if line.strip() and (Path(line.strip()) / STORY_FILE).exists()]


def pick_folder() -> str:
    """前に元画像・リファレンス・ラストフレームを選んだフォルダ (なければ空)。"""
    folder = (store.read_text(store.app_file(PICK_FOLDER_FILE)) or "").strip()
    return folder if folder and Path(folder).is_dir() else ""


def remember_pick(path: str) -> None:
    """選んだ画像のフォルダを覚える (物語のフォルダの中のもの = 候補・コマは覚えない)。"""
    folder = Path(path).parent
    if any((d / STORY_FILE).exists() for d in [folder, *folder.parents][:4]):
        return
    try:
        store.write_text(store.app_file(PICK_FOLDER_FILE), str(folder))
    except OSError:
        pass


def remember(folder: str) -> None:
    items = [folder] + [f for f in recent() if Path(f) != Path(folder)]
    try:
        store.write_text(store.app_file(RECENT_FILE), "\r\n".join(items[:MAX_RECENT]) + "\r\n")
    except OSError:
        pass
