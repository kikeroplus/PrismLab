"""物語のバッチ (プロトタイプ): 最初の画像とカットごとのプロンプトから、物語を何本かまとめて作る。

1 本ごとに新しい物語のフォルダを作り、プロンプトの数だけシーンを並べる (2 つ目からは「続き」・顔は前と同じ)。
シーン 1 から順に、(英語に / 盛る) → 候補を 1 つ作る → 自動で採用 → その最後のコマが次のシーンの元画像、
を最後まで繰り返す。失敗したらその物語はそこで止め、次の本へ進む。出来た物語は普通の物語 (手で直せる)。

バッチは「待ち行列」に足す (BatchQueue)。物語 1 本は順にしか作れない (次のシーンは前のシーンの最後のコマから) ので、
別々の物語 (別の件・同じ件の何本目) を、台の数 (最大 MAX_PARALLEL 本) まで並べて作る (StoryWorker。2026-11-17 案 A)。
物語 1 本ごとに自分の生成の窓口を持ち、画面 (物語のタブ) は使わない。台の空きは量産ラインと分け合う (engine.POOL。物語が先)。
動いているあいだも足せる (物語のタブ・J-Rate・PromptWordCrafter・量産ラインの結果から)。
待ち行列はファイルに残し、落ちても次の起動で続きから始める (作りかけの物語は、まだ採用していないシーンから)。
"""

import json
import shutil
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                               QDoubleSpinBox, QFileDialog, QFormLayout, QHBoxLayout, QHeaderView, QLabel,
                               QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPlainTextEdit,
                               QPushButton, QSpinBox, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from gencommon import comfy, enhancer, store

from . import engine, kinds, recipes, story, theme, translate

PLAN_FILE = "story_batch.json"  # 前に使ったバッチの設定 (窓を開いたときに入れる)
QUEUE_FILE = "story_batch_queue.json"  # 待ち行列 (落ちても続きから始める)
MAX_RUNS = 20
TICK_MS = 2000  # 空いた枠に次の物語を入れられるか見る間隔
MAX_PARALLEL = 3  # 同時に作る物語の本数の上限 (実際は台の数まで)

WAITING, RUNNING, DONE, STOPPED = "waiting", "running", "done", "stopped"
STATE_LABELS = {WAITING: "待機中", RUNNING: "実行中", DONE: "完了", STOPPED: "止めた"}


@dataclass
class Plan:
    name: str = ""
    parent: str = ""  # 物語のフォルダを作る場所
    image: str = ""  # 最初の画像 (シーン 1 の元画像)
    reference: str = ""  # リファレンス (顔)。空なら使わない
    prompts: str = ""  # 「/」か改行で区切ったカットごとの元のプロンプト
    workflow: str = ""
    seconds: Optional[float] = None
    base_prompt: bool = True
    english: bool = False  # 元のプロンプトを英語に直してから送る
    enhance: bool = False  # 盛ってから送る
    enhancer: str = ""  # エンハンサーのプリセットの名前
    runs: int = 1  # 物語を何本作るか
    cuts: list = field(default_factory=list)  # 区切ったプロンプト (始めるときに入れる)

    def to_json(self) -> str:
        data = asdict(self)
        data.pop("cuts", None)
        return json.dumps(data, ensure_ascii=False, indent=1)

    @staticmethod
    def from_dict(data: dict) -> "Plan":
        plan = Plan()
        if isinstance(data, dict):
            for key, value in data.items():
                if key in plan.__dataclass_fields__:
                    setattr(plan, key, value)
        return plan

    @staticmethod
    def load() -> "Plan":
        try:
            data = json.loads(store.read_text(store.app_file(PLAN_FILE)) or "{}")
        except ValueError:
            data = {}
        plan = Plan.from_dict(data if isinstance(data, dict) else {})
        plan.cuts = []
        return plan

    def save(self) -> None:
        try:
            store.write_text(store.app_file(PLAN_FILE), self.to_json())
        except OSError:
            pass


def split_prompts(text: str) -> list[str]:
    """「/」か改行で区切る (空のものは捨てる)。"""
    parts = []
    for line in (text or "").replace("／", "/").splitlines():
        parts.extend(p.strip() for p in line.split("/"))
    return [p for p in parts if p]


def story_folder(parent: str, name: str, index: int, runs: int) -> Path:
    """その本の物語のフォルダ (1 本なら名前のまま、何本もなら 名前_01…)。あれば (2) などを付ける。"""
    base = story.safe(name) or "batch"
    if runs > 1:
        base = f"{base}_{index + 1:02}"
    folder = Path(parent) / base
    n = 2
    while folder.exists():
        folder = Path(parent) / f"{base} ({n})"
        n += 1
    return folder


def create_story(plan: Plan, folder: Path, title: str) -> story.Story:
    """物語を作り、カットの数だけシーンを並べる (1 つ目はシーンチェンジ・最初の画像、2 つ目からは続き)。
    最初の画像・リファレンスを写せなければ、作りかけのフォルダを消して StoryError (空の物語を残さない)。"""
    st = story.Story.create(str(folder), title)
    first = st.scenes[0]
    try:
        st.set_source_file(first, plan.image)
        if plan.reference:
            st.set_reference(first, plan.reference)
    except (story.StoryError, OSError) as exc:
        shutil.rmtree(folder, ignore_errors=True)  # story_folder で決めた新しいフォルダなので、丸ごと消してよい
        raise story.StoryError(str(exc)) from exc
    first.workflow, first.seconds, first.base_prompt, first.count = plan.workflow, plan.seconds, plan.base_prompt, 1
    first.original = plan.cuts[0]
    prev = first
    for text in plan.cuts[1:]:
        scene = st.add_scene(prev, story.CONT)  # 設定は前のシーンから・顔は前と同じ
        scene.original = text
        prev = scene
    st.save()
    return st


