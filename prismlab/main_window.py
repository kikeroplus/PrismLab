"""PrismLab のウィンドウ: 「量産ライン」と「物語」のタブ、ステータスバー、閉じるときの確認、続きから始める。"""

import time

from PySide6.QtCore import QEvent, Signal
from PySide6.QtWidgets import QMainWindow, QMessageBox, QPushButton, QTabWidget, QToolBar

from gencommon import enhancer, reader
from gencommon.comfy_dialogs import ComfySettingsDialog

from . import engine, external, recipes, settings, theme
from .line_view import LineView
from .story_view import StoryView

AWAY_SECONDS = 1.0  # これより長くほかのウィンドウにいたら、戻ったときに入口の★を読み直す


class MainWindow(QMainWindow):
    closed_to_tray = Signal()  # 頼まれた生成が動いているので、閉じずに隠した

    def __init__(self, notice: str = ""):
        super().__init__()
        self.setWindowTitle("PrismLab")
        self.engine = engine.Engine(self)
        self.engine.set_machines(recipes.machines())
        # ほかのアプリから頼まれた生成 (spec 6 章)。画面を出していなくても裏で動く
        self.external = external.ExternalRuns(self)
        # 裏で起動した (--background) ときは、量産・物語の「続きから始める」を、画面を初めて出すときに聞く
        self.background_start = False
        self._resume_offered = False
        recipes.write_published(recipes.load())  # ほかのアプリに出すレシピ (保存していなくても今の中身で書いておく)

        self.line = LineView(self.engine)
        self.tabs = QTabWidget()
        self.tabs.addTab(self.line, "量産ライン")
        self.story = StoryView()
        self.tabs.addTab(self.story, "物語")
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
        self.story.status.connect(self.show_status)
        self.line.to_story.connect(self._line_to_story)
        self.line.to_batch.connect(self._line_to_batch)
        # 台は量産ラインと物語で共通。空きは engine.POOL で分け合い、両方が待っていれば物語が先 (同時に動いてよい)
        self.line.machines_changed.connect(self.story.machines_changed)
        self.story.edit_machines = self.line._edit_machines
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
        if self.background_start and not self._resume_offered:
            from PySide6.QtCore import QTimer
            QTimer.singleShot(0, self.offer_resume)

    def busy(self) -> bool:
        """量産ライン・物語・ほかのアプリから頼まれた生成のどれかが動いているか。"""
        run = self.engine.run
        line = run is not None and run.unfinished > 0 and self.engine.active
        return bool(line or self.story.batch.active or self.story.busy() or self.external.active())

    def quit_quietly(self) -> None:
        """画面を出していないときに終わる (動いているものがないとき。裏で起動して、頼まれたものが終わった)。"""
        self.engine.stop_threads()
        self.story.shutdown()
        self.external.stop_threads()

    # ほかのアプリから送られたもの (spec 5 章) ------------------------------------
    def receive(self, message: dict) -> None:
        """J-Rate・PromptWordCrafter の「PrismLab に送る」。入れたらそのタブに切り替え、入れられなければ理由を出す。"""
        self.bring_to_front()
        files = [str(f) for f in message.get("files") or [] if isinstance(f, str) and f]
        sender = str(message.get("from") or "")
        target = message.get("target")
        if target == "recipe":
            try:
                index = int(message.get("index", -1))
            except (TypeError, ValueError):
                index = -1
            problem = self.line.receive_picked(files, sender, str(message.get("recipe") or ""), index)
            tab = self.line
        elif target == "story":
            problem = self.story.receive(files, str(message.get("slot") or ""), sender)
            tab = self.story
        else:
            return
        if problem:
            QMessageBox.warning(self, "PrismLab に送る", problem)
        else:
            self.tabs.setCurrentWidget(tab)

    def _line_to_story(self, files: list) -> None:
        """量産ラインの結果の画像を、開いている物語の最後にシーンとして足す。続けて足せるようにタブはそのまま。"""
        opened = self.story.story
        before = len(opened.scenes) if opened is not None else 0
        problem = self.story.receive(files, "scenes", "量産ライン")
        added = len(opened.scenes) - before if opened is not None else 0
        if added:
            self.show_status(f"物語「{opened.name}」の最後にシーンを {added} 個足しました (シーンチェンジ・手元の画像)。")
        if problem:
            QMessageBox.warning(self, "物語に足す", problem)

    def _line_to_batch(self, files: list) -> None:
        """量産ラインの結果の画像を、物語のバッチの最初の画像にする (設定の窓を出す)。"""
        problem = self.story.queue_images(files)
        if problem:
            QMessageBox.warning(self, "物語のバッチに足す", problem)

    # 設定の窓 (GenCommon のもの。3 つのアプリと共有の登録) ---------------------
    def _comfy_settings(self) -> None:
        ComfySettingsDialog(self).exec()
        self.engine.set_machines(recipes.machines())  # 接続先の名前を変えたとき
        self.line.refresh(rebuild=True)
        self.story.settings_changed()

    def _reader_settings(self) -> None:
        reader.ReaderSettingsDialog(0, self).exec()
        self.line.refresh(rebuild=True)
        self.story.settings_changed()

    def _enhancer_settings(self) -> None:
        enhancer.EnhancerSettingsDialog(0, self).exec()
        self.line.refresh(rebuild=True)
        self.story.settings_changed()

    # 続きから始める ---------------------------------------------------------
    def offer_resume(self) -> None:
        if self._resume_offered:
            return
        self._resume_offered = True
        self._offer_line_resume()
        self.story.offer_batch_resume()

    def _offer_line_resume(self) -> None:
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
        if self.external.active():
            # ほかのアプリから頼まれた生成が動いている: 画面を閉じるだけにして、裏で続ける (量産・物語も止めない)
            event.ignore()
            settings.save_geometry(self)
            self.hide()
            self.closed_to_tray.emit()
            return
        if self.story.batch.active:
            # バッチは待ち行列に残すが、今作っている候補は受け取れない
            if QMessageBox.question(self, "閉じる", "物語のバッチを作っています。閉じると、今作っている候補は受け取れません"
                                    " (ComfyUI には中止を送ります)。待ち行列は残り、次に起動したときに、まだ採用していない"
                                    "シーンから続けられます。閉じますか?",
                                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                    QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        elif self.story.busy():
            # 物語の候補は続きから受け取れないので、閉じる前に聞く
            if QMessageBox.question(self, "閉じる", "物語の候補を作っています。閉じると、作っている候補は受け取れません"
                                    " (ComfyUI には中止を送ります)。閉じますか?",
                                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                    QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
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
        self.story.shutdown()
        self.external.stop_threads()
        settings.save_geometry(self)
        event.accept()
        # 画面を閉じても終わらない設定にしてある (裏で続けるため) ので、ここで終わる
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QApplication
        QTimer.singleShot(0, lambda: QApplication.instance().exit(0))

