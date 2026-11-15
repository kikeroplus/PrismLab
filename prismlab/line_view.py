"""量産ラインの画面。

上から: レシピの札 / 段の札の並び (入口 → 段 → …) / 出力フォルダ / 台の札 / 開始・一時停止・中止 / 結果。
量産が動いているあいだは、始めたときの段の写しを見せ、レシピは直せない。
"""

import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (QApplication, QButtonGroup, QComboBox, QFileDialog, QHBoxLayout, QLabel, QListView,
                               QListWidget, QListWidgetItem, QMenu, QMessageBox, QPushButton, QScrollArea,
                               QVBoxLayout, QWidget)

from gencommon import comfy

from . import engine, kinds, rating, recipes, theme
from .dialogs import MachinesDialog, StageDialog, ask_text
from .thumbs import ThumbCache
from .widgets import AddButton, Card, MachineCard, arrow, io_html

CONFIRM_COUNT = 30  # これを超える件数で始めるときは確かめる (generation-standard 6 章)
RESULT_ICON = QSize(128, 128)
STAR_ITEMS = ["★の絞り込みなし"] + [f"★{n} 以上" for n in range(1, 6)]


def _fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    h, m = seconds // 3600, seconds % 3600 // 60
    return f"{h}時間{m}分" if h else f"{max(m, 1)}分"


def find_jrate() -> Optional[Path]:
    """J-Rate の EXE (デスクトップの Application か、隣の J-Rate のフォルダ)。"""
    here = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve()
    candidates = [Path.home() / "Desktop" / "Application" / "J-Rate.exe"]
    for parent in here.parents:
        candidates.append(parent / "J-Rate" / "J-Rate.exe")
    return next((p for p in candidates if p.is_file()), None)


class _Scan:
    """入口の対象 (フォルダの画像を★で絞ったもの) を裏で数える。"""

    def __init__(self, done):
        self._done = done
        self._token = 0

    def start(self, folder: str, min_stars: int):
        self._token += 1
        token = self._token

        def work():
            files = recipes.list_images(folder) if folder else []
            if min_stars:
                files = [p for p in files if rating.get_stars(p) >= min_stars]
            self._done(token, folder, min_stars, files)

        threading.Thread(target=work, daemon=True).start()
        return token

    def current(self, token: int) -> bool:
        return token == self._token


