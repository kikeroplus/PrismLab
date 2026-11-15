"""PrismLab のウィンドウ: 「量産ライン」と「物語」のタブ、ステータスバー、閉じるときの確認、続きから始める。"""

import time

from PySide6.QtCore import QEvent
from PySide6.QtWidgets import QLabel, QMainWindow, QMessageBox, QPushButton, QTabWidget, QToolBar, QVBoxLayout, QWidget

from gencommon import enhancer, reader
from gencommon.comfy_dialogs import ComfySettingsDialog

from . import engine, recipes, settings, theme
from .line_view import LineView

AWAY_SECONDS = 1.0  # これより長くほかのウィンドウにいたら、戻ったときに入口の★を読み直す


class StoryPlaceholder(QWidget):
    """物語 (段階 3)。いまは何を作るかだけを見せる。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        box = QVBoxLayout(self)
        box.setContentsMargins(40, 40, 40, 40)
        title = QLabel("物語")
        title.setStyleSheet("font-size: 20px; font-weight: bold;")
        text = QLabel(
            "シーンを横に並べた絵コンテで、1 シーンずつ作ってつなぎます (段階 3 で作ります)。\n\n"
            "・つなぎ方は「続き」(前のシーンで採用した動画の最後のコマを元画像にする) と「シーンチェンジ」\n"
            "・シーンごとにプロンプト (読み取り → 手直し → 差し込み → 盛る) と候補。候補から 1 つを採用して次へ\n"
            "・「▶ 通して再生」で採用した動画を続けて再生。書き出しは動画編集ソフトで")
        text.setWordWrap(True)
        text.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 13px;")
        box.addWidget(title)
        box.addWidget(text)
        box.addStretch(1)


class MainWindow(QMainWindow):
    def __init__(self, notice: str = ""):
        super().__init__()
        self.setWindowTitle("PrismLab")
        self.engine = engine.Engine(self)
        self.engine.set_machines(recipes.machines())

        self.line = LineView(self.engine)
        self.tabs = QTabWidget()
        self.tabs.addTab(self.line, "量産ライン")
        self.tabs.addTab(StoryPlaceholder(), "物語")
        self.tabs.setStyleSheet("QTabBar::tab { padding: 6px 22px; font-size: 13px; }"
                                f"QTabBar::tab:selected {{ background: {theme.ACCENT}; color: black; font-weight: bold; }}")
        self.setCentralWidget(self.tabs)

        bar = QToolBar("設定")
        bar.setMovable(False)
        self.addToolBar(bar)
        for text, func in (("生成の設定", self._comfy_settings), ("読み取りの設定", self._reader_settings),
                           ("エンハンサーの設定", self._enhancer_settings)):
            button = QPushButton(text)
            button.clicked.connect(func)
            bar.addWidget(button)

        self.line.status.connect(self.show_status)
        self.statusBar().showMessage(notice or "準備ができました。", 8000)
        settings.restore_geometry(self)

    def changeEvent(self, event) -> None:
        # J-Rate などで★を付けて戻ってきたら、入口の★を読み直す (ダイアログを閉じただけのときは読まない)
        if event.type() == QEvent.Type.ActivationChange:
            if self.isActiveWindow():
                away = getattr(self, "_left_at", None)
                if away is not None and time.monotonic() - away >= AWAY_SECONDS:
                    self.line.rescan()
                self._left_at = None
            elif not any(w.isVisible() for w in self.findChildren(QMessageBox)):
                self._left_at = time.monotonic()
        super().changeEvent(event)

    def show_status(self, text: str) -> None:
        self.statusBar().showMessage(text, 15000)

    def bring_to_front(self) -> None:
        if self.isMinimized():
            self.showNormal()
        self.show()
        self.raise_()
        self.activateWindow()

    # 設定の窓 (GenCommon のもの。3 つのアプリと共有の登録) ---------------------
    def _comfy_settings(self) -> None:
        ComfySettingsDialog(self).exec()
        self.engine.set_machines(recipes.machines())  # 接続先の名前を変えたとき
        self.line.refresh(rebuild=True)

    def _reader_settings(self) -> None:
        reader.ReaderSettingsDialog(0, self).exec()
        self.line.refresh(rebuild=True)

    def _enhancer_settings(self) -> None:
        enhancer.EnhancerSettingsDialog(0, self).exec()
        self.line.refresh(rebuild=True)

    # 続きから始める ---------------------------------------------------------
    def offer_resume(self) -> None:
        run = engine.load_run()
        if run is None:
            return
        if run.unfinished == 0:
            self.engine.show(run)  # 前回の結果を見せる
            return
        if run.paused and not any(i.prompt_id for i in run.items if not i.finished):
            self.engine.start(run)  # 一時停止したまま閉じた: 聞かずに一時停止のまま戻す
            self.show_status(f"前回の「{run.name}」を一時停止のまま戻しました (待機 {run.unfinished} 件)。")
            return
        box = QMessageBox(self)
        box.setWindowTitle("続きから始める")
        box.setText(f"前回の量産「{run.name}」が終わっていません (残り {run.unfinished} 件)。")
        box.setInformativeText("続きから始めると、頼み済みのものはその台で結果を受け取ります。")
        resume = box.addButton("続きから始める", QMessageBox.ButtonRole.AcceptRole)
        discard = box.addButton("中断する", QMessageBox.ButtonRole.DestructiveRole)
        box.setEscapeButton(resume)  # うっかり捨てないように
        box.setDefaultButton(resume)
        box.exec()
        if box.clickedButton() is discard:
            self.engine.run = run
            self.engine.discard()
            self.engine.show(run)
            self.show_status("前回の量産を中断しました。")
        else:
            self.engine.start(run)
            self.show_status(f"前回の「{run.name}」を続けます (残り {run.unfinished} 件)。")

    # 閉じる -----------------------------------------------------------------
    def closeEvent(self, event) -> None:
        run = self.engine.run
        busy = run is not None and run.unfinished > 0 and self.engine.active
        stopped = busy and run.paused and not any(i.state == engine.RUNNING for i in run.items)
        if busy and not stopped:
            box = QMessageBox(self)
            box.setWindowTitle("閉じる")
            box.setText(f"量産「{run.name}」が終わっていません (残り {run.unfinished} 件)。")
            box.setInformativeText("残して閉じると、次に起動したときに続きから始められます。"
                                   "どちらで閉じても、ComfyUI で実行中のものは止まりません。")
            box.addButton("残して閉じる", QMessageBox.ButtonRole.AcceptRole)
            drop = box.addButton("捨てて閉じる", QMessageBox.ButtonRole.DestructiveRole)
            cancel = box.addButton("やめる", QMessageBox.ButtonRole.RejectRole)
            box.setEscapeButton(cancel)
            box.setDefaultButton(cancel)
            box.exec()
            clicked = box.clickedButton()
            if clicked is cancel:
                event.ignore()
                return
            if clicked is drop:
                self.engine.discard()
            else:  # 残して閉じる
                self.engine.stop_threads()
        else:
            self.engine.stop_threads()
        settings.save_geometry(self)
        event.accept()

