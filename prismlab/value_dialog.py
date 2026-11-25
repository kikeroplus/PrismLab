"""生成のステップの「値を変える」(2026-11-25 利用者の希望)。

モデル・LoRA・サイズ (megapixels)・サンプラー・フォルダなど、値だけが違うワークフローを何本も作らずに済むように、
送るときにワークフローの中の値を差し替える (ファイルは書き換えない。GenCommon の comfy.value_slots / apply_values)。
"""

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
                               QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QSpinBox, QStackedWidget,
                               QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from gencommon import comfy

from . import recipes

BAD_COLOR = "#ef5350"
# よく変えるもの (「よく変えるものだけ」で出す): 名前で選ぶもの (モデル・LoRA・サンプラー)・強さ・サイズ・回数・フォルダ
COMMON_KEYS = ("megapixels", "width", "height", "steps", "cfg", "scheduler", "denoise", "folder_path", "directory",
               "aspect_ratio", "length", "batch_size")


def is_common(slot: dict) -> bool:
    key = str(slot.get("input") or "")
    if key.endswith("_name") or key.startswith("strength") or key in COMMON_KEYS:
        return True
    # オン・オフのスイッチ (Boolean (Enable LoRA?) など)
    return isinstance(slot.get("value"), bool) and str(slot.get("class_type") or "").startswith("Primitive")


def show_value(value) -> str:
    if isinstance(value, bool):
        return "オン" if value else "オフ"
    text = str(value).replace("\n", " ⏎ ")
    return text if len(text) <= 80 else text[:77] + "..."


ASK_TIMEOUT = 4.0  # 1 台に聞いて待つ秒数 (止まっている台で長く待たないように)


def candidate_urls() -> list[tuple[str, str]]:
    """一覧を聞く台の順 (名前, URL): 量産ラインの台 (休みを除く) → 登録してあるほかの接続先。
    ワークフローとモデルは全台で同じ名前にそろえる決まり (generation-standard 2 章) なので、どの台に聞いてもよい。"""
    names = {p.value.rstrip("/").lower(): p.name for p in comfy.connections() if p.value}
    out: dict = {}
    for url in recipes.machines() + [p.value for p in comfy.connections() if p.value]:
        url = url.strip().rstrip("/")
        if url and url.lower() not in out:
            out[url.lower()] = (names.get(url.lower(), "") or url, url)
    return list(out.values()) or [(comfy.DEFAULT_URL, comfy.DEFAULT_URL)]


