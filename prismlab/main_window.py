"""PrismLab のウィンドウ: 「量産ライン」と「物語」のタブ、ステータスバー、閉じるときの確認、続きから始める。"""

import time

from PySide6.QtCore import QEvent, Signal
from PySide6.QtWidgets import QMainWindow, QMessageBox, QPushButton, QTabWidget, QToolBar

from gencommon import enhancer, reader
from gencommon.comfy_dialogs import ComfySettingsDialog

from . import engine, external, line_queue, recipes, settings, theme
from .line_view import LineView
from .story_view import StoryView

AWAY_SECONDS = 1.0  # これより長くほかのウィンドウにいたら、戻ったときに入口の★を読み直す


class MainWindow(QMainWindow):
    closed_to_tray = Signal()  # 頼まれた生成が動いているので、閉じずに隠した

    def __init__(self, notice: str = ""):
        super().__init__()
        self.setWindowTitle("PrismLab")
        # 量産ラインの待ち行列 (1 本ずつ。試作はすぐ。前に終わらなかったものは、続きから始めるかを聞くまで動かさない)
        self.queue = line_queue.LineQueue(self)
        self.queue.set_machines(recipes.machines())
        self.queue.load()
        # ほかのアプリから頼まれた生成 (spec 6 章)。画面を出していなくても裏で動く
        self.external = external.ExternalRuns(self)
        # 裏で起動した (--background) ときは、量産・物語の「続きから始める」を、画面を初めて出すときに聞く
        self.background_start = False
        self._resume_offered = False
        recipes.write_published(recipes.load())  # ほかのアプリに出すレシピ (保存していなくても今の中身で書いておく)

        self.line = LineView(self.queue)
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

    @property
    def engine(self):
        """量産ラインで選んでいる量産のエンジン (テストで見る)。"""
        return self.line.engine

    def busy(self) -> bool:
        """量産ライン・物語・ほかのアプリから頼まれた生成のどれかが動いているか。"""
        line = self.queue.active() or (self.line.job is None and self.line.running())
        return bool(line or self.story.batch.active or self.story.busy() or self.external.active())

    def quit_quietly(self) -> None:
        """画面を出していないときに終わる (動いているものがないとき。裏で起動して、頼まれたものが終わった)。"""
        self.queue.stop_threads()
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
        self.queue.set_machines(recipes.machines())  # 接続先の名前を変えたとき
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
        """前に終わらなかった量産 (動いていたもの・待ち行列に入っていたもの) を、続けるか聞く。"""
        q = self.queue
        if not q.held:
            return
        started = q.running_jobs()
        waiting = q.waiting_jobs()
        if not started and not waiting:
            q.held = False
            return
        quiet = all(j.run.paused and not any(i.prompt_id for i in j.run.items if not i.finished) for j in started) \
            and bool(started)
        if quiet:
            q.resume()  # 一時停止したまま閉じた: 聞かずに一時停止のまま戻す (待ち行列はそのまま待つ)
            self.line.refresh(rebuild=True)
            self.show_status(f"前回の「{started[0].name}」を一時停止のまま戻しました (待機 {started[0].run.unfinished} 件)。")
            return
        parts = [f"「{j.name}」残り {j.run.unfinished} 件" for j in started]
        if waiting:
            parts.append(f"待ち行列 {len(waiting)} 本 (" + "、".join(f"「{j.name}」" for j in waiting[:3])
                         + (" …" if len(waiting) > 3 else "") + ")")
        box = QMessageBox(self)
        box.setWindowTitle("続きから始める")
        box.setText("前回の量産が終わっていません: " + "・".join(parts) + "。")
        box.setInformativeText("続きから始めると、頼み済みのものはその台で結果を受け取り、待ち行列も順に動きます。"
                               "中断すると、残りを取り消し、待ち行列を空にします。")
        resume = box.addButton("続きから始める", QMessageBox.ButtonRole.AcceptRole)
        discard = box.addButton("中断する", QMessageBox.ButtonRole.DestructiveRole)
        box.setEscapeButton(resume)  # うっかり捨てないように
        box.setDefaultButton(resume)
        box.exec()
        if box.clickedButton() is discard:
            q.discard_unfinished()
            self.show_status("前回の量産を中断しました。")
        else:
            q.resume()
            self.show_status("前回の量産を続けます (" + "・".join(parts) + ")。")
        self.line.refresh(rebuild=True)

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
        q = self.queue
        moving = [j for j in q.running_jobs() if j.engine.active]
        waiting = q.waiting_jobs()
        stopped = all(j.run.paused and not any(i.state == engine.RUNNING for i in j.run.items) for j in moving)
        if (moving and not stopped) or (waiting and moving):
            box = QMessageBox(self)
            box.setWindowTitle("閉じる")
            parts = [f"「{j.name}」残り {j.run.unfinished} 件" for j in moving]
            if waiting:
                parts.append(f"待ち行列 {len(waiting)} 本")
            box.setText("量産が終わっていません: " + "・".join(parts) + "。")
            box.setInformativeText("残して閉じると、次に起動したときに続きから始められます (待ち行列も残ります)。"
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
                q.discard_unfinished()
            q.stop_threads()
        else:
            q.stop_threads()
        self.story.shutdown()
        self.external.stop_threads()
        settings.save_geometry(self)
        event.accept()
        # 画面を閉じても終わらない設定にしてある (裏で続けるため) ので、ここで終わる
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QApplication
        QTimer.singleShot(0, lambda: QApplication.instance().exit(0))

