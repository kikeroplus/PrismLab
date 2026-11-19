"""ステップの設定・台の設定の窓。登録 (接続先・ワークフロー・読み取りプリセット・エンハンサー) は
GenCommon の設定の窓をそのまま開く (3 つのアプリと共有の登録)。"""

from typing import Optional

from pathlib import Path

from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog,
                               QFormLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QPushButton,
                               QSpinBox, QVBoxLayout)

from gencommon import comfy, enhancer, inserts, reader, settings
from gencommon.comfy_dialogs import ComfySettingsDialog

from . import kinds, recipes, theme
from .widgets import KindChip


def _hint(text: str) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    label.setProperty("hint", True)
    return label


class StageDialog(QDialog):
    """1 つのステップの設定。OK で stage を書き換える。"""

    def __init__(self, stage: recipes.Stage, parent=None, running: bool = False):
        """running: 動いている量産のステップを直す (ワークフローは変えられない。これからそのステップに来る件から効く)。"""
        super().__init__(parent)
        self.stage = stage
        self.setWindowTitle(f"ステップの設定: {recipes.STAGE_TITLES[stage.type]}" + (" (量産中)" if running else ""))
        self.resize(520, 0)
        box = QVBoxLayout(self)
        form = QFormLayout()
        box.addLayout(form)

        if stage.type == recipes.READ:
            self.preset = QComboBox()
            self.reread = QCheckBox("読み取り済み (.txt がある) でも読み直す")
            self.reread.setChecked(stage.reread)
            edit = QPushButton("読み取りの設定...")
            edit.clicked.connect(self._edit_reader)
            row = QHBoxLayout()
            row.addWidget(self.preset, 1)
            row.addWidget(edit)
            form.addRow("読み取りプリセット", row)
            form.addRow("", self.reread)
            box.addWidget(_hint("画像をビジョン対応の LLM に見せて、説明プロンプトを書かせます。"
                                "書いた説明は画像と同じ名前の .txt に残し、次からはそれを使います。"))
            self._fill_llm(reader.load_presets(), stage.reader)
        elif stage.type == recipes.ENHANCE:
            self.preset = QComboBox()
            edit = QPushButton("エンハンサーの設定...")
            edit.clicked.connect(self._edit_enhancer)
            row = QHBoxLayout()
            row.addWidget(self.preset, 1)
            row.addWidget(edit)
            form.addRow("エンハンサー", row)
            box.addWidget(_hint("前のステップまでの文 (読み取った説明・その後に足した文) を LLM に盛らせます。"
                                "1 枚ごとに盛るので、毎回違う盛り方になります。"))
            self._fill_llm(enhancer.load_presets(), stage.enhancer)
        elif stage.type == recipes.TEXT:
            self.text = QPlainTextEdit(stage.text)
            self.text.setPlaceholderText("例: slow camera pan, cinematic lighting")
            self.text.setMinimumHeight(120)
            form.addRow("足す文", self.text)
            box.addWidget(_hint("前のステップまでのプロンプトに、改行してこの文を足します (どの画像にも同じ文)。"
                                "「盛る」より前に置くと一緒に盛り、あとに置くと盛った文にそのまま付けます。\n"
                                "ワイルドカード: {red|blue|white} と書くと、1 件ごとにどれか 1 つを選びます"
                                " (入れ子もできます。| のない {…} はそのまま送ります)。"))
        elif stage.type in recipes.MEMO_TYPES:
            self.memo = QComboBox()
            self.memo.setEditable(True)
            owner = getattr(parent, "recipe", None)
            names = [s.memo or recipes.DEFAULT_MEMO for s in getattr(owner, "stages", []) if s.type == recipes.SAVE]
            for n in dict.fromkeys(names + [recipes.DEFAULT_MEMO]):
                self.memo.addItem(n)
            self.memo.setCurrentText(stage.memo or (names[-1] if stage.type == recipes.LOAD and names
                                                    else recipes.DEFAULT_MEMO))
            form.addRow("メモの名前", self.memo)
            if stage.type == recipes.SAVE:
                box.addWidget(_hint("この時点のプロンプト (元の文・盛った文・足した文) を、この名前のメモに覚えます。"
                                    "生成を通るとプロンプトは空に戻りますが、メモは残ります。"
                                    "あとの「思い出す」で同じ名前を選ぶと、その文を使えます"
                                    " (例: 盛る → 覚える → T2I → 思い出す → I2V で、T2I と同じ文を I2V にも送る)。"))
            else:
                self.replace = QCheckBox("今の文を置き換える (外すと、今の文の後ろに足す)")
                self.replace.setChecked(stage.mode != recipes.APPEND)
                form.addRow("", self.replace)
                box.addWidget(_hint("前の「覚える」で覚えた文を戻します。置き換えるときは覚えた時点の状態 (盛った文・足した文も) "
                                    "にそっくり戻します。後ろに足すときは、今の文 (読み取った説明など) のあとに、覚えた文をつなげます。"
                                    "このあとに「盛る」を置けば、それを盛ります。"))
        elif stage.type == recipes.TRANSLATE:
            edit = QPushButton("エンハンサーの設定...")
            edit.clicked.connect(self._edit_llm)
            self.model_label = QLabel()
            row = QHBoxLayout()
            row.addWidget(self.model_label, 1)
            row.addWidget(edit)
            form.addRow("LLM", row)
            self._show_model()
            box.addWidget(_hint("いまのプロンプト (読み取った文・盛った文・足した文) を LLM で英語にします。"
                                "日本語などが入っている文だけを訳し、英語だけの文は送りません。"
                                "同じ文は 1 回だけ訳します。(word:1.2) や <lora:...> はそのまま残すよう頼みます。\n"
                                "サーバー・モデルはエンハンサーと同じものを使います。\n"
                                "例: 追加テキスト (日本語) → 英語に → I2V。盛ったあとに置けば、盛った文も英語にします。"))
        elif stage.type == recipes.FRAME:
            box.addWidget(_hint("前の生成で出来た動画の最後のコマを画像にして、次のステップに渡します"
                                " (出力フォルダ\\コマ に PNG で残します)。\n"
                                "例: I2V → 最後のコマ → I2V で、続きの動画を作る (物語の「続き」と同じつなぎ方)。"
                                "プロンプトは生成を通ると空に戻るので、続きの文は「思い出す」や「追加テキスト」で入れます。"))
        elif stage.type == recipes.SPLIT:
            self.count = QSpinBox()
            self.count.setRange(2, recipes.MAX_SPLIT)
            self.count.setValue(stage.count)
            self.count.setSuffix(" 本")
            form.addRow("分ける数", self.count)
            box.addWidget(_hint("1 件をここで何本かに分けて、そこから先をそれぞれ通します。ここまで (読み取りなど) は 1 回で済みます。"
                                "あとの「盛る」は 1 本ごとに盛り直し、「差し込み」は 1 本ごとに次のファイル、"
                                "追加テキストのワイルドカードも 1 本ごとに選び直します。\n"
                                "例: 読み取り → 増やす ×3 → 盛る → I2V で、1 枚から 3 本の違う動画。"))
        elif stage.type == recipes.GATE:
            self.stars = QComboBox()
            for n in range(1, 6):
                self.stars.addItem("★" * n + f" ({n} 以上)", n)
            self.stars.setCurrentIndex(max(0, min(4, stage.stars - 1)))
            form.addRow("先へ進める★", self.stars)
            box.addWidget(_hint("ここで止めて、J-Rate でこの★以上を付けたものだけを先へ進めます。"
                                "★は PrismLab に戻ったとき・1 分ごと・「★を読み直す」で読みます。"
                                "選び終わったら「★待ちを終える...」で、★のないものを終えます (出来たものは残ります)。\n"
                                "例: T2I → ★を待つ → 読み取り → 盛る → I2V。夜に T2I を流し、朝に★を付けて続きを流す。"
                                "「1 枚で試す」では止めずに通します。"))
        elif stage.type == recipes.SUB:
            self.rules = QPlainTextEdit(stage.rules)
            self.rules.setPlaceholderText("masterpiece\nlong hair => short hair\nbest quality =>")
            self.rules.setMinimumHeight(140)
            form.addRow("置き換え", self.rules)
            box.addWidget(_hint("1 行に「元 => 先」と書きます (「→」でも可)。先を書かない行は、その語を消します。"
                                "大文字・小文字は区別しません。消したあとに残る「, ,」は詰めます。# で始まる行は飛ばします。\n"
                                "いまのプロンプト (読み取った文・盛った文・足した文) のすべてに使います。"
                                "盛ったあとに置けば、LLM が入れがちな語を消せます。"))
        elif stage.type == recipes.INSERT:
            self.folder = QLineEdit(stage.folder)
            self.folder.setPlaceholderText("セリフやアクションの .txt を入れたフォルダ")
            browse = QPushButton("参照...")
            browse.clicked.connect(self._browse_folder)
            row = QHBoxLayout()
            row.addWidget(self.folder, 1)
            row.addWidget(browse)
            form.addRow("フォルダ", row)
            self.next_note = _hint("")
            self.restart = QPushButton("最初から使う")
            self.restart.setToolTip("このフォルダを 1 つ目のファイルから使い直します (ほかのアプリとも共有の位置)。")
            self.restart.clicked.connect(self._restart_folder)
            nrow = QHBoxLayout()
            nrow.addWidget(self.next_note, 1)
            nrow.addWidget(self.restart)
            form.addRow("", nrow)
            box.addWidget(_hint("フォルダの .txt を名前順に、1 枚ごとに 1 つずつ足します。最後まで使ったら最初に戻ります。"
                                "どこまで使ったかは PromptWordCrafter・ImageMimicArt・J-Rate の差し込みと共有で、"
                                "次の量産はその続きから使います (「1 枚で試す」では進めません)。"
                                "「盛る」より前に置くと一緒に盛り、あとに置くとそのまま付けます (セリフを一字一句守るならあと)。"))
            self.folder.textChanged.connect(self._folder_changed)
            self._folder_changed()
        else:
            self.workflow = QComboBox()
            self.chip = KindChip()
            edit = QPushButton("生成の設定...")
            edit.clicked.connect(self._edit_comfy)
            row = QHBoxLayout()
            row.addWidget(self.workflow, 1)
            row.addWidget(self.chip)
            row.addWidget(edit)
            form.addRow("ワークフロー", row)
            self.wf_note = _hint("")
            form.addRow("", self.wf_note)
            self.use_seconds = QCheckBox("長さを指定する")
            self.seconds = QDoubleSpinBox()
            self.seconds.setRange(1, 60)
            self.seconds.setDecimals(1)
            self.seconds.setSuffix(" 秒")
            self.seconds.setValue(stage.seconds or 5)
            self.use_seconds.setChecked(stage.seconds is not None)
            self.use_seconds.toggled.connect(self.seconds.setEnabled)
            srow = QHBoxLayout()
            srow.addWidget(self.use_seconds)
            srow.addWidget(self.seconds)
            srow.addStretch(1)
            form.addRow("長さ", srow)
            self.base = QCheckBox("ベースプロンプトを付ける")
            self.base.setChecked(stage.base_prompt)
            form.addRow("", self.base)
            self.base_text = _hint("")
            form.addRow("", self.base_text)
            self.workflow.currentIndexChanged.connect(self._workflow_changed)
            self._fill_workflows(stage.workflow)
            if running:
                # ワークフローは量産中は直さない運用 (2026-11-19)。長さ・ベースプロンプトは直せる
                self.workflow.setEnabled(False)
                edit.setEnabled(False)

        if running:
            box.addWidget(_hint("量産中です。OK すると、これからこのステップに来る件から新しい中身で進みます "
                                "(もう通った件はそのまま)。"))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        box.addWidget(buttons)

    # 差し込み -------------------------------------------------------------
    def _browse_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "差し込みのフォルダを選ぶ", self.folder.text().strip())
        if folder:
            self.folder.setText(str(Path(folder)))

    def _folder_changed(self) -> None:
        try:
            note = inserts.peek(self.folder.text().strip()).describe()
            ok = True
        except inserts.InsertError as exc:
            note, ok = str(exc), False
        self.next_note.setText(note)
        self.next_note.setStyleSheet("" if ok else "color: #ef5350;")
        self.restart.setEnabled(ok)

    def _restart_folder(self) -> None:
        inserts.reset(self.folder.text().strip())
        self._folder_changed()

    # 読み取り・盛る -------------------------------------------------------
    def _fill_llm(self, presets: list, current: str) -> None:
        self.preset.clear()
        for i, p in enumerate(presets):
            if not p.is_empty:
                self.preset.addItem(p.label(i), p.name)
        index = self.preset.findData(current)
        self.preset.setCurrentIndex(index if index >= 0 else 0)

    def _edit_reader(self) -> None:
        index = max(0, next((i for i, p in enumerate(reader.load_presets()) if p.name == self.preset.currentData()), 0))
        reader.ReaderSettingsDialog(index, self).exec()
        self._fill_llm(reader.load_presets(), self.preset.currentData())

    def _edit_enhancer(self) -> None:
        index = max(0, next((i for i, p in enumerate(enhancer.load_presets())
                             if p.name == self.preset.currentData()), 0))
        enhancer.EnhancerSettingsDialog(index, self).exec()
        self._fill_llm(enhancer.load_presets(), self.preset.currentData())

    def _show_model(self) -> None:
        self.model_label.setText(f"{settings.enhance_model()}  ({settings.llm_url()})")

    def _edit_llm(self) -> None:
        enhancer.EnhancerSettingsDialog(0, self).exec()
        self._show_model()

    # 生成 -----------------------------------------------------------------
    def _fill_workflows(self, current: str) -> None:
        self.workflow.blockSignals(True)
        self.workflow.clear()
        for i, p in enumerate(comfy.workflows()):
            if not p.is_empty:
                kind = kinds.kind_of(p.value)
                self.workflow.addItem(p.file_label(i) + (f"   [{kind}]" if kind else ""), p.value)
        index = next((i for i in range(self.workflow.count())
                      if str(self.workflow.itemData(i)).lower() == current.lower()), -1)
        if index < 0 and current:
            self.workflow.addItem(f"(登録にない) {current}", current)
            index = self.workflow.count() - 1
        self.workflow.setCurrentIndex(max(index, 0))
        self.workflow.blockSignals(False)
        self._workflow_changed()

    def _workflow_changed(self) -> None:
        path = str(self.workflow.currentData() or "")
        kind = kinds.kind_of(path)
        self.chip.set(kind, kinds.color_of(kind))
        duration = None
        note = ""
        if not path:
            note = "ワークフローが登録されていません。「生成の設定...」で登録してください。"
        else:
            try:
                wf = comfy.load_workflow(path)
                duration = comfy.get_duration(wf)
                note = comfy.describe_workflow(wf)
            except comfy.ComfyError as exc:
                note = str(exc)
        self.wf_note.setText(note)
        has_duration = duration is not None
        self.use_seconds.setEnabled(has_duration)
        self.seconds.setEnabled(has_duration and self.use_seconds.isChecked())
        if not has_duration:
            self.use_seconds.setToolTip("このワークフローには長さを入れる場所がありません。")
        base = comfy.base_prompt_for(kinds.is_video(kind)).strip().replace("\n", " ")
        which = "動画用" if kinds.is_video(kind) else "画像用"
        self.base_text.setText(f"＋ ベースプロンプト ({which}) : {base[:90] or '(空)'}")

    def _edit_comfy(self) -> None:
        dialog = ComfySettingsDialog(self, "ワークフロー")
        dialog.exec()
        self._fill_workflows(str(self.workflow.currentData() or ""))

    def _accept(self) -> None:
        s = self.stage
        if s.type == recipes.READ:
            s.reader = str(self.preset.currentData() or "")
            s.reread = self.reread.isChecked()
        elif s.type == recipes.ENHANCE:
            s.enhancer = str(self.preset.currentData() or "")
        elif s.type == recipes.TEXT:
            text = self.text.toPlainText().strip()
            if not text:
                QMessageBox.warning(self, "追加テキスト", "足す文を入れてください。")
                return
            s.text = text
        elif s.type in recipes.MEMO_TYPES:
            s.memo = self.memo.currentText().strip() or recipes.DEFAULT_MEMO
            if s.type == recipes.LOAD:
                s.mode = recipes.REPLACE if self.replace.isChecked() else recipes.APPEND
        elif s.type in (recipes.FRAME, recipes.TRANSLATE):
            pass
        elif s.type == recipes.SPLIT:
            s.count = self.count.value()
        elif s.type == recipes.GATE:
            s.stars = int(self.stars.currentData())
        elif s.type == recipes.SUB:
            text = self.rules.toPlainText().strip()
            if not recipes.parse_rules(text):
                QMessageBox.warning(self, "置き換え", "置き換える語を入れてください (1 行に「元 => 先」)。")
                return
            s.rules = text
        elif s.type == recipes.INSERT:
            folder = self.folder.text().strip()
            try:
                inserts.list_files(folder)
            except inserts.InsertError as exc:
                QMessageBox.warning(self, "差し込み", str(exc))
                return
            s.folder = str(Path(folder))
        else:
            if self.workflow.isEnabled():  # 量産中は変えない (一覧になくても元のまま)
                s.workflow = str(self.workflow.currentData() or "")
            s.seconds = self.seconds.value() if self.use_seconds.isChecked() and self.use_seconds.isEnabled() else None
            s.base_prompt = self.base.isChecked()
        self.accept()