class ValueDialog(QDialog):
    """変える値を 1 つ選んで、値を決める。edit を渡すと、その値を直す (場所は変えない)。"""

    def __init__(self, workflow: dict, parent=None, edit: Optional[dict] = None, taken=()):
        super().__init__(parent)
        self.setWindowTitle("値を変える" + (" (直す)" if edit else ""))
        self.resize(760, 560)
        self.result_value: Optional[dict] = None
        self._edit = edit
        self._taken = {tuple(t) for t in taken}  # もう変えている場所 (同じ場所を 2 つ足さない)
        self._slots = comfy.value_slots(workflow)
        box = QVBoxLayout(self)

        top = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("探す (ノードの名前・入力の名前・値)  例: lora, unet, megapixels")
        self.common = QCheckBox("よく変えるものだけ")
        self.common.setChecked(edit is None)
        top.addWidget(self.search, 1)
        top.addWidget(self.common)
        box.addLayout(top)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["ノード", "入力", "ワークフローの値"])
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(True)
        self.tree.setColumnWidth(0, 260)
        self.tree.setColumnWidth(1, 150)
        box.addWidget(self.tree, 1)

        self.where = QLabel()
        self.where.setWordWrap(True)
        box.addWidget(self.where)
        row = QHBoxLayout()
        row.addWidget(QLabel("変える値"))
        self.stack = QStackedWidget()
        self.text = QComboBox()
        self.text.setEditable(True)
        self.text.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.number = QDoubleSpinBox()
        self.number.setRange(-1e9, 1e9)
        self.number.setDecimals(4)
        self.integer = QSpinBox()
        self.integer.setRange(-2_000_000_000, 2_000_000_000)
        self.switch = QCheckBox("オン")
        for w in (self.text, self.number, self.integer, self.switch):
            self.stack.addWidget(w)
        row.addWidget(self.stack, 1)
        self.choices = QPushButton("ComfyUI から一覧を読む")
        self.choices.setToolTip("ComfyUI に、この入力で選べるもの (モデル・LoRA・サンプラーなど) を聞いて一覧に入れます。")
        self.choices.clicked.connect(self._load_choices)
        row.addWidget(self.choices)
        box.addLayout(row)
        self.choice_note = QLabel()
        self.choice_note.setStyleSheet("color: #9aa0a6;")
        box.addWidget(self.choice_note)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        box.addWidget(buttons)

        self.search.textChanged.connect(self._fill)
        self.common.toggled.connect(self._fill)
        self.tree.currentItemChanged.connect(lambda *_: self._picked())
        self.tree.itemDoubleClicked.connect(lambda *_: self.stack.currentWidget().setFocus())
        self._fill()
        if edit is not None:
            self.search.setEnabled(False)
            self.common.setEnabled(False)
            self.tree.setEnabled(False)

    # ------------------------------------------------------------------
    def _fill(self) -> None:
        words = self.search.text().strip().lower().split()
        keep = self._edit
        self.tree.clear()
        for slot in self._slots:
            here = (slot["node"], slot["input"])
            if keep is not None:
                if here != (keep.get("node"), keep.get("input")):
                    continue
            else:
                if here in self._taken:
                    continue
                if self.common.isChecked() and not words and not is_common(slot):
                    continue
                hay = " ".join([slot["title"], slot["class_type"], slot["input"], str(slot["value"])]).lower()
                if not all(w in hay for w in words):
                    continue
            name = slot["title"] or slot["class_type"]
            item = QTreeWidgetItem([f"{name}  ({slot['class_type']} #{slot['node']})" if slot["title"] else
                                    f"{name}  (#{slot['node']})", slot["input"], show_value(slot["value"])])
            item.setData(0, Qt.ItemDataRole.UserRole, slot)
            item.setToolTip(2, str(slot["value"]))
            self.tree.addTopLevelItem(item)
        if self.tree.topLevelItemCount():
            self.tree.setCurrentItem(self.tree.topLevelItem(0))
        self._picked()

    def _slot(self) -> Optional[dict]:
        item = self.tree.currentItem()
        return item.data(0, Qt.ItemDataRole.UserRole) if item is not None else None

    def _picked(self) -> None:
        slot = self._slot()
        self.choice_note.setText("")
        if slot is None:
            self.where.setText("変える値を選んでください。" if self._slots else "このワークフローには、変えられる値がありません。")
            self.stack.setEnabled(False)
            self.choices.setEnabled(False)
            return
        self.stack.setEnabled(True)
        self.where.setText(f"{comfy.value_label(slot)}  (ワークフローの値: {show_value(slot['value'])})")
        current = self._edit.get("value") if self._edit is not None else slot["value"]
        base = slot["value"]
        if isinstance(base, bool):
            self.stack.setCurrentWidget(self.switch)
            self.switch.setChecked(bool(current))
        elif isinstance(base, int):
            self.stack.setCurrentWidget(self.integer)
            self.integer.setValue(int(current) if isinstance(current, (int, float)) else base)
        elif isinstance(base, float):
            self.stack.setCurrentWidget(self.number)
            self.number.setValue(float(current) if isinstance(current, (int, float)) else base)
        else:
            self.stack.setCurrentWidget(self.text)
            self.text.clear()
            self.text.setEditText(str(current))
        self.choices.setEnabled(isinstance(base, str))

    def _load_choices(self) -> None:
        """繋がる台を順に探して、最初に答えた台の一覧を使う (2026-11-25 利用者の希望)。"""
        slot = self._slot()
        if slot is None:
            return
        tried = []
        options, used = None, None
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            for name, url in candidate_urls():
                self.choice_note.setText(f"{name} に聞いています...")
                self.choice_note.setStyleSheet("color: #9aa0a6;")
                QApplication.processEvents()
                try:
                    options = comfy.input_choices(url, slot["class_type"], slot["input"], timeout=ASK_TIMEOUT)
                except comfy.ComfyError:
                    tried.append(name)
                    continue
                used = name
                break
        finally:
            QApplication.restoreOverrideCursor()
        if used is None:
            self.choice_note.setText("どの台の ComfyUI にも繋がりません (" + "・".join(tried) + ")。"
                                     "どれかの台で ComfyUI を起動してから押してください。")
            self.choice_note.setStyleSheet(f"color: {BAD_COLOR};")
            return
        self.choice_note.setStyleSheet("color: #9aa0a6;")
        skipped = f" ({'・'.join(tried)} は繋がらないので飛ばしました)" if tried else ""
        if not options:
            self.choice_note.setText(f"この入力は一覧から選ぶ形ではありません (値を書いてください)。{used} に聞きました。")
            return
        now = self.text.currentText()
        self.text.clear()
        self.text.addItems(options)
        self.text.setEditText(now)
        self.choice_note.setText(f"{len(options)} 件 ({used} から){skipped}。▼ で選べます。")
        self.text.showPopup()

    def _value(self, base):
        if isinstance(base, bool):
            return self.switch.isChecked()
        if isinstance(base, int):
            return self.integer.value()
        if isinstance(base, float):
            return self.number.value()
        return self.text.currentText()

    def _accept(self) -> None:
        slot = self._slot()
        if slot is None:
            QMessageBox.warning(self, "値を変える", "変える値を選んでください。")
            return
        self.result_value = {"node": slot["node"], "input": slot["input"], "value": self._value(slot["value"]),
                             "class_type": slot["class_type"], "title": slot["title"]}
        self.accept()