class LineView(QWidget):
    status = Signal(str)  # ステータスバーに出す文
    _scanned = Signal(int, str, int, list)

    def __init__(self, eng: engine.Engine, parent=None):
        super().__init__(parent)
        self.engine = eng
        self.recipes = recipes.load()
        if not self.recipes:
            self.recipes = recipes.default_recipes()
        self.index = min(max(recipes.last_index(), 0), len(self.recipes) - 1)
        self.thumbs = ThumbCache(160, self)
        self.thumbs.changed.connect(lambda _p: self._schedule())
        self._inputs: list[Path] = []
        self._input_key = None
        self._scan = _Scan(lambda *a: self._scanned.emit(*a))
        self._scanned.connect(self._on_scanned)
        self._results_key = None
        self._result_items: dict = {}

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 8, 10, 8)
        root.setSpacing(8)

        # レシピの札
        self.recipe_row = QHBoxLayout()
        self.recipe_row.setSpacing(6)
        self.recipe_group = QButtonGroup(self)
        self.recipe_group.setExclusive(True)
        self.recipe_group.idClicked.connect(self._select_recipe)
        recipe_host = QWidget()
        recipe_host.setLayout(self.recipe_row)
        top = QHBoxLayout()
        top.addWidget(recipe_host)
        top.addStretch(1)
        root.addLayout(top)

        # 段の並び
        self.flow_host = QWidget()
        self.flow = QHBoxLayout(self.flow_host)
        self.flow.setContentsMargins(2, 2, 2, 2)
        self.flow.setSpacing(0)
        scroll = QScrollArea()
        scroll.setWidget(self.flow_host)
        scroll.setWidgetResizable(True)
        scroll.setFixedHeight(232)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        root.addWidget(scroll)

        # 出力フォルダ
        out_row = QHBoxLayout()
        self.out_label = QLabel()
        self.out_label.setStyleSheet(f"color: {theme.TEXT_DIM};")
        self.out_change = QPushButton("変更...")
        self.out_change.clicked.connect(self._change_output)
        self.out_reset = QPushButton("既定に戻す")
        self.out_reset.clicked.connect(self._reset_output)
        out_row.addWidget(self.out_label, 1)
        out_row.addWidget(self.out_change)
        out_row.addWidget(self.out_reset)
        root.addLayout(out_row)

        # 台
        mrow = QHBoxLayout()
        mrow.setSpacing(8)
        self.machine_cards = [MachineCard() for _ in range(recipes.MAX_MACHINES)]
        for card in self.machine_cards:
            mrow.addWidget(card, 1)
        self.machines_button = QPushButton("台の設定...")
        self.machines_button.clicked.connect(self._edit_machines)
        mrow.addWidget(self.machines_button)
        root.addLayout(mrow)

        # 開始・一時停止・中止
        crow = QHBoxLayout()
        self.start_button = QPushButton("▶ 開始")
        self.start_button.setStyleSheet(f"QPushButton {{ background: {theme.ACCENT}; color: black; font-weight: bold;"
                                        " padding: 6px 18px; } QPushButton:disabled { background: #3a3a3a; color: #777; }")
        self.start_button.clicked.connect(self._start)
        self.pause_button = QPushButton("一時停止")
        self.pause_button.clicked.connect(self._toggle_pause)
        self.cancel_button = QPushButton("中止")
        self.cancel_button.clicked.connect(self._cancel)
        self.retry_button = QPushButton("失敗したものをやり直す")
        self.retry_button.clicked.connect(self._retry)
        self.summary = QLabel()
        self.summary.setStyleSheet("font-size: 13px;")
        for w in (self.start_button, self.pause_button, self.cancel_button, self.retry_button):
            crow.addWidget(w)
        crow.addSpacing(12)
        crow.addWidget(self.summary, 1)
        root.addLayout(crow)

        # 結果
        rhead = QHBoxLayout()
        self.results_title = QLabel("結果")
        self.results_title.setStyleSheet("font-weight: bold;")
        self.open_folder = QPushButton("出力フォルダを開く")
        self.open_folder.clicked.connect(self._open_output)
        self.open_jrate = QPushButton("J-Rate で開く")
        self.open_jrate.clicked.connect(self._open_in_jrate)
        rhead.addWidget(self.results_title)
        rhead.addStretch(1)
        rhead.addWidget(self.open_folder)
        rhead.addWidget(self.open_jrate)
        root.addLayout(rhead)
        self.results = QListWidget()
        self.results.setViewMode(QListView.ViewMode.IconMode)
        self.results.setIconSize(RESULT_ICON)
        self.results.setGridSize(QSize(RESULT_ICON.width() + 16, RESULT_ICON.height() + 34))
        self.results.setResizeMode(QListView.ResizeMode.Adjust)
        self.results.setMovement(QListView.Movement.Static)
        self.results.setWordWrap(True)
        self.results.setUniformItemSizes(True)
        self.results.itemDoubleClicked.connect(self._open_result)
        root.addWidget(self.results, 1)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(300)
        self._timer.timeout.connect(self.refresh)
        self.engine.changed.connect(self._schedule)
        self.engine.finished.connect(self._on_finished)

        self._build_recipe_chips()
        self.refresh(rebuild=True)

    # ------------------------------------------------------------------
    # 状態
    # ------------------------------------------------------------------
    @property
    def recipe(self) -> recipes.Recipe:
        return self.recipes[self.index]

    def running(self) -> bool:
        run = self.engine.run
        return self.engine.active and run is not None and run.unfinished > 0

    def _schedule(self) -> None:
        if not self._timer.isActive():
            self._timer.start()

    def _save_recipes(self) -> None:
        try:
            recipes.save(self.recipes)
            recipes.set_last_index(self.index)
        except OSError as exc:
            self.status.emit(f"レシピを保存できません: {exc}")

    # ------------------------------------------------------------------
    # レシピの札
    # ------------------------------------------------------------------
    def _build_recipe_chips(self) -> None:
        while self.recipe_row.count():
            w = self.recipe_row.takeAt(0).widget()
            if w is None:
                continue
            if isinstance(w, QPushButton):
                self.recipe_group.removeButton(w)
            w.deleteLater()
        for i, r in enumerate(self.recipes):
            b = QPushButton(r.name or "(名前なし)")
            b.setCheckable(True)
            b.setChecked(i == self.index)
            b.setStyleSheet("QPushButton { border-radius: 12px; padding: 4px 14px; }"
                            f"QPushButton:checked {{ background: {theme.ACCENT}; color: black; font-weight: bold; }}")
            b.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            b.customContextMenuRequested.connect(lambda pos, n=i, btn=b: self._recipe_menu(n, btn.mapToGlobal(pos)))
            self.recipe_group.addButton(b, i)
            self.recipe_row.addWidget(b)
        add = QPushButton("＋ レシピ")
        add.setStyleSheet("QPushButton { border-radius: 12px; padding: 4px 12px; border-style: dashed; }")
        add.clicked.connect(self._add_recipe)
        self.recipe_row.addWidget(add)

    def _select_recipe(self, index: int) -> None:
        if self.running():
            return
        self.index = index
        recipes.set_last_index(index)
        self.refresh(rebuild=True)

    def _recipe_menu(self, index: int, pos) -> None:
        if self.running():
            return
        menu = QMenu(self)
        rename = menu.addAction("名前を変える...")
        dup = menu.addAction("複製する")
        menu.addSeparator()
        left = menu.addAction("◀ 左へ")
        right = menu.addAction("右へ ▶")
        left.setEnabled(index > 0)
        right.setEnabled(index < len(self.recipes) - 1)
        menu.addSeparator()
        delete = menu.addAction("削除する...")
        delete.setEnabled(len(self.recipes) > 1)
        chosen = menu.exec(pos)
        if chosen is rename:
            name = ask_text(self, "名前を変える", "レシピの名前", self.recipes[index].name)
            if name:
                self.recipes[index].name = name
        elif chosen is dup:
            copy = self.recipes[index].copy()
            copy.name += " のコピー"
            self.recipes.insert(index + 1, copy)
            self.index = index + 1
        elif chosen in (left, right):
            j = index - 1 if chosen is left else index + 1
            self.recipes[index], self.recipes[j] = self.recipes[j], self.recipes[index]
            self.index = j
        elif chosen is delete:
            if QMessageBox.question(self, "レシピの削除", f"レシピ「{self.recipes[index].name}」を削除しますか?",
                                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                    QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
                return
            del self.recipes[index]
            self.index = min(self.index, len(self.recipes) - 1)
        else:
            return
        self._save_recipes()
        self._build_recipe_chips()
        self.refresh(rebuild=True)

    def _add_recipe(self) -> None:
        if self.running():
            return
        name = ask_text(self, "レシピを足す", "レシピの名前")
        if not name:
            return
        self.recipes.append(recipes.Recipe(name, folder=self.recipe.folder, stages=[recipes.Stage(recipes.GEN)]))
        self.index = len(self.recipes) - 1
        self._save_recipes()
        self._build_recipe_chips()
        self.refresh(rebuild=True)

    # ------------------------------------------------------------------
    # 段の並び
    # ------------------------------------------------------------------
    def _stage_views(self) -> list[dict]:
        """見せる段: 動いている量産なら始めたときの写し、そうでなければ選んでいるレシピ。"""
        if self.running():
            views = []
            for s in self.engine.run.stages:
                v = {"type": s["type"]}
                if s["type"] == recipes.READ:
                    v["detail"] = s["reader"].get("name", "")
                elif s["type"] == recipes.ENHANCE:
                    v["detail"] = s["enhancer"].get("name", "")
                else:
                    v["kind"] = s.get("kind", "")
                    v["detail"] = s.get("workflow_name", "")
                    v["seconds"] = s.get("seconds")
                views.append(v)
            return views
        views = []
        for s in self.recipe.stages:
            v = {"type": s.type}
            if s.type == recipes.READ:
                v["detail"] = s.reader or "(読み取りプリセットを選んでください)"
            elif s.type == recipes.ENHANCE:
                v["detail"] = s.enhancer or "(エンハンサーを選んでください)"
            else:
                v["kind"] = kinds.kind_of(s.workflow)
                names = [p.name for p in comfy.workflows() if s.workflow and p.value.lower() == s.workflow.lower()]
                v["detail"] = (names[0] if names and names[0] else Path(s.workflow).stem) if s.workflow \
                    else "(ワークフローを選んでください)"
                v["seconds"] = s.seconds
            views.append(v)
        return views

    def _rebuild_flow(self) -> None:
        while self.flow.count():
            w = self.flow.takeAt(0).widget()
            if w is not None:
                w.deleteLater()
        self.entry = Card()
        self.entry.title.setText("入口")
        self.entry.chip.set("画像", kinds.IMAGE_COLOR)
        self.entry.set_color(kinds.IMAGE_COLOR)
        self.entry.clicked.connect(self._choose_folder)
        self.entry.bar.hide()
        self.star_box = QComboBox()
        self.star_box.addItems(STAR_ITEMS)
        self.star_box.setCurrentIndex(self.recipe.min_stars)
        self.star_box.currentIndexChanged.connect(self._stars_changed)
        self.entry.box.insertWidget(2, self.star_box)
        self.flow.addWidget(self.entry)

        self.stage_cards: list[Card] = []
        views = self._stage_views()
        prev_out = ("画像", kinds.IMAGE_COLOR)
        has_text = False
        for i, v in enumerate(views):
            self.flow.addWidget(arrow())
            card = Card()
            card.title.setText(recipes.STAGE_TITLES[v["type"]])
            card.detail.setText(v["detail"] + (f"  ・ {v['seconds']:g} 秒" if v.get("seconds") else ""))
            if v["type"] == recipes.GEN:
                kind = v.get("kind", "")
                color = kinds.color_of(kind)
                card.chip.set(kind or "?", color)
                card.set_color(color)
                ins = [prev_out] if kind.startswith("I") or not kind else []
                if has_text:
                    ins.append(("文字", kinds.TEXT_COLOR))
                out = ("動画", color) if kinds.is_video(kind) else ("画像", color)
                parts = []
                for n, p in enumerate(ins):
                    if n:
                        parts.append("+")
                    parts.append(p)
                card.io.setText(io_html(parts + ["→", out]) if parts else io_html(["→", out]))
                prev_out, has_text = out, False
            else:
                card.chip.set("LLM", kinds.LLM_LINE)
                card.set_color(kinds.LLM_LINE)
                if v["type"] == recipes.READ:
                    card.io.setText(io_html([prev_out, "→", ("文字", kinds.TEXT_COLOR)]))
                else:
                    card.io.setText(io_html([("文字", kinds.TEXT_COLOR), "→", ("文字", kinds.TEXT_COLOR)]))
                has_text = True
                card.thumbs.hide()
            card.clicked.connect(lambda n=i: self._edit_stage(n))
            card.menu_requested.connect(lambda pos, n=i: self._stage_menu(n, pos))
            self.flow.addWidget(card)
            self.stage_cards.append(card)

        self.flow.addWidget(arrow())
        self.add_stage = AddButton("＋ 段")
        menu = QMenu(self.add_stage)
        for t in (recipes.READ, recipes.ENHANCE, recipes.GEN):
            menu.addAction(recipes.STAGE_TITLES[t], lambda t=t: self._add_stage(t))
        self.add_stage.setMenu(menu)
        self.flow.addWidget(self.add_stage)
        self.flow.addStretch(1)

    def _edit_stage(self, index: int) -> None:
        if self.running():
            self.status.emit("量産が動いているあいだは段を直せません (一時停止しても、始めたときの中身で続けます)。")
            return
        stage = self.recipe.stages[index]
        edited = recipes.Stage.from_dict(stage.to_dict())
        if StageDialog(edited, self).exec():
            self.recipe.stages[index] = edited
            self._save_recipes()
            self.refresh(rebuild=True)

    def _stage_menu(self, index: int, pos) -> None:
        if self.running():
            return
        menu = QMenu(self)
        edit = menu.addAction("設定...")
        left = menu.addAction("◀ 左へ")
        right = menu.addAction("右へ ▶")
        left.setEnabled(index > 0)
        right.setEnabled(index < len(self.recipe.stages) - 1)
        menu.addSeparator()
        delete = menu.addAction("この段を外す")
        chosen = menu.exec(pos)
        stages = self.recipe.stages
        if chosen is edit:
            self._edit_stage(index)
            return
        if chosen in (left, right):
            j = index - 1 if chosen is left else index + 1
            stages[index], stages[j] = stages[j], stages[index]
        elif chosen is delete:
            del stages[index]
        else:
            return
        self._save_recipes()
        self.refresh(rebuild=True)

    def _add_stage(self, stage_type: str) -> None:
        if self.running():
            return
        stage = recipes.Stage(stage_type)
        if StageDialog(stage, self).exec():
            self.recipe.stages.append(stage)
            self._save_recipes()
            self.refresh(rebuild=True)

    # ------------------------------------------------------------------
    # 入口・出力
    # ------------------------------------------------------------------
    def _choose_folder(self) -> None:
        if self.running():
            return
        folder = QFileDialog.getExistingDirectory(self, "入口のフォルダを選ぶ", self.recipe.folder or "")
        if folder:
            self.recipe.folder = str(Path(folder))
            self._save_recipes()
            self.refresh(rebuild=True)

    def _stars_changed(self, index: int) -> None:
        if self.running():
            return
        self.recipe.min_stars = index
        self._save_recipes()
        self.refresh()

    def _change_output(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "出力フォルダを選ぶ", self.recipe.resolved_output_dir() or "")
        if folder:
            self.recipe.output_dir = str(Path(folder))
            self._save_recipes()
            self.refresh()

    def _reset_output(self) -> None:
        self.recipe.output_dir = ""
        self._save_recipes()
        self.refresh()

    def _on_scanned(self, token: int, folder: str, min_stars: int, files: list) -> None:
        if not self._scan.current(token):
            return
        self._inputs = files
        self.refresh()

    def _output_dir(self) -> str:
        if self.engine.run is not None and (self.running() or not self.recipe.folder):
            return self.engine.run.output_dir
        return self.recipe.resolved_output_dir()

    # ------------------------------------------------------------------
    # 台
    # ------------------------------------------------------------------
    def _edit_machines(self) -> None:
        dialog = MachinesDialog(recipes.machines(), self)
        if dialog.exec():
            urls = dialog.urls()
            recipes.set_machines(urls)
            self.engine.set_machines(urls)

    # ------------------------------------------------------------------
    # 開始・一時停止・中止
    # ------------------------------------------------------------------
    def _start(self) -> None:
        r = self.recipe
        if not r.folder or not Path(r.folder).is_dir():
            QMessageBox.warning(self, "開始", "入口のフォルダを選んでください (入口の札をクリック)。")
            return
        if not r.stages:
            QMessageBox.warning(self, "開始", "段がありません。「＋ 段」で足してください。")
            return
        if any(s.type == recipes.GEN for s in r.stages) and not self.engine.machines:
            QMessageBox.warning(self, "開始", "使う台が決まっていません。「台の設定...」で選んでください。")
            return
        self.setCursor(Qt.CursorShape.WaitCursor)
        try:
            files = recipes.list_images(r.folder)
            if r.min_stars:
                files = [p for p in files if rating.get_stars(p) >= r.min_stars]
        finally:
            self.unsetCursor()
        if not files:
            QMessageBox.warning(self, "開始", "入口に対象の画像がありません (★の絞り込みも確かめてください)。")
            return
        try:
            run = engine.Run.create(r.copy(), files)
        except (ValueError, comfy.ComfyError) as exc:
            QMessageBox.warning(self, "開始", str(exc))
            return
        if len(files) > CONFIRM_COUNT:
            if QMessageBox.question(self, "開始", f"{len(files)} 枚を「{r.name}」で量産します。始めますか?",
                                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                    QMessageBox.StandardButton.Yes) != QMessageBox.StandardButton.Yes:
                return
        self.engine.start(run)
        self.status.emit(f"「{r.name}」を始めました ({len(files)} 枚)。")
        self.refresh(rebuild=True)

    def _toggle_pause(self) -> None:
        run = self.engine.run
        if run is None:
            return
        self.engine.set_paused(not run.paused)
        if run.paused:
            self.status.emit("一時停止しました。実行中のものが終わったら止まります。")

    def _cancel(self) -> None:
        run = self.engine.run
        if run is None or run.unfinished == 0:
            return
        running = sum(1 for i in run.items if i.state == engine.RUNNING)
        text = f"残りの {run.unfinished} 件を取り消しますか?"
        if running:
            text += f"\n実行中のもの ({running} 件) は ComfyUI に中断を送ります。"
        if QMessageBox.question(self, "中止", text, QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
            self.engine.cancel_all()

    def _retry(self) -> None:
        n = self.engine.retry_failed()
        if n:
            self.status.emit(f"失敗した {n} 件をやり直します。")
            self.refresh(rebuild=True)

    def _on_finished(self) -> None:
        run = self.engine.run
        if run is None:
            return
        done = sum(1 for i in run.items if i.state == engine.DONE)
        failed = sum(1 for i in run.items if i.state == engine.FAILED)
        self.status.emit(f"「{run.name}」が終わりました: 完了 {done} 件" + (f"・失敗 {failed} 件" if failed else ""))
        window = self.window()
        if not window.isActiveWindow():
            QApplication.alert(window)
        self.refresh(rebuild=True)

    # ------------------------------------------------------------------
    # 結果
    # ------------------------------------------------------------------
    def _open_output(self) -> None:
        folder = self._output_dir()
        if folder and Path(folder).is_dir():
            os.startfile(folder)  # noqa: S606 - 利用者のフォルダをエクスプローラーで開く
        else:
            self.status.emit("出力フォルダはまだありません (生成すると作ります)。")

    def _open_in_jrate(self) -> None:
        folder = self._output_dir()
        if not folder or not Path(folder).is_dir():
            self.status.emit("出力フォルダはまだありません (生成すると作ります)。")
            return
        exe = find_jrate()
        if exe is None:
            self.status.emit("J-Rate が見つかりません (デスクトップの Application か、隣の J-Rate のフォルダに置いてください)。")
            return
        subprocess.Popen([str(exe), folder])  # noqa: S603 - 決まった EXE に出力フォルダを渡す

    def _open_result(self, item: QListWidgetItem) -> None:
        path = item.data(Qt.ItemDataRole.UserRole)
        if path and Path(path).exists():
            os.startfile(path)  # noqa: S606

    def _refresh_results(self, items: list) -> None:
        entries = []  # (時刻, パス, 失敗の理由, 見出し)
        for it in items:
            for o in it.outputs:
                entries.append((float(o.get("time") or 0), str(o["path"]), "", Path(o["path"]).name))
            if it.state == engine.FAILED:
                entries.append((float("inf"), it.current, it.error, "失敗: " + Path(it.source).name))
        entries.sort(key=lambda e: e[0], reverse=True)
        key = tuple((e[1], e[2]) for e in entries)
        if key != self._results_key:
            self._results_key = key
            self.results.clear()
            self._result_items = {}
            for _t, path, error, title in entries:
                item = QListWidgetItem(title)
                item.setData(Qt.ItemDataRole.UserRole, path)
                item.setData(Qt.ItemDataRole.UserRole + 1, error)
                item.setToolTip(error or path)
                if error:
                    item.setForeground(QColor("#ef5350"))
                self.results.addItem(item)
                self._result_items.setdefault(path.lower(), []).append(item)
        for path_key, list_items in self._result_items.items():
            for item in list_items:
                if not item.icon().isNull():
                    continue
                pix = self.thumbs.get(item.data(Qt.ItemDataRole.UserRole))
                if pix is not None:
                    item.setIcon(QIcon(self._framed(pix, bool(item.data(Qt.ItemDataRole.UserRole + 1)))))
        done = sum(len(i.outputs) for i in items)
        self.results_title.setText(f"結果 ({done})")

    @staticmethod
    def _framed(pix: QPixmap, failed: bool) -> QPixmap:
        canvas = QPixmap(RESULT_ICON)
        canvas.fill(Qt.GlobalColor.transparent)
        p = QPainter(canvas)
        if not pix.isNull():
            scaled = pix.scaled(RESULT_ICON - QSize(8, 8), Qt.AspectRatioMode.KeepAspectRatio,
                                Qt.TransformationMode.SmoothTransformation)
            p.drawPixmap((RESULT_ICON.width() - scaled.width()) // 2, (RESULT_ICON.height() - scaled.height()) // 2,
                         scaled)
        if failed:
            p.setPen(QPen(QColor("#ef5350"), 4))
            p.drawRect(2, 2, RESULT_ICON.width() - 4, RESULT_ICON.height() - 4)
        p.end()
        return canvas

    # ------------------------------------------------------------------
    # 描き直し
    # ------------------------------------------------------------------
    def refresh(self, rebuild: bool = False) -> None:
        run, items, machines = self.engine.snapshot()
        running = self.running()
        mode = (running, id(run), self.index)
        if rebuild or mode != getattr(self, "_flow_mode", None):
            self._flow_mode = mode
            self._rebuild_flow()
        # 段の札に数を出すのは、動いている量産か、見ているレシピで最後に回した量産のとき
        show_stats = running or (run is not None and run.name == self.recipe.name
                                 and len(run.stages) == len(self.stage_cards))
        for b in self.recipe_group.buttons():
            b.setEnabled(not running or self.recipe_group.id(b) == self.index)

        # 入口
        if running:
            sources = [Path(i.source) for i in items]
            folder = str(run.recipe.get("folder", ""))
            stars = int(run.recipe.get("min_stars") or 0)
        else:
            key = (self.recipe.folder, self.recipe.min_stars)
            if key != self._input_key:
                self._input_key = key
                self._inputs = []
                self._scan.start(*key)
            sources = self._inputs
            folder, stars = self.recipe.folder, self.recipe.min_stars
        self.star_box.blockSignals(True)
        self.star_box.setCurrentIndex(stars)
        self.star_box.setEnabled(not running)
        self.star_box.blockSignals(False)
        self.entry.detail.setText((folder or "クリックしてフォルダを選ぶ"))
        self.entry.detail.setToolTip(folder)
        self.entry.count.setText(f"対象 {len(sources)} 枚" if folder else "")
        self.entry.thumbs.set([self.thumbs.get(str(p)) for p in sources[:3]])

        # 段
        total = len(items)
        for i, card in enumerate(self.stage_cards):
            if not show_stats:
                card.bar.setValue(0)
                card.count.setText("")
                card.thumbs.set([])
                continue
            passed = sum(1 for it in items if it.stage > i)
            here = [it for it in items if it.stage == i and it.state == engine.RUNNING]
            failed = sum(1 for it in items if it.state == engine.FAILED and it.error_stage == i)
            card.bar.setMaximum(max(total, 1))
            card.bar.setValue(passed)
            text = f"済み {passed} / {total}"
            if here:
                text += f" ・ 実行中 {len(here)}"
            if failed:
                text += f" ・ 失敗 {failed}"
            card.count.setText(text)
            stage_type = run.stages[i]["type"]
            card.set_color(card._color, bool(here))
            if stage_type == recipes.GEN:
                outs = sorted((o for it in items for o in it.outputs if o.get("stage") == i),
                              key=lambda o: float(o.get("time") or 0), reverse=True)[:3]
                card.thumbs.set([self.thumbs.get(str(o["path"])) for o in outs])
            elif here:
                card.detail.setToolTip(here[0].status)

        # 出力
        out = self._output_dir()
        self.out_label.setText("出力フォルダ: " + (out or "(入口のフォルダを選ぶと決まります)")
                               + ("" if self.recipe.output_dir or running else "   (既定: 入口のフォルダ\\レシピ名)"))
        self.out_change.setEnabled(not running)
        self.out_reset.setEnabled(not running and bool(self.recipe.output_dir))

        # 台
        for n, card in enumerate(self.machine_cards):
            if n >= len(machines):
                card.setVisible(n == 0)
                if n == 0:
                    card.name.setText("台が決まっていません")
                    card.done.setText("")
                    card.status.setText("「台の設定...」で ComfyUI を選んでください")
                    card.set_down(False, "#777777")
                continue
            m = machines[n]
            card.setVisible(True)
            card.name.setText(m.label)
            card.done.setText(f"{m.done} 件")
            mine = [it for it in items if it.state == engine.RUNNING and it.at.lower() == m.url.lower()]
            if m.state == engine.DOWN:
                text = m.reason + "。残りはほかの台に回しています"
            elif m.state == engine.OFF:
                text = m.reason
            elif mine:
                text = " / ".join(f"{Path(it.current).name}: {it.status or '準備中'}" for it in mine)
            else:
                text = "待機中" if running else ""
            card.status.setText(text)
            card.set_down(m.state != engine.UP)

        # 開始・一時停止・中止
        self.start_button.setEnabled(not running)
        self.pause_button.setEnabled(running)
        self.pause_button.setText("再開" if run is not None and run.paused else "一時停止")
        self.cancel_button.setEnabled(running)
        failed_total = sum(1 for it in items if it.state == engine.FAILED)
        self.retry_button.setEnabled(failed_total > 0 and not (run is not None and run.paused))
        self.retry_button.setVisible(failed_total > 0)
        self.summary.setText(self._summary(run, items, running))
        self._refresh_results(items)

    def _summary(self, run, items, running: bool) -> str:
        if run is None:
            return ""
        total = len(items)
        done = sum(1 for i in items if i.state == engine.DONE)
        failed = sum(1 for i in items if i.state == engine.FAILED)
        canceled = sum(1 for i in items if i.state == engine.CANCELED)
        text = f"「{run.name}」 完了 {done} / {total}"
        if failed:
            text += f" ・ 失敗 {failed}"
        if canceled:
            text += f" ・ 取り消し {canceled}"
        if running:
            if run.paused:
                text = '<span style="color:#ff9800; font-weight:bold">一時停止中</span>  ' + text
            elif done:
                left = total - done - failed - canceled
                spent = time.time() - run.started
                text += f" ・ 残り 約 {_fmt_duration(spent / done * left)}"
            if self.engine.llm_status:
                text += f" ・ {self.engine.llm_status}"
        return text