def video_workflows() -> list[tuple[str, str]]:
    """バッチで使えるワークフロー (表示, パス): 画像から動画 (I2V)。ラストフレームを指定するものは除く。"""
    out = []
    for i, p in enumerate(comfy.workflows()):
        if p.is_empty:
            continue
        kind = kinds.kind_of(p.value)
        if kind == "I2V" and not kinds.has_last_frame(p.value):
            out.append((p.file_label(i) + ("   [顔]" if kinds.has_reference(p.value) else ""), p.value))
    return out


# ----------------------------------------------------------------------
# 待ち行列の 1 件
# ----------------------------------------------------------------------
@dataclass
class Entry:
    plan: Plan
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    state: str = WAITING
    run_index: int = 0  # 始めた本の数 (作りかけも含む。次に新しく作るのは run_index 本目)
    folders: list = field(default_factory=list)  # 作りかけの物語 (一時停止・落ちたあとは、ここから続きを作る)
    made: list = field(default_factory=list)  # 最後まで出来た物語の名前
    failed: list = field(default_factory=list)  # 途中で止まった物語 (名前: 理由)
    added: float = field(default_factory=time.time)
    ended: float = 0.0

    @property
    def unfinished(self) -> bool:
        return self.state in (WAITING, RUNNING)

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(data: dict) -> Optional["Entry"]:
        if not isinstance(data, dict) or not isinstance(data.get("plan"), dict):
            return None
        entry = Entry(Plan.from_dict(data["plan"]))
        for key in ("id", "state", "run_index", "folders", "made", "failed", "added", "ended"):
            if key in data:
                setattr(entry, key, data[key])
        if data.get("folder") and not data.get("folders"):  # 2026-11-17 までの形: 作りかけは 1 本、run_index はその本
            entry.folders = [str(data["folder"])]
            entry.run_index = int(entry.run_index or 0) + 1
        entry.folders = [str(f) for f in entry.folders if f]
        if not entry.plan.cuts:
            entry.plan.cuts = split_prompts(entry.plan.prompts)
        return entry

    def summary(self) -> str:
        line = f"{self.plan.name}: 出来た物語 {len(self.made)} / {self.plan.runs} 本"
        if self.failed:
            line += " (途中で止まったもの: " + " / ".join(self.failed) + ")"
        return line


def load_queue() -> tuple[list[Entry], bool]:
    try:
        data = json.loads(store.read_text(store.app_file(QUEUE_FILE)) or "{}")
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        return [], False
    entries = [e for e in (Entry.from_dict(d) for d in data.get("entries") or []) if e is not None]
    return entries, bool(data.get("paused"))


def save_queue(entries: list[Entry], paused: bool) -> None:
    data = {"paused": paused, "entries": [e.to_dict() for e in entries]}
    try:
        store.write_text(store.app_file(QUEUE_FILE), json.dumps(data, ensure_ascii=False, indent=1))
    except OSError:
        pass