class ValuesBox(QWidget):
    """生成のステップの設定に置く「値を変える」の一覧 (追加... / 直す... / 外す)。"""

    def __init__(self, values: list, parent=None):
        super().__init__(parent)
        self.values = [dict(v) for v in values]
        self._wf: Optional[dict] = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["ノード / 入力", "ワークフローの値", "変える値"])
        self.tree.setRootIsDecorated(False)
        self.tree.setColumnWidth(0, 230)
        self.tree.setColumnWidth(1, 150)
        self.tree.setMinimumHeight(110)
        self.tree.itemDoubleClicked.connect(lambda *_: self._edit())
        lay.addWidget(self.tree)
        row = QHBoxLayout()
        self.add_button = QPushButton("追加...")
        self.edit_button = QPushButton("直す...")
        self.remove_button = QPushButton("外す")
        self.add_button.clicked.connect(self._add)
        self.edit_button.clicked.connect(self._edit)
        self.remove_button.clicked.connect(self._remove)
        for b in (self.add_button, self.edit_button, self.remove_button):
            row.addWidget(b)
        row.addStretch(1)
        lay.addLayout(row)
        self.note = QLabel()
        self.note.setWordWrap(True)
        lay.addWidget(self.note)
        self.tree.currentItemChanged.connect(lambda *_: self._buttons())
        self.set_workflow(None)

    def set_workflow(self, wf: Optional[dict]) -> None:
        """選んでいるワークフロー (読めなければ None)。見つからない場所を赤で出す。"""
        self._wf = wf
        self._fill()

    def problems(self) -> list[str]:
        return comfy.value_problems(self._wf, self.values) if self._wf is not None else []

    def _fill(self) -> None:
        keep = self.tree.currentIndex().row()
        self.tree.clear()
        slots = {(s["node"], s["input"]): s for s in comfy.value_slots(self._wf)} if self._wf is not None else {}
        bad = set(self.problems())
        for v in self.values:
            label = comfy.value_label(v)
            slot = slots.get((v["node"], v["input"]))
            missing = label in bad
            item = QTreeWidgetItem([label, "(見つかりません)" if missing else
                                    show_value(slot["value"]) if slot else "", show_value(v.get("value"))])
            item.setToolTip(2, str(v.get("value")))
            if missing:
                for c in range(3):
                    item.setForeground(c, QBrush(QColor(BAD_COLOR)))
            self.tree.addTopLevelItem(item)
        if self.values:
            self.tree.setCurrentItem(self.tree.topLevelItem(max(0, min(keep, len(self.values) - 1))))
        if bad:
            self.note.setText("⚠ 赤の行は、このワークフローに場所がありません (ワークフローを変えた・作り直した)。"
                              "外すか、追加... で選び直してください。このままでは始められません。")
            self.note.setStyleSheet(f"color: {BAD_COLOR};")
        else:
            self.note.setText("送るときに、ワークフローの中の値をこの値に変えます (ファイルは書き換えません)。"
                              "モデル・LoRA・サイズなどだけが違うワークフローを、1 本にまとめられます。")
            self.note.setStyleSheet("color: #9aa0a6;")
        self._buttons()

    def _buttons(self) -> None:
        self.add_button.setEnabled(self._wf is not None)
        has = self.tree.currentItem() is not None
        row = self.tree.currentIndex().row()
        ok = has and self._wf is not None and 0 <= row < len(self.values) and \
            comfy.value_label(self.values[row]) not in set(self.problems())
        self.edit_button.setEnabled(ok)
        self.remove_button.setEnabled(has)

    def _add(self) -> None:
        if self._wf is None:
            return
        dialog = ValueDialog(self._wf, self, taken=[(v["node"], v["input"]) for v in self.values])
        if dialog.exec() and dialog.result_value is not None:
            self.values.append(dialog.result_value)
            self._fill()
            self.tree.setCurrentItem(self.tree.topLevelItem(len(self.values) - 1))

    def _edit(self) -> None:
        row = self.tree.currentIndex().row()
        if self._wf is None or not 0 <= row < len(self.values) or not self.edit_button.isEnabled():
            return
        dialog = ValueDialog(self._wf, self, edit=self.values[row])
        if dialog.exec() and dialog.result_value is not None:
            self.values[row] = dialog.result_value
            self._fill()

    def _remove(self) -> None:
        row = self.tree.currentIndex().row()
        if 0 <= row < len(self.values):
            del self.values[row]
            self._fill()
