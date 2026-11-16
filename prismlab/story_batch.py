"""物語のバッチ (プロトタイプ): 最初の画像とカットごとのプロンプトから、物語を何本かまとめて作る。

1 本ごとに新しい物語のフォルダを作り、プロンプトの数だけシーンを並べる (2 つ目からは「続き」・顔は前と同じ)。
シーン 1 から順に、(英語に / 盛る) → 候補を 1 つ作る → 自動で採用 → その最後のコマが次のシーンの元画像、
を最後まで繰り返す。失敗したらその物語はそこで止め、次の本へ進む。出来た物語は普通の物語 (手で直せる)。
候補づくりは物語のタブのエンジン (一度に 1 つ) をそのまま使う。量産ラインとは同時に動かさない。
"""

import json
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
                               QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
                               QPlainTextEdit, QPushButton, QSpinBox, QVBoxLayout, QWidget)

from gencommon import comfy, enhancer, store

from . import kinds, recipes, story, theme, translate

PLAN_FILE = "story_batch.json"  # 前に使ったバッチの設定 (窓を開いたときに入れる)
MAX_RUNS = 20


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
    def load() -> "Plan":
        plan = Plan()
        try:
            data = json.loads(store.read_text(store.app_file(PLAN_FILE)) or "{}")
        except ValueError:
            data = {}
        if isinstance(data, dict):
            for key, value in data.items():
                if key in plan.__dataclass_fields__ and key != "cuts":
                    setattr(plan, key, value)
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
    """物語を作り、カットの数だけシーンを並べる (1 つ目はシーンチェンジ・最初の画像、2 つ目からは続き)。"""
    st = story.Story.create(str(folder), title)
    first = st.scenes[0]
    st.set_source_file(first, plan.image)
    if plan.reference:
        st.set_reference(first, plan.reference)
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
# 設定の窓
# ----------------------------------------------------------------------
class BatchDialog(QDialog):
    def __init__(self, plan: Plan, parent=None):
        super().__init__(parent)
        self.setWindowTitle("物語をバッチで作る (試し)")
        self.setMinimumWidth(620)
        self.plan = plan
        root = QVBoxLayout(self)
        note = QLabel("最初の画像とカットごとのプロンプトから、物語をまとめて作ります。各シーンの候補は 1 つで、"
                      "自動で採用して次のシーンへ続けます (出来は運まかせ。あとで物語のタブで手直しできます)。")
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {theme.TEXT_DIM};")
        root.addWidget(note)
        form = QFormLayout()
        root.addLayout(form)

        self.name = QLineEdit(plan.name)
        form.addRow("物語の名前", self.name)
        self.parent_edit = QLineEdit(plan.parent)
        form.addRow("作る場所", self._with_button(self.parent_edit, "選ぶ...", self._choose_parent))
        self.image = QLineEdit(plan.image)
        form.addRow("最初の画像", self._with_button(self.image, "選ぶ...", lambda: self._choose_image(self.image)))
        self.reference = QLineEdit(plan.reference)
        self.reference.setPlaceholderText("なし (顔を指定するワークフローのときだけ使います)")
        form.addRow("リファレンス (顔)", self._with_button(self.reference, "選ぶ...",
                                                         lambda: self._choose_image(self.reference)))
        self.prompts = QPlainTextEdit(plan.prompts)
        self.prompts.setPlaceholderText("歩いてくる / 振り返って笑う / 走り去る\n(「/」か改行で区切ると、その数がカット数)")
        self.prompts.setFixedHeight(120)
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
        self.start = buttons.addButton("始める", QDialogButtonBox.ButtonRole.AcceptRole)
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
        self.cut_count.setText(f"カット数 {n}" + (f" × {runs} 本 = 候補 {n * runs} 本" if runs > 1 else ""))

    def _accept(self) -> None:
        p = self.plan
        p.name = self.name.text().strip()
        p.parent = self.parent_edit.text().strip()
        p.image = self.image.text().strip()
        p.reference = self.reference.text().strip()
        p.prompts = self.prompts.toPlainText()
        p.workflow = str(self.workflow.currentData() or "")
        p.seconds = self.seconds.value() if self.use_seconds.isChecked() else None
        p.base_prompt = self.base.isChecked()
        p.english, p.enhance = self.english.isChecked(), self.enhance.isChecked()
        p.enhancer = str(self.enhancer.currentData() or "")
        p.runs = self.runs.value()
        p.cuts = split_prompts(p.prompts)
        problem = check_plan(p)
        if problem:
            QMessageBox.information(self, "バッチ", problem)
            return
        p.save()
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
# 進める役
# ----------------------------------------------------------------------
class BatchRunner(QObject):
    """物語のタブ (view) の候補づくりを使って、シーンを順に作る。"""

    progress = Signal(str)  # 今していること (物語のタブの帯に出す)
    done = Signal(str)  # 終わったときのまとめ
    _llm = Signal(int, str, str)  # (頼んだ番号, 文, 失敗の理由)

    def __init__(self, view):
        super().__init__(view)
        self.view = view
        self.plan: Optional[Plan] = None
        self.active = False
        self.story: Optional[story.Story] = None
        self.run_index = 0
        self.scene_index = 0
        self.made: list[str] = []  # 最後まで出来た物語の名前
        self.failed: list[str] = []  # 途中で止まった物語 (名前: 理由)
        self._waiting = False  # 候補の出来上がりを待っている
        self._known: set = set()  # 頼む前の候補
        self._ticket = 0  # LLM の頼みの番号 (止めたあとに届いたものは捨てる)
        self._llm.connect(self._on_llm)
        view.engine.finished.connect(self._on_finished)  # 物語のタブが候補を受け取ったあとに呼ばれる

    # 始める・止める ---------------------------------------------------------
    def start(self, plan: Plan) -> None:
        self.plan, self.active = plan, True
        self.run_index, self.made, self.failed = 0, [], []
        self._next_story()

    def stop(self) -> None:
        if not self.active:
            return
        self._ticket += 1
        reason = "止めました"
        if self._waiting and self.view.engine.active:
            self.view.engine.cancel_all()  # 出来上がり (取り消し) は _on_finished で受けるが、active でないので何もしない
        self._waiting = False
        if self.story is not None:
            self.failed.append(f"{self.story.name}: {reason}")
        self._finish(stopped=True)

    def _finish(self, stopped: bool = False) -> None:
        self.active = False
        self.story = None
        lines = [f"出来た物語 {len(self.made)} 本" + (" (止めました)" if stopped else "")]
        if self.failed:
            lines.append("途中で止まったもの: " + " / ".join(self.failed))
        self.done.emit("\n".join(lines))

    # 物語 ------------------------------------------------------------------
    def _next_story(self) -> None:
        p = self.plan
        if not self.active:
            return
        if self.run_index >= p.runs:
            self._finish()
            return
        title = p.name if p.runs == 1 else f"{p.name} {self.run_index + 1:02}"
        try:
            folder = story_folder(p.parent, p.name, self.run_index, p.runs)
            self.story = create_story(p, folder, title)
        except story.StoryError as exc:
            self.failed.append(f"{title}: {exc}")
            self._finish()
            return
        self.view.open_batch_story(str(folder))
        self.scene_index = 0
        self._next_scene()

    def _story_failed(self, reason: str) -> None:
        if self.story is not None:
            self.failed.append(f"{self.story.name}: {reason}")
        self.run_index += 1
        self._next_story()

    # シーン ----------------------------------------------------------------
    def _status(self, what: str) -> None:
        p = self.plan
        head = f"バッチ {self.run_index + 1} / {p.runs} 本目 · シーン {self.scene_index + 1} / {len(p.cuts)}"
        self.progress.emit(f"{head} · {what}")

    def _scene(self) -> Optional[story.Scene]:
        st = self.view.story
        if st is None or self.story is None or Path(st.folder) != Path(self.story.folder):
            return None  # 物語のタブで別の物語に替わった (バッチ中は替えられないはず)
        return st.scenes[self.scene_index] if self.scene_index < len(st.scenes) else None

    def _next_scene(self) -> None:
        if not self.active:
            return
        st = self.view.story
        if self.scene_index >= len(st.scenes):
            self.made.append(st.name)
            self.run_index += 1
            self._next_story()
            return
        scene = self._scene()
        if scene is None:
            self._story_failed("物語が替わりました")
            return
        self.view.select(scene)
        if not scene.source or not Path(scene.source).is_file():
            self._story_failed(f"シーン {self.scene_index + 1} の元画像がありません (前のシーンの最後のコマが取れません)")
            return
        p = self.plan
        if p.english or p.enhance:
            self._ticket += 1
            ticket, text = self._ticket, scene.original
            snap_en = translate.snapshot() if p.english else None
            found = [x for x in enhancer.load_presets() if not x.is_empty and x.name == p.enhancer]
            snap_enh = enhancer.snapshot(found[0]) if p.enhance and found else None
            self._status("英語に直しています…" if p.english else "盛っています…")

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
        scene = self._scene()
        if scene is None:
            self._story_failed("物語が替わりました")
            return
        if error or not text:
            self._story_failed(f"シーン {self.scene_index + 1} の LLM に失敗: {error or '返事が空'}")
            return
        scene.prompt = text
        scene.record("バッチで送る文 (英語に / 盛る)", text)
        self.view.scene_text_changed(scene)
        self._make(scene)

    def _make(self, scene: story.Scene) -> None:
        self._known = {c.path for c in scene.candidates}
        self._status("候補を作っています…")
        problem = self.view.make_candidates_for(scene)
        if problem:
            self._story_failed(f"シーン {self.scene_index + 1}: {problem}")
            return
        self._waiting = True

    def _on_finished(self) -> None:
        if not self.active or not self._waiting:
            return
        self._waiting = False
        scene = self._scene()
        if scene is None:
            self._story_failed("物語が替わりました")
            return
        new = sorted((c for c in scene.candidates if c.path not in self._known and Path(c.path).is_file()),
                     key=lambda c: c.time)
        if not new:
            _r, items, _m = self.view.engine.snapshot()
            errors = [it.error.splitlines()[0] for it in items if it.error]
            self._story_failed(f"シーン {self.scene_index + 1} の候補が出来ませんでした"
                               + (f" ({errors[0]})" if errors else ""))
            return
        try:
            self.view.story.adopt(scene, new[0].path)  # 次の「続き」のシーンの元画像 (最後のコマ) もここで作る
        except story.StoryError as exc:
            self._story_failed(f"シーン {self.scene_index + 1} を採用できません: {exc}")
            return
        self.view.batch_adopted()
        self.scene_index += 1
        self._next_scene()


def ask_plan(parent, defaults: Callable[[Plan], None]) -> Optional[Plan]:
    """設定の窓を出して、始めるなら Plan を返す。defaults で今の物語から初めの値を入れる (前の設定がないところだけ)。"""
    plan = Plan.load()
    defaults(plan)
    dialog = BatchDialog(plan, parent)
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return None
    return dialog.plan