class MachinesDialog(QDialog):
    """台の設定: 登録してある接続先から 3 台まで選ぶ。"""

    def __init__(self, urls: list[str], parent=None, title: str = "台の設定", hint: str = "",
                 off: Optional[set] = None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(480, 0)
        box = QVBoxLayout(self)
        box.addWidget(_hint(hint or (
            "使う ComfyUI を 3 台まで選びます (量産ラインと物語で共通)。役割は決めず、空いた台に何でも回します。"
            "1 台に送るのは量産ラインと物語を合わせて 2 件までで、両方が待っているときは物語が先です。"
            "どの台も同じワークフローを使うので、ワークフローが使うモデルはどの台にも同じ名前で置いてください。"
            "「使う」を外した台は休み (設定に残したまま一時的に使わない) です。台の札のチェックでも切り替えられます。")))
        form = QFormLayout()
        box.addLayout(form)
        self.boxes: list[QComboBox] = []
        self.uses: list[QCheckBox] = []
        for n in range(recipes.MAX_MACHINES):
            combo = QComboBox()
            use = QCheckBox("使う")
            use.setChecked(not (n < len(urls) and urls[n].lower() in (off or set())))
            row = QHBoxLayout()
            row.addWidget(combo, 1)
            row.addWidget(use)
            form.addRow(f"台 {n + 1}", row)
            self.boxes.append(combo)
            self.uses.append(use)
        edit = QPushButton("接続先を登録する (生成の設定)...")
        edit.clicked.connect(self._edit)
        box.addWidget(edit)
        self.test = QPushButton("接続テスト")
        self.test.clicked.connect(self._test)
        self.result = _hint("")
        row = QHBoxLayout()
        row.addWidget(self.test)
        row.addWidget(self.result, 1)
        box.addLayout(row)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        box.addWidget(buttons)
        self._fill(urls)

    def _fill(self, urls: list[str]) -> None:
        conns = [(p.label(i), p.value.rstrip("/")) for i, p in enumerate(comfy.connections()) if not p.is_empty]
        for n, combo in enumerate(self.boxes):
            combo.clear()
            combo.addItem("(使わない)", "")
            for label, url in conns:
                combo.addItem(f"{label}", url)
            want = urls[n] if n < len(urls) else ""
            index = combo.findData(want)
            if index < 0 and want:
                combo.addItem(f"(登録にない) {want}", want)
                index = combo.count() - 1
            combo.setCurrentIndex(max(index, 0))

    def urls(self) -> list[str]:
        result = []
        for combo in self.boxes:
            url = str(combo.currentData() or "")
            if url and url.lower() not in [u.lower() for u in result]:
                result.append(url)
        return result

    def off(self) -> set:
        """「使う」を外した台 (休み) の URL。"""
        return {str(c.currentData()) for c, u in zip(self.boxes, self.uses) if c.currentData() and not u.isChecked()}

    def _edit(self) -> None:
        current = self.urls()
        ComfySettingsDialog(self, "接続先").exec()
        self._fill(current)

    def _test(self) -> None:
        lines = []
        off = {u.lower() for u in self.off()}
        for url in self.urls():
            if url.lower() in off:
                lines.append(f"{url}: 休み")
                continue
            error = comfy.test_connection(url)
            lines.append(f"{url}: " + ("OK" if error is None else "繋がりません"))
        self.result.setText("\n".join(lines) or "台が選ばれていません。")
        self.result.setStyleSheet(f"color: {theme.TEXT_DIM};")


def ask_text(parent, title: str, label: str, text: str = "") -> Optional[str]:
    from PySide6.QtWidgets import QInputDialog
    value, ok = QInputDialog.getText(parent, title, label, text=text)
    return value.strip() if ok and value.strip() else None