# ----------------------------------------------------------------------
# 設定の窓
# ----------------------------------------------------------------------
class BatchDialog(QDialog):
    """バッチの設定。最初の画像は何枚でも並べられ、1 枚ごとに 1 件のバッチになる (ほかの設定は同じ)。"""

    def __init__(self, plan: Plan, parent=None, images: Optional[list] = None):
        super().__init__(parent)
        self.setWindowTitle("物語のバッチを待ち行列に足す (試し)")
        self.setMinimumWidth(640)
        self.plan = plan
        self.plans: list[Plan] = []  # 足すもの (OK のあと)
        root = QVBoxLayout(self)
        note = QLabel("最初の画像とカットごとのプロンプトから、物語をまとめて作ります。各シーンの候補は 1 つで、"
                      "自動で採用して次のシーンへ続けます (出来は運まかせ。あとで物語のタブで手直しできます)。"
                      "待ち行列に足すと、前のものが終わってから順に作ります。")
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {theme.TEXT_DIM};")
        root.addWidget(note)
        form = QFormLayout()
        root.addLayout(form)

        self.name = QLineEdit(plan.name)
        form.addRow("物語の名前", self.name)
        self.parent_edit = QLineEdit(plan.parent)
        form.addRow("作る場所", self._with_button(self.parent_edit, "選ぶ...", self._choose_parent))

        self.images = QListWidget()
        self.images.setFixedHeight(96)
        self.images.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.images.setToolTip("1 枚ごとに 1 件のバッチになります (名前の後ろに画像の名前を付けます)。"
                               "J-Rate などから「物語のバッチに足す」で送った画像もここに入ります。")
        image_host = QWidget()
        image_row = QHBoxLayout(image_host)
        image_row.setContentsMargins(0, 0, 0, 0)
        image_row.addWidget(self.images, 1)
        image_buttons = QVBoxLayout()
        add = QPushButton("足す...")
        add.clicked.connect(self._choose_images)
        remove = QPushButton("外す")
        remove.clicked.connect(self._remove_images)
        image_buttons.addWidget(add)
        image_buttons.addWidget(remove)
        image_buttons.addStretch(1)
        image_row.addLayout(image_buttons)
        form.addRow("最初の画像", image_host)
        for path in (images if images else [plan.image] if plan.image else []):
            self._add_image(path)

        self.reference = QLineEdit(plan.reference)
        self.reference.setPlaceholderText("なし (顔を指定するワークフローのときだけ使います)")
        form.addRow("リファレンス (顔)", self._with_button(self.reference, "選ぶ...",
                                                         lambda: self._choose_image(self.reference)))
        self.prompts = QPlainTextEdit(plan.prompts)
        self.prompts.setPlaceholderText("歩いてくる / 振り返って笑う / 走り去る\n(「/」か改行で区切ると、その数がカット数)")
        self.prompts.setFixedHeight(110)
        self.prompts.textChanged.connect(self._update)
        form.addRow("プロンプト", self.prompts)
        self.cut_count = QLabel()
        form.addRow("", self.cut_count)

        self.workflow = QComboBox()
        for label, path in video_workflows():
            self.workflow.addItem(label, path)
        index = next((i for i in range(self.workflow.count())
                      if str(self.workflow.itemData(i)).lower() == plan.workflow.lower()), 0)
        self.workflow.setCurrentIndex(index)
        form.addRow("ワークフロー", self.workflow)
        row = QHBoxLayout()
        self.use_seconds = QCheckBox("長さ")
        self.seconds = QDoubleSpinBox()
        self.seconds.setRange(1, 60)
        self.seconds.setSuffix(" 秒")
        self.use_seconds.setChecked(plan.seconds is not None)
        self.seconds.setValue(float(plan.seconds or 5))
        self.seconds.setEnabled(self.use_seconds.isChecked())
        self.use_seconds.toggled.connect(self.seconds.setEnabled)
        self.base = QCheckBox("ベースプロンプト")
        self.base.setChecked(plan.base_prompt)
        row.addWidget(self.use_seconds)
        row.addWidget(self.seconds)
        row.addSpacing(16)
        row.addWidget(self.base)
        row.addStretch(1)
        form.addRow("", row)
        row2 = QHBoxLayout()
        self.english = QCheckBox("英語に")
        self.english.setChecked(plan.english)
        self.english.setToolTip("元のプロンプトを LLM で英語に直してから送ります。")
        self.enhance = QCheckBox("盛る")
        self.enhance.setChecked(plan.enhance)
        self.enhance.setToolTip("LLM で盛ってから送ります (英語にもするなら、英語にしてから盛ります)。")
        self.enhancer = QComboBox()
        for i, p in enumerate(enhancer.load_presets()):
            if not p.is_empty:
                self.enhancer.addItem(p.label(i), p.name)
        self.enhancer.setCurrentIndex(max(self.enhancer.findData(plan.enhancer), 0))
        self.enhance.toggled.connect(self.enhancer.setEnabled)
        self.enhancer.setEnabled(plan.enhance)
        row2.addWidget(self.english)
        row2.addSpacing(16)
        row2.addWidget(self.enhance)
        row2.addWidget(self.enhancer, 1)
        form.addRow("送る前に", row2)
        self.runs = QSpinBox()
        self.runs.setRange(1, MAX_RUNS)
        self.runs.setValue(max(1, min(MAX_RUNS, int(plan.runs or 1))))
        self.runs.setSuffix(" 本")
        self.runs.valueChanged.connect(self._update)
        form.addRow("本数", self.runs)

        buttons = QDialogButtonBox()
        self.start = buttons.addButton("待ち行列に足す", QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton("やめる", QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)
        self._update()

    @staticmethod
    def _with_button(edit: QLineEdit, text: str, func) -> QWidget:
        host = QWidget()
        row = QHBoxLayout(host)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(edit, 1)
        button = QPushButton(text)
        button.clicked.connect(func)
        row.addWidget(button)
        return host

    # 最初の画像 -------------------------------------------------------------
    def image_paths(self) -> list[str]:
        return [str(self.images.item(i).data(Qt.ItemDataRole.UserRole)) for i in range(self.images.count())]

    def _add_image(self, path: str) -> None:
        path = str(path)
        if not path or any(p.lower() == path.lower() for p in self.image_paths()):
            return
        item = QListWidgetItem(Path(path).name)
        item.setData(Qt.ItemDataRole.UserRole, path)
        item.setToolTip(path)
        self.images.addItem(item)

    def add_images(self, paths: list) -> None:
        """送られた画像を足す (窓を開いているあいだに J-Rate などから送ったとき)。"""
        for p in paths:
            self._add_image(p)
        self._update()
        self.raise_()
        self.activateWindow()

    def _choose_images(self) -> None:
        exts = " ".join("*" + e for e in recipes.IMAGE_EXTS)
        paths = self.image_paths()
        start = str(Path(paths[-1]).parent) if paths else story.pick_folder()
        chosen, _ = QFileDialog.getOpenFileNames(self, "最初の画像を選ぶ (何枚でも)", start, f"画像 ({exts})")
        if chosen:
            self.add_images(chosen)
            story.remember_pick(chosen[-1])

    def _remove_images(self) -> None:
        for item in self.images.selectedItems():
            self.images.takeItem(self.images.row(item))
        self._update()

    def _choose_parent(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "物語のフォルダを作る場所を選ぶ", self.parent_edit.text())
        if folder:
            self.parent_edit.setText(folder)

    def _choose_image(self, edit: QLineEdit) -> None:
        exts = " ".join("*" + e for e in recipes.IMAGE_EXTS)
        start = edit.text() or story.pick_folder()
        path, _ = QFileDialog.getOpenFileName(self, "画像を選ぶ", start, f"画像 ({exts})")
        if path:
            edit.setText(path)
            story.remember_pick(path)

    def _update(self) -> None:
        n = len(split_prompts(self.prompts.toPlainText()))
        runs = self.runs.value()
        images = max(1, self.images.count())
        text = f"カット数 {n}" + (f" × {runs} 本" if runs > 1 else "")
        if images > 1:
            text += f" × 画像 {images} 枚 (バッチ {images} 件)"
        if runs > 1 or images > 1:
            text += f" = 候補 {n * runs * images} 本"
        self.cut_count.setText(text)

    def _accept(self) -> None:
        p = self.plan
        p.name = self.name.text().strip()
        p.parent = self.parent_edit.text().strip()
        images = self.image_paths()
        p.image = images[0] if images else ""
        p.reference = self.reference.text().strip()
        p.prompts = self.prompts.toPlainText()
        p.workflow = str(self.workflow.currentData() or "")
        p.seconds = self.seconds.value() if self.use_seconds.isChecked() else None
        p.base_prompt = self.base.isChecked()
        p.english, p.enhance = self.english.isChecked(), self.enhance.isChecked()
        p.enhancer = str(self.enhancer.currentData() or "")
        p.runs = self.runs.value()
        p.cuts = split_prompts(p.prompts)
        plans = []
        for image in images or [""]:
            one = Plan.from_dict(asdict(p))
            one.image = image
            if len(images) > 1:
                one.name = f"{p.name}_{Path(image).stem}" if p.name else ""
            problem = check_plan(one)
            if problem:
                QMessageBox.information(self, "バッチ", (f"{Path(image).name}: " if len(images) > 1 else "") + problem)
                return
            plans.append(one)
        p.save()
        self.plans = plans
        self.accept()


def check_plan(p: Plan) -> str:
    """始められないときの理由 (なければ空)。"""
    if not p.name:
        return "物語の名前を入れてください。"
    if not p.parent or not Path(p.parent).is_dir():
        return "物語のフォルダを作る場所を選んでください。"
    if not p.image or not Path(p.image).is_file():
        return "最初の画像を選んでください。"
    if p.reference and not Path(p.reference).is_file():
        return "リファレンス (顔) の画像が見つかりません。"
    if not p.cuts:
        return "プロンプトを入れてください (「/」か改行で区切ります)。"
    if not p.workflow:
        return "画像から動画 (I2V) のワークフローがありません (「生成の設定」で登録します)。"
    if p.enhance and not any(x.name == p.enhancer for x in enhancer.load_presets() if not x.is_empty):
        return "エンハンサーが登録されていません (「エンハンサーの設定」)。"
    return ""


# ----------------------------------------------------------------------
# 物語 1 本を作る役 (画面に出さずに裏で進める。何本も並べて動かす)
# ----------------------------------------------------------------------
MADE, FAILED, HALTED, STOPPED_ONE = "made", "failed", "halted", "stopped"


class StoryWorker(QObject):
    """物語 1 本のシーンを順に作る。自分の生成の窓口 (Engine) を持ち、台の空きは量産ラインと分け合う (物語が先)。
    ended(自分, 結果, 理由): 結果は MADE / FAILED / HALTED (一時停止・閉じる。作りかけは残す) / STOPPED_ONE。"""

    ended = Signal(object, str, str)
    changed = Signal()  # 進んだ (帯・待ち行列の窓を描き直す)
    _llm = Signal(int, str, str)  # (頼んだ番号, 文, 失敗の理由)

    def __init__(self, parent, should_pause: Callable[[], bool]):
        super().__init__(parent)
        self.engine = engine.Engine(self, run_file=None, first=True)
        self.engine.finished.connect(self._on_finished)
        self.should_pause = should_pause
        self.entry: Optional[Entry] = None
        self.plan: Optional[Plan] = None
        self.story: Optional[story.Story] = None
        self.folder = ""
        self.scene_index = 0
        self.status = ""
        self.active = False
        self._waiting = False  # 候補の出来上がりを待っている
        self._ticket = 0  # LLM の頼みの番号 (止めたあとに届いたものは捨てる)
        self._job = ("", "", "")  # (送った文, 元のプロンプト, リファレンス)
        self._llm.connect(self._on_llm)

    @property
    def winding_down(self) -> bool:
        """止めた候補づくりが、まだ片付いていない (この役はまだ次に使えない)。"""
        run = self.engine.run
        return self.engine.active and run is not None and run.unfinished > 0

    # 始める・止める ---------------------------------------------------------
    def start(self, entry: Entry, folder: str, index: int) -> None:
        """folder があればその作りかけの物語の続きから、なければ index 本目の物語を新しく作る。"""
        self.entry, self.plan, self.active = entry, entry.plan, True
        self.story, self.folder, self.status = None, folder, "始めています…"
        p = self.plan
        if folder:
            try:
                self.story = story.Story.load(folder)
            except story.StoryError as exc:
                self._end(FAILED, str(exc))
                return
        else:
            title = p.name if p.runs == 1 else f"{p.name} {index + 1:02}"
            target = story_folder(p.parent, p.name, index, p.runs)
            try:
                self.story = create_story(p, target, title)
            except story.StoryError as exc:
                self.folder = str(target)
                self._end(FAILED, str(exc))
                return
            self.folder = str(target)
            entry.folders.append(self.folder)  # 作りかけ (落ちても続きから作れるように、すぐ書く)
        st = self.story
        self.scene_index = next((i for i, s in enumerate(st.scenes)
                                 if not (s.adopted and Path(s.adopted).is_file())), len(st.scenes))
        self.changed.emit()
        QTimer.singleShot(0, self._next_scene)

    def _cancel(self) -> None:
        self._ticket += 1
        if self._waiting and self.engine.active:
            self.engine.cancel_all()  # 出来上がり (取り消し) は _on_finished で受けるが、_waiting でないので何もしない
        self._waiting = False

    def halt(self) -> None:
        """一時停止・閉じるとき: 今の候補はやめ、作りかけの物語は残す (次はまだ採用していないシーンから)。"""
        if self.active:
            self._cancel()
            self._end(HALTED)

    def stop(self) -> None:
        """この物語をやめる (途中で止まったものにする)。"""
        if self.active:
            self._cancel()
            self._end(STOPPED_ONE, "止めました")

    def _end(self, outcome: str, reason: str = "") -> None:
        self.active = False
        self.status = ""
        self.ended.emit(self, outcome, reason)

    @property
    def name(self) -> str:
        return self.story.name if self.story is not None else (Path(self.folder).name if self.folder else "")

    def _save(self) -> bool:
        try:
            self.story.save()
            return True
        except story.StoryError as exc:
            self._end(FAILED, str(exc))
            return False

    def _set_status(self, what: str) -> None:
        n = len(self.story.scenes) if self.story is not None else 0
        self.status = f"{self.name} · シーン {self.scene_index + 1} / {n} · {what}"
        self.changed.emit()

    # シーン ----------------------------------------------------------------
    def _next_scene(self) -> None:
        if not self.active:
            return
        st = self.story
        if self.scene_index >= len(st.scenes):
            self._end(MADE)
            return
        if self.should_pause():
            self.halt()
            return
        scene = st.scenes[self.scene_index]
        if not scene.source or not Path(scene.source).is_file():
            self._end(FAILED, f"シーン {self.scene_index + 1} の元画像がありません (前のシーンの最後のコマが取れません)")
            return
        p = self.plan
        if p.english or p.enhance:
            self._ticket += 1
            ticket, text = self._ticket, scene.original
            snap_en = translate.snapshot() if p.english else None
            found = [x for x in enhancer.load_presets() if not x.is_empty and x.name == p.enhancer]
            snap_enh = enhancer.snapshot(found[0]) if p.enhance and found else None
            self._set_status("英語に直しています…" if p.english else "盛っています…")

            def work():
                try:
                    out = text
                    if snap_en is not None:
                        out = translate.to_english(snap_en, out)
                    if snap_enh is not None:
                        out = enhancer.enhance_with(snap_enh, out)
                    self._llm.emit(ticket, out.strip(), "")
                except Exception as exc:  # noqa: BLE001 - LLM の失敗は理由を出す
                    self._llm.emit(ticket, "", str(exc) or exc.__class__.__name__)

            threading.Thread(target=work, daemon=True).start()
            return
        self._make(scene)

    def _on_llm(self, ticket: int, text: str, error: str) -> None:
        if ticket != self._ticket or not self.active:
            return
        if error or not text:
            self._end(FAILED, f"シーン {self.scene_index + 1} の LLM に失敗: {error or '返事が空'}")
            return
        scene = self.story.scenes[self.scene_index]
        scene.prompt = text
        scene.record("バッチで送る文 (英語に / 盛る)", text)
        if self._save():
            self._make(scene)

    def _make(self, scene: story.Scene) -> None:
        """候補を 1 つ作り始める (物語のタブの「候補を作る」と同じ中身。画面は使わない)。"""
        st = self.story
        if kinds.has_last_frame(scene.workflow):
            self._end(FAILED, "ラストフレームを指定するワークフローはバッチでは使えません")
            return
        machines = recipes.machines()
        if not machines:
            self._end(FAILED, recipes.no_machines_reason())
            return
        folder = st.scene_dir(scene)
        folder.mkdir(parents=True, exist_ok=True)
        reference = st.reference_of(scene) if kinds.has_reference(scene.workflow) else ""
        recipe = recipes.Recipe(name=st.name, output_dir=str(folder), inherit=False,
                                stages=[recipes.Stage(recipes.GEN, workflow=scene.workflow, seconds=scene.seconds,
                                                      base_prompt=scene.base_prompt)])
        try:
            run = engine.Run.create(recipe, [Path(scene.source)])
        except (ValueError, comfy.ComfyError) as exc:
            self._end(FAILED, f"シーン {self.scene_index + 1}: {exc}")
            return
        text = scene.send_text
        for it in run.items:
            it.text = text or None  # 空ならワークフローに書かれたプロンプトのまま
            it.reference = reference
        self._job = (text, scene.original, reference)
        scene.record("候補に送った文", text)
        if not self._save():
            return
        self._set_status("候補を作っています…")
        self.engine.set_machines(machines)
        try:
            self.engine.start(run)
        except RuntimeError as exc:
            self._end(FAILED, str(exc))
            return
        self._waiting = True

    def _on_finished(self) -> None:
        if not self.active or not self._waiting:
            return
        self._waiting = False
        st = self.story
        scene = st.scenes[self.scene_index]
        _run, items, _m = self.engine.snapshot()
        prompt, original, reference = self._job
        known = {c.path for c in scene.candidates}
        new = []
        for it in items:
            for o in it.outputs:
                if o["path"] not in known and Path(o["path"]).is_file():
                    c = story.Candidate(o["path"], prompt, float(o.get("time") or time.time()), original, reference, "")
                    scene.candidates.append(c)
                    new.append(c)
        if not new:
            errors = [it.error.splitlines()[0] for it in items if it.error]
            self._end(FAILED, f"シーン {self.scene_index + 1} の候補が出来ませんでした" + (f" ({errors[0]})" if errors else ""))
            return
        try:
            st.adopt(scene, new[0].path)  # 次の「続き」のシーンの元画像 (最後のコマ) もここで作る
        except story.StoryError as exc:
            self._end(FAILED, f"シーン {self.scene_index + 1} を採用できません: {exc}")
            return
        if not self._save():
            return
        self.scene_index += 1
        self._next_scene()


# ----------------------------------------------------------------------
# 待ち行列
# ----------------------------------------------------------------------
class BatchQueue(QObject):
    """バッチの物語を、台の数 (最大 MAX_PARALLEL 本) まで並べて作る。上の件から順に、1 件の何本目も並べる。"""

    changed = Signal()  # 中身や状態が変わった (待ち行列の窓・帯を作り直す)
    progress = Signal(str)  # 帯に出す文
    running = Signal(bool)  # 作り始めた / すべて止まった
    entry_done = Signal(str)  # 1 件が終わった (ステータスバー)
    all_done = Signal(str)  # 待ち行列が空になった (この回に終わったもののまとめ)

    def __init__(self, view):
        super().__init__(view)
        self.view = view
        self.entries: list[Entry] = []
        self.paused = False
        self.restoring: list[Entry] = []  # 前回の残り (起動したときに続きからにするか聞くまで、書くときに残す)
        self._restoring_paused = False
        self._session: list[Entry] = []  # この回に終わったもの (まとめに出す)
        self.workers: list[StoryWorker] = []
        self._was_active = False
        self._timer = QTimer(self)
        self._timer.setInterval(TICK_MS)
        self._timer.timeout.connect(self.tick)
        self._timer.start()

    # 様子 ------------------------------------------------------------------
    @property
    def active(self) -> bool:
        return any(w.active for w in self.workers)

    def busy_workers(self) -> list[StoryWorker]:
        return [w for w in self.workers if w.active]

    def working_ids(self) -> set:
        return {w.entry.id for w in self.busy_workers()}

    def is_working(self, folder: str) -> bool:
        """その物語をバッチが作っている (作りかけで待っているものも)。"""
        key = str(Path(folder)).lower()
        return any(str(Path(f)).lower() == key for e in self.entries + self.restoring if e.unfinished for f in e.folders)

    def waiting(self) -> list[Entry]:
        return [e for e in self.entries if e.state == WAITING]

    def unfinished(self) -> int:
        return sum(1 for e in self.entries if e.unfinished)

    def set_machines(self, urls: list[str]) -> None:
        """台を変えた・休みにしたとき: 作っている物語の候補づくりにも新しい台を使わせる (次のシーンは作るときに読み直す)。"""
        for w in self.workers:
            if w.engine.active:
                w.engine.set_machines(urls)

    @staticmethod
    def capacity() -> int:
        """同時に作る物語の本数 (台の数。台の空きは量産ラインと分け合う)。"""
        return max(1, min(MAX_PARALLEL, len(recipes.machines())))

    def headline(self) -> str:
        """帯と待ち行列の窓の上に出す文。"""
        busy = self.busy_workers()
        n = sum(1 for e in self.entries if e.unfinished and e.id not in self.working_ids())
        if busy:
            text = f"物語のバッチ: {len(busy)} 本を作っています"
            if self.paused:
                text += " · 一時停止: 今のシーンが終わったら止まります"
            if n:
                text += f" · ほかに待機 {n} 件"
            return text + "  —  " + " / ".join(w.status for w in busy if w.status)
        if not n:
            return ""
        if self.paused:
            return f"一時停止中 (待機 {n} 件)。「再開」で続けます。"
        return f"待機 {n} 件 · まもなく始めます"

    def _emit_progress(self) -> None:
        self.progress.emit(self.headline())

    # 足す・外す・並べ替え --------------------------------------------------
    def add(self, plans: list[Plan]) -> list[Entry]:
        new = [Entry(p) for p in plans]
        self.entries.extend(new)
        self._save_and_notify()
        QTimer.singleShot(0, self.tick)  # 窓を閉じてから始める
        return new

    def remove(self, ids: list[str]) -> None:
        """待機中・終わったものは外す。作っているものは止める (止めた、として残す)。"""
        working = self.working_ids()
        stop = [e for e in self.entries if e.id in ids and e.id in working]
        self.entries = [e for e in self.entries if e.id not in ids or e.id in working]
        self._save_and_notify()
        for e in stop:
            self._stop_entry(e)

    def move(self, entry_id: str, delta: int) -> None:
        i = next((k for k, e in enumerate(self.entries) if e.id == entry_id), -1)
        j = i + delta
        if i < 0 or not 0 <= j < len(self.entries):
            return
        self.entries[i], self.entries[j] = self.entries[j], self.entries[i]
        self._save_and_notify()

    def clear_finished(self) -> None:
        self.entries = [e for e in self.entries if e.unfinished]
        self._save_and_notify()

    # 一時停止・止める ------------------------------------------------------
    def set_paused(self, on: bool) -> None:
        self.paused = on
        self._save_and_notify()
        self._emit_progress()
        if not on:
            QTimer.singleShot(0, self.tick)

    def stop_current(self) -> None:
        """作っているものをすべて止める (その件は「止めた」にして、次の件へ)。"""
        for e in [e for e in self.entries if e.id in self.working_ids()]:
            self._stop_entry(e)

    def _stop_entry(self, entry: Entry) -> None:
        entry.state = STOPPED
        entry.ended = time.time()
        for w in self.busy_workers():
            if w.entry is entry:
                w.stop()
        for f in entry.folders:  # 一時停止で残っていた作りかけも、止めたものにする
            entry.failed.append(f"{Path(f).name}: 止めました")
        entry.folders = []
        self._entry_finished(entry)
        self._save_and_notify()

    def shutdown(self) -> None:
        """閉じるとき: 作っているものは作りかけとして残す (次に起動したときに続きから)。"""
        self._timer.stop()
        for w in self.workers:
            try:
                w.ended.disconnect()
            except (RuntimeError, TypeError):
                pass
            w.halt()
            if w.winding_down:
                w.engine.discard()  # 頼み済みのものは ComfyUI の待ち行列から外す (受け取れないので)
            else:
                w.engine.stop_threads()
        for e in self.entries:
            if e.state == RUNNING:
                e.state = WAITING
        self._save()

    # 進める ----------------------------------------------------------------
    def _next_job(self) -> Optional[tuple]:
        """次に作る物語: (件, 作りかけのフォルダ, 本の番号)。上の件から、作りかけ → 新しい本の順。"""
        taken = {str(Path(w.folder)).lower() for w in self.busy_workers() if w.folder}
        for e in self.entries:
            if not e.unfinished:
                continue
            for f in e.folders:
                if str(Path(f)).lower() not in taken:
                    return e, f, -1
            if e.run_index < e.plan.runs:
                if not Path(e.plan.image).is_file():
                    # 足したあとで最初の画像を動かした・消した: 残りの本も作れないので、この件はここで終える
                    e.failed.append(f"{e.plan.name}: 最初の画像が見つかりません ({e.plan.image})")
                    e.run_index = e.plan.runs
                    self._update_state(e)
                    continue
                e.run_index += 1
                return e, "", e.run_index - 1
        return None

    def _idle_worker(self) -> Optional[StoryWorker]:
        for w in self.workers:
            if not w.active and not w.winding_down:
                return w
        if len(self.workers) < MAX_PARALLEL:
            w = StoryWorker(self, lambda: self.paused)
            w.ended.connect(self._ended)
            w.changed.connect(self._emit_progress)
            w.changed.connect(self.changed)
            self.workers.append(w)
            return w
        return None

    def tick(self) -> None:
        if self.paused:
            return
        if not recipes.machines():
            if self.waiting():
                self.progress.emit(f"待機 {len(self.waiting())} 件 · " + recipes.no_machines_reason())
            return
        started = False
        while len(self.busy_workers()) < self.capacity():
            worker = self._idle_worker()
            if worker is None:
                break  # 止めた候補づくりの片付けを待つ
            job = self._next_job()
            if job is None:
                break
            entry, folder, index = job
            if folder:
                self.view.release_story(folder)  # 物語のタブで開いていたら閉じる (同じ物語を両方が書かないように)
            entry.state = RUNNING
            started = True
            worker.start(entry, folder, index)
        if started:
            self._save_and_notify()
            self._emit_progress()
            if not self._was_active and self.active:
                self._was_active = True
                self.running.emit(True)
        self._check_all_done()

    def _ended(self, worker: StoryWorker, outcome: str, reason: str) -> None:
        e = worker.entry
        if e is None:
            return
        name = worker.name or e.plan.name
        if outcome != HALTED and worker.folder:
            key = str(Path(worker.folder)).lower()
            e.folders = [f for f in e.folders if str(Path(f)).lower() != key]
        if outcome == MADE:
            e.made.append(name)
        elif outcome in (FAILED, STOPPED_ONE):
            e.failed.append(f"{name}: {reason}")
        self._update_state(e)
        self._save_and_notify()
        self._emit_progress()
        QTimer.singleShot(0, self.tick)  # 次があれば続けて始める

    def _update_state(self, e: Entry) -> None:
        if e.state == STOPPED:
            return
        if any(w.entry is e for w in self.busy_workers()):
            e.state = RUNNING
        elif e.run_index >= e.plan.runs and not e.folders:
            e.state = DONE
            e.ended = time.time()
            self._entry_finished(e)
        else:
            e.state = WAITING

    def _entry_finished(self, e: Entry) -> None:
        if e not in self._session:
            self._session.append(e)
            self.entry_done.emit(e.summary())

    def _check_all_done(self) -> None:
        if self.active:
            return
        if self._was_active:
            self._was_active = False
            self.running.emit(False)
        if not any(e.unfinished for e in self.entries):
            self.paused = False  # 終わっていないものがなくなったら、一時停止は解く
            if self._session:
                done, self._session = self._session, []
                total = sum(len(e.made) for e in done)
                stopped = any(e.state == STOPPED for e in done)
                lines = [f"出来た物語 {total} 本 (バッチ {len(done)} 件)" + (" (止めました)" if stopped else "")]
                lines += [e.summary() for e in done]
                places = sorted({e.plan.parent for e in done if e.made})
                if places:  # バッチの物語は「最近開いた物語」に入れないので、場所を知らせる (「開く...」で開く)
                    lines.append("作った場所: " + " / ".join(places))
                self._save_and_notify()
                self.all_done.emit("\n".join(lines))
                return
        self._emit_progress()

    # 保存・続きから --------------------------------------------------------
    def _save(self) -> None:
        save_queue(self.restoring + self.entries, self.paused or self._restoring_paused)

    def _save_and_notify(self) -> None:
        self._save()
        self.changed.emit()

    def load_previous(self) -> tuple[int, bool]:
        """前回の残りを読む (まだ始めない)。(終わっていない件数, 一時停止のまま閉じたか)。"""
        entries, paused = load_queue()
        self.restoring = [e for e in entries if e.unfinished]
        for e in self.restoring:
            e.state = WAITING  # 作っていたものも、待機から続ける (作りかけの物語は続きから)
        self._restoring_paused = paused
        return len(self.restoring), paused

    def restore(self, resume: bool) -> None:
        """前回の残りを待ち行列の前に入れる (resume=False なら捨てる)。"""
        old, paused = self.restoring, self._restoring_paused
        self.restoring, self._restoring_paused = [], False
        if resume and old:
            self.entries = old + self.entries
            self.paused = self.paused or paused
        self._save_and_notify()
        if resume:
            QTimer.singleShot(0, self.tick)


# ----------------------------------------------------------------------
# 待ち行列の窓
# ----------------------------------------------------------------------
class QueueWindow(QDialog):
    """待ち行列の一覧 (開いたままにできる)。"""

    def __init__(self, queue: BatchQueue, add: Callable[[], None], parent=None):
        super().__init__(parent)
        self.setWindowTitle("物語のバッチ - 待ち行列")
        self.resize(820, 380)
        self.queue = queue
        root = QVBoxLayout(self)
        self.head = QLabel()
        self.head.setWordWrap(True)
        root.addWidget(self.head)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["状態", "名前", "最初の画像", "中身", "結果"])
        self.table.verticalHeader().hide()
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        header = self.table.horizontalHeader()
        for col, mode in enumerate((QHeaderView.ResizeMode.ResizeToContents, QHeaderView.ResizeMode.Interactive,
                                    QHeaderView.ResizeMode.Interactive, QHeaderView.ResizeMode.ResizeToContents,
                                    QHeaderView.ResizeMode.Stretch)):
            header.setSectionResizeMode(col, mode)
        self.table.setColumnWidth(1, 160)
        self.table.setColumnWidth(2, 160)
        self.table.itemSelectionChanged.connect(self._update_buttons)
        root.addWidget(self.table, 1)
        row = QHBoxLayout()
        self.add_b = QPushButton("足す...")
        self.add_b.clicked.connect(add)
        self.pause_b = QPushButton()
        self.pause_b.clicked.connect(lambda: self.queue.set_paused(not self.queue.paused))
        self.up_b = QPushButton("上へ")
        self.up_b.clicked.connect(lambda: self._move(-1))
        self.down_b = QPushButton("下へ")
        self.down_b.clicked.connect(lambda: self._move(1))
        self.remove_b = QPushButton("選んだものを外す")
        self.remove_b.clicked.connect(self._remove)
        self.clear_b = QPushButton("終わったものを消す")
        self.clear_b.clicked.connect(self.queue.clear_finished)
        close = QPushButton("閉じる")
        close.clicked.connect(self.close)
        for b in (self.add_b, self.pause_b, self.up_b, self.down_b, self.remove_b, self.clear_b):
            row.addWidget(b)
        row.addStretch(1)
        row.addWidget(close)
        root.addLayout(row)
        queue.changed.connect(self.refresh)
        queue.progress.connect(lambda _t: self._update_head())
        self.refresh()

    def _selected_ids(self) -> list[str]:
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        return [self.table.item(r, 0).data(Qt.ItemDataRole.UserRole) for r in rows if self.table.item(r, 0)]

    def refresh(self) -> None:
        keep = set(self._selected_ids())
        entries = self.queue.entries
        self.table.setRowCount(len(entries))
        working = self.queue.working_ids()
        for r, e in enumerate(entries):
            p = e.plan
            if e.id in working:
                k = sum(1 for w in self.queue.busy_workers() if w.entry is e)
                state = f"実行中 ({k} 本を作っています · {e.run_index} / {p.runs} 本目まで)"
            elif e.state == WAITING and self.queue.paused:
                state = "一時停止"
            else:
                state = STATE_LABELS.get(e.state, e.state)
            result = ""
            if e.made or e.failed or not e.unfinished:
                result = f"出来た {len(e.made)} 本"
                if e.failed:
                    result += " · 止まった: " + " / ".join(e.failed)
            if e.folders:
                result += (" · " if result else "") + "作りかけ: " + " / ".join(Path(f).name for f in e.folders)
            cells = [state, p.name, Path(p.image).name, f"{len(p.cuts)} カット × {p.runs} 本", result]
            for c, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setToolTip(p.image if c == 2 else text)
                if c == 0:
                    item.setData(Qt.ItemDataRole.UserRole, e.id)
                if e.id in working:  # 実行中の行は色付きの太字 (生成の標準のジョブ一覧と同じ)
                    font = item.font()
                    font.setBold(True)
                    item.setFont(font)
                    item.setForeground(QColor(theme.ACCENT))
                self.table.setItem(r, c, item)
            if e.id in keep:
                self.table.selectRow(r)
        self.pause_b.setText("再開" if self.queue.paused else "一時停止")
        self.pause_b.setToolTip("今のシーンが終わったら止めます。作りかけの物語は、再開するとその続きから作ります。"
                                if not self.queue.paused else "待ち行列を続けます。")
        self._update_head()
        self._update_buttons()

    def _update_head(self) -> None:
        busy = self.queue.active
        text = self.queue.headline()
        if not text:
            text = "待ち行列は空です。" if not self.queue.entries else "動いているものはありません。"
        color = "#f0a030" if self.queue.paused else theme.ACCENT if busy else theme.TEXT_DIM
        self.head.setText(text)
        self.head.setStyleSheet(f"color: {color}; font-weight: bold;")

    def _update_buttons(self) -> None:
        ids = self._selected_ids()
        self.remove_b.setEnabled(bool(ids))
        movable = len(ids) == 1
        self.up_b.setEnabled(movable)
        self.down_b.setEnabled(movable)
        self.clear_b.setEnabled(any(not e.unfinished for e in self.queue.entries))

    def _move(self, delta: int) -> None:
        ids = self._selected_ids()
        if len(ids) == 1:
            self.queue.move(ids[0], delta)
            for r in range(self.table.rowCount()):
                if self.table.item(r, 0).data(Qt.ItemDataRole.UserRole) == ids[0]:
                    self.table.selectRow(r)

    def _remove(self) -> None:
        ids = self._selected_ids()
        if not ids:
            return
        chosen = [e for e in self.queue.entries if e.id in ids]
        text = (f"「{chosen[0].plan.name}」を外しますか?" if len(chosen) == 1 else f"{len(chosen)} 件を外しますか?")
        if any(i in self.queue.working_ids() for i in ids):
            text += "\n実行中のものは止めます (ComfyUI に中止を送ります。出来た物語はそのまま残ります)。"
        if QMessageBox.question(self, "外す", text, QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        self.queue.remove(ids)


def ask_plans(parent, defaults: Callable[[Plan], None], images: Optional[list] = None,
              opened: Optional[Callable[[Optional[BatchDialog]], None]] = None) -> list[Plan]:
    """設定の窓を出して、足すなら Plan (最初の画像 1 枚ごとに 1 つ) を返す。
    defaults で今の物語から初めの値を入れる (前の設定がないところだけ)。opened には開いている窓を知らせる。"""
    plan = Plan.load()
    defaults(plan)
    dialog = BatchDialog(plan, parent, images)
    if opened is not None:
        opened(dialog)
    try:
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return []
    finally:
        if opened is not None:
            opened(None)
    return dialog.plans
