"""物語の画面: シーンを横に並べた絵コンテ。

上から: 物語を選ぶ・通して再生 / 絵コンテ (シーンとつなぎ、＋ シーン) / 選んだシーンの 元画像・プロンプト・候補。
候補づくりは量産ラインとは別のエンジン (続きからの書き出しはしない) で、同じ台に頼む。一度に 1 つのシーンだけ。
プロンプトは人が組み立てる: 読み取る (案) → 直す → 差し込み → 盛る。送るのは欄の文そのまま (+ ベースプロンプト)。
"""

import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QCoreApplication, QEvent, QEventLoop, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QIcon, QKeySequence, QPainter, QPen, QPixmap, QShortcut, QTextCursor
from PySide6.QtWidgets import (QButtonGroup, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
                               QFileDialog, QFrame, QHBoxLayout, QLabel, QListView, QListWidget, QListWidgetItem,
                               QMenu, QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QSizePolicy, QSlider,
                               QSpinBox, QToolButton, QVBoxLayout, QWidget)

from gencommon import comfy, enhancer, inserts, reader, store

from . import engine, highlight, kinds, recipes, recycle, story, theme, translate
from .dialogs import ask_text
from .thumbs import ThumbCache
from .widgets import AddButton

ENGLISH = "英語に直し"  # LLM の仕事の名前 (「英語に直し中…」「英語に直しに失敗しました」)
TRANSLATE_DELAY_MS = 900  # 送るプロンプトを直して、これだけ止まったら訳し直す
MAX_TRANSLATIONS = 100  # 覚えておく訳の数
CONT_COLOR = "#f0a04b"  # 続き (動画の色)
CUT_COLOR = "#8c99ff"  # シーンチェンジ
OK_COLOR = "#66bb6a"  # 採用
SCENE_W, SCENE_THUMB = 168, QSize(152, 96)
CAND_ICON = QSize(150, 96)
INSERT_FOLDER_FILE = "story_insert_folder.txt"  # 前に差し込みで使ったフォルダ
STORY_RUN = "story"  # 候補づくりの種類 (エンジンの仕事の印)


def _workflow_items() -> list[tuple[str, str, str]]:
    """登録してあるワークフロー (表示, パス, 種類)。"""
    out = []
    for i, p in enumerate(comfy.workflows()):
        if not p.is_empty:
            kind = kinds.kind_of(p.value)
            out.append((p.file_label(i) + (f"   [{kind}]" if kind else ""), p.value, kind))
    return out


def _fill_workflows(box: QComboBox, current: str, only=None) -> None:
    box.blockSignals(True)
    box.clear()
    for label, path, kind in _workflow_items():
        if only is None or only(kind):
            box.addItem(label, path)
    index = next((i for i in range(box.count()) if str(box.itemData(i)).lower() == (current or "").lower()), -1)
    if index < 0 and current and (only is None or only(kinds.kind_of(current))):
        box.addItem(f"(登録にない) {current}", current)
        index = box.count() - 1
    box.setCurrentIndex(max(index, 0))
    box.blockSignals(False)


def _framed(pix: Optional[QPixmap], size: QSize, color: str = "", width: int = 3) -> QPixmap:
    canvas = QPixmap(size)
    canvas.fill(QColor("#161616"))
    p = QPainter(canvas)
    if pix is not None and not pix.isNull():
        scaled = pix.scaled(size - QSize(6, 6), Qt.AspectRatioMode.KeepAspectRatio,
                            Qt.TransformationMode.SmoothTransformation)
        p.drawPixmap((size.width() - scaled.width()) // 2, (size.height() - scaled.height()) // 2, scaled)
    if color:
        p.setPen(QPen(QColor(color), width))
        p.drawRect(width // 2, width // 2, size.width() - width, size.height() - width)
    p.end()
    return canvas


def _is_video(path: str) -> bool:
    return Path(path).suffix.lower() in (".mp4", ".webm", ".mov", ".mkv", ".gif", ".avi")


# ----------------------------------------------------------------------
# 絵コンテの部品
# ----------------------------------------------------------------------
class SceneCard(QFrame):
    """シーン 1 つ (サムネール・名前・状態)。クリックで選ぶ、右クリックでメニュー。"""

    clicked = Signal()
    menu_requested = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedWidth(SCENE_W)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        box = QVBoxLayout(self)
        box.setContentsMargins(7, 7, 7, 7)
        box.setSpacing(4)
        self.thumb = QLabel()
        self.thumb.setFixedSize(SCENE_THUMB)
        box.addWidget(self.thumb)
        self.name = QLabel()
        self.name.setStyleSheet("font-weight: bold; background: transparent;")
        box.addWidget(self.name)
        self.state = QLabel()
        self.state.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px; background: transparent;")
        box.addWidget(self.state)
        self.set_selected(False)

    def set_selected(self, on: bool) -> None:
        color = theme.ACCENT if on else "#444"
        self.setStyleSheet(f"SceneCard {{ background: #262626; border: {3 if on else 1}px solid {color};"
                           " border-radius: 8px; }")

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(event.position().toPoint()):
            self.clicked.emit()
        super().mouseReleaseEvent(event)

    def contextMenuEvent(self, event):
        self.menu_requested.emit(event.globalPos())


class LinkButton(QToolButton):
    """シーンのあいだのつなぎ (⟶ 続き / ┃ シーンチェンジ)。押すと切り替える。"""

    def __init__(self, link: str, parent=None):
        super().__init__(parent)
        self.setFixedWidth(70)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("押すと「続き」と「シーンチェンジ」を切り替えます。\n"
                        "続き: 前のシーンで採用した動画の最後のコマを元画像にする\n"
                        "シーンチェンジ: 元画像を選び直す")
        cont = link == story.CONT
        color = CONT_COLOR if cont else CUT_COLOR
        self.setText("⟶\n続き" if cont else "┃\nシーン\nチェンジ")
        self.setStyleSheet(f"QToolButton {{ background: transparent; border: none; color: {color};"
                           " font-size: 11px; font-weight: bold; }"
                           "QToolButton:hover { color: white; }")


# ----------------------------------------------------------------------
# 前のシーンのコマを選ぶ
# ----------------------------------------------------------------------
class FrameDialog(QDialog):
    def __init__(self, video: str, parent=None, title: str = "前のシーンのコマを選ぶ"):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.video = video
        box = QVBoxLayout(self)
        self.view = QLabel()
        self.view.setFixedSize(480, 300)
        self.view.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.view.setStyleSheet("background: #111;")
        box.addWidget(self.view)
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.label = QLabel()
        box.addWidget(self.slider)
        box.addWidget(self.label)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        box.addWidget(buttons)
        self.count = story.frame_count(video)
        self.slider.setRange(0, max(0, self.count - 1))
        self.slider.setValue(max(0, self.count - 1))
        self.slider.valueChanged.connect(self._show)
        self._show(self.slider.value())

    def _show(self, index: int) -> None:
        try:
            img = story.frame_to_qimage(story.read_frame(self.video, index))
            self.view.setPixmap(QPixmap.fromImage(img).scaled(self.view.size(), Qt.AspectRatioMode.KeepAspectRatio,
                                                              Qt.TransformationMode.SmoothTransformation))
        except story.StoryError as exc:
            self.view.setText(str(exc))
        self.label.setText(f"{index + 1} / {self.count} コマ")

    @property
    def frame(self) -> int:
        return self.slider.value()

    @property
    def is_last(self) -> bool:
        return self.slider.value() >= self.slider.maximum()


# ----------------------------------------------------------------------
# 通して再生
# ----------------------------------------------------------------------
class _Deck:
    """通して再生のプレーヤー 1 つ分 (プレーヤー・音・画面と、読み込んだクリップの番号)。"""

    def __init__(self, parent):
        from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
        from PySide6.QtMultimediaWidgets import QVideoWidget
        self.player = QMediaPlayer(parent)
        self.audio = QAudioOutput(parent)
        self.widget = QVideoWidget()
        self.player.setAudioOutput(self.audio)
        self.player.setVideoOutput(self.widget)
        self.index = -1  # 読み込んだクリップ (-1 = なし)
        self.start_ms = 0  # 再生を始める位置 (続きのシーンは 2 コマ目から)
        self.frame_ms = 42  # 1 コマの長さ
        self.ready = False  # 読み込んで、始める位置で止まっている


def _frame_ms(path: str) -> int:
    """1 コマの長さ (ミリ秒)。分からなければ 24 fps とみなす。"""
    try:
        import cv2
        cap = cv2.VideoCapture(str(path))
        fps = cap.get(cv2.CAP_PROP_FPS) if cap.isOpened() else 0
        cap.release()
    except Exception:  # noqa: BLE001 - 分からなくても再生はできる
        fps = 0
    return int(round(1000 / fps)) if fps and fps > 1 else 42


class PlayDialog(QDialog):
    """採用した動画を間をあけずに順番に再生する。

    切れ目が目立たないように、プレーヤーを 2 つ用意して交互に使う: 再生しているあいだに次のクリップを
    裏で読み込み、始める位置で止めておく (その絵がもう出ている)。
    「終わり」の合図は最後のコマから 100 ms ほど遅れ、そのあいだに空のコマ (黒) も出るので待たない:
    最後のコマが出たら次のプレーヤーを動かし始め (最初のコマまで 1 コマ分ほどかかる)、1 コマ分たったら
    (または前のほうが空のコマを出した・次のほうが動き出したら) 画面を切り替える。
    「続き」のシーン (playlist の 3 つ目が True) は 1 コマ目が前のクリップの最後のコマと同じ絵なので、
    2 コマ目から再生する (同じ絵が 2 コマ続いて止まって見えないように)。
    """

    def __init__(self, playlist: list, parent=None):
        super().__init__(parent)
        from PySide6.QtWidgets import QStackedWidget
        self.setWindowTitle("通して再生")
        self.resize(960, 620)
        # (見出し, パス) または (見出し, パス, 前のクリップの続きか)
        self.playlist = [(e[0], e[1], bool(e[2]) if len(e) > 2 else False) for e in playlist]
        self.index = 0
        box = QVBoxLayout(self)
        self.stack = QStackedWidget()
        self.decks = [_Deck(self), _Deck(self)]
        for n, deck in enumerate(self.decks):
            self.stack.addWidget(deck.widget)
            deck.player.mediaStatusChanged.connect(lambda status, n=n: self._status(n, status))
            deck.widget.videoSink().videoFrameChanged.connect(lambda frame, n=n: self._frame(n, frame))
        self.active = 0
        self._incoming: Optional[int] = None  # 動かし始めた次のプレーヤー (画面を切り替える前)
        self._closed = False
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)  # 閉じたら窓ごと片付ける
        box.addWidget(self.stack, 1)
        row = QHBoxLayout()
        self.title = QLabel()
        self.title.setStyleSheet("font-weight: bold;")
        prev_b, next_b, again = QPushButton("◀ 前"), QPushButton("次 ▶"), QPushButton("最初から")
        prev_b.clicked.connect(lambda: self._go(self.index - 1))
        next_b.clicked.connect(lambda: self._go(self.index + 1))
        again.clicked.connect(lambda: self._go(0))
        row.addWidget(self.title, 1)
        for b in (again, prev_b, next_b):
            row.addWidget(b)
        box.addLayout(row)
        self._go(0)

    @property
    def player(self):
        """いま見せているプレーヤー。"""
        return self.decks[self.active].player

    def _load(self, deck: _Deck, index: int, play: bool) -> None:
        """deck にクリップを読み込む。play でなければ、始める位置で止めて待つ (次のクリップの用意)。"""
        if self._closed:  # 閉じたあと (予約していた先読みなど) は開かない
            return
        _title, path, cont = self.playlist[index]
        deck.index, deck.ready = index, False
        deck.frame_ms = _frame_ms(path)
        # 「◀ 前」「最初から」で選んだときは、続きでも 1 コマ目から見せる
        deck.start_ms = deck.frame_ms if cont and not play else 0
        deck.player.setSource(QUrl.fromLocalFile(path))
        if play:
            deck.player.play()
        else:
            deck.player.pause()  # 読み込んで最初のコマで止まる

    def _show(self, n: int) -> None:
        self.active = n
        self.stack.setCurrentWidget(self.decks[n].widget)
        index = self.decks[n].index
        self.title.setText(f"{index + 1} / {len(self.playlist)}   {self.playlist[index][0]}")

    def _preload_next(self) -> None:
        nxt = self.index + 1
        other = self.decks[1 - self.active]
        if nxt < len(self.playlist) and other.index != nxt:
            self._load(other, nxt, play=False)

    def _go(self, index: int) -> None:
        """選んだクリップから再生する (ボタン)。次のクリップを裏で用意する。"""
        if not 0 <= index < len(self.playlist):
            return
        self.index = index
        self._incoming = None
        for deck in self.decks:
            deck.player.stop()
            deck.index = -1
        self._load(self.decks[self.active], index, play=True)
        self._show(self.active)
        self._preload_next()

    def _advance(self) -> None:
        """次のクリップのプレーヤーを動かし始める (画面はまだ前のクリップの最後のコマ)。"""
        if self._closed or self._incoming is not None or self.index + 1 >= len(self.playlist):
            return
        self.index += 1
        n = 1 - self.active
        other = self.decks[n]
        if other.index != self.index:  # 用意が間に合わなかった (すぐ読み込んで再生)
            self._load(other, self.index, play=True)
        else:
            other.player.play()
        self._incoming = n
        QTimer.singleShot(self.decks[self.active].frame_ms, self._switch)  # 最後のコマを 1 コマ分見せてから

    def _switch(self) -> None:
        """画面を次のプレーヤーに切り替える (止めて出していた最初のコマから続く)。"""
        n = self._incoming
        if n is None:
            return
        self._incoming = None
        old = self.decks[self.active]
        self._show(n)
        old.index = -1
        QTimer.singleShot(0, self._preload_next)  # 終わったほうに、次の次を読み込む

    def _frame(self, n: int, frame) -> None:
        """画面に出たコマ: 最後のコマが出たら次を動かし始める。"""
        if n == self.active:
            deck = self.decks[n]
            if not frame.isValid():  # 終わって空のコマ (黒) になった: すぐ切り替える
                self._switch()
                return
            if self._incoming is None and deck.index == self.index:
                duration = deck.player.duration()
                # 最後のコマ (このコマの終わりが、長さの半コマ手前より後) が出たら
                if duration > 0 and frame.startTime() // 1000 + deck.frame_ms >= duration - deck.frame_ms // 2:
                    self._advance()
        elif n == self._incoming and frame.isValid() and \
                frame.startTime() // 1000 > self.decks[n].start_ms + self.decks[n].frame_ms // 2:
            self._switch()  # 次のほうが、止めていたコマの次へ進んだ

    def _status(self, n: int, status) -> None:
        from PySide6.QtMultimedia import QMediaPlayer
        deck = self.decks[n]
        if n != self.active:  # 裏で用意しているほう: 読めたら始める位置へ
            if status in (QMediaPlayer.MediaStatus.LoadedMedia, QMediaPlayer.MediaStatus.BufferedMedia) \
                    and not deck.ready and n != self._incoming:
                if deck.start_ms:
                    deck.player.setPosition(deck.start_ms)
                deck.ready = True
            return
        if status == QMediaPlayer.MediaStatus.EndOfMedia:  # 最後のコマを見逃したとき
            if self._incoming is None:
                self._advance()
            self._switch()

    def done(self, result: int) -> None:
        # 動画を手放す (stop だけではファイルを開いたままで、候補をごみ箱へ送れない)
        self._closed = True
        self._incoming = None
        for deck in self.decks:
            deck.player.stop()
            deck.player.setSource(QUrl())
            deck.index = -1
        super().done(result)


# ----------------------------------------------------------------------
# T2I で元画像を作る
# ----------------------------------------------------------------------
class T2IDialog(QDialog):
    """シーンの元画像を T2I で作る。作ったものから 1 枚選ぶ。"""

    def __init__(self, view: "StoryView", scene: story.Scene, parent=None):
        super().__init__(parent)
        self.view, self.scene = view, scene
        self.setWindowTitle("T2I で元画像を作る")
        self.resize(720, 560)
        self.chosen = ""
        box = QVBoxLayout(self)
        row = QHBoxLayout()
        self.workflow = QComboBox()
        _fill_workflows(self.workflow, "", only=lambda k: k == "T2I")
        self.count = QSpinBox()
        self.count.setRange(1, 9)
        self.count.setValue(4)
        self.count.setPrefix("枚数 ")
        row.addWidget(QLabel("ワークフロー"))
        row.addWidget(self.workflow, 1)
        row.addWidget(self.count)
        box.addLayout(row)
        self.prompt = QPlainTextEdit(scene.send_text)
        self.prompt.setPlaceholderText("作りたい画像の説明")
        self.prompt.setFixedHeight(90)
        box.addWidget(self.prompt)
        self.make = QPushButton("作る")
        self.make.clicked.connect(self._make)
        self.state = QLabel()
        row2 = QHBoxLayout()
        row2.addWidget(self.make)
        row2.addWidget(self.state, 1)
        box.addLayout(row2)
        self.results = QListWidget()
        self.results.setViewMode(QListView.ViewMode.IconMode)
        self.results.setIconSize(CAND_ICON)
        self.results.setGridSize(CAND_ICON + QSize(14, 30))
        self.results.setResizeMode(QListView.ResizeMode.Adjust)
        self.results.setMovement(QListView.Movement.Static)
        self.results.itemDoubleClicked.connect(lambda _i: self._use())
        box.addWidget(self.results, 1)
        buttons = QDialogButtonBox()
        self.use = buttons.addButton("この画像を元画像にする", QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton("閉じる", QDialogButtonBox.ButtonRole.RejectRole)
        self.use.clicked.connect(self._use)
        buttons.rejected.connect(self.reject)
        box.addWidget(buttons)
        if self.workflow.count() == 0:
            self.state.setText("T2I のワークフローが登録されていません (「生成の設定」で登録してください)。")
            self.make.setEnabled(False)
        self.view.engine.changed.connect(self._schedule)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(300)
        self._timer.timeout.connect(self._refresh)
        self.view.thumbs.changed.connect(lambda _p: self._schedule())
        self._outputs: list[str] = []

    def _schedule(self) -> None:
        if not self._timer.isActive():
            self._timer.start()

    def _make(self) -> None:
        folder = self.view.story.scene_dir(self.scene) / "t2i"
        folder.mkdir(parents=True, exist_ok=True)
        source = folder / "prompt.txt"  # 元画像がないので、文を残したファイルを元にする (出力の名前にも使う)
        text = self.prompt.toPlainText().strip()
        try:
            source.write_text(text, encoding="utf-8")
        except OSError:
            pass
        ok = self.view.start_job(("t2i", self.scene.id), str(self.workflow.currentData() or ""), text, None, True,
                                 self.count.value(), str(source), str(folder))
        if ok:
            self.make.setEnabled(False)

    def _refresh(self) -> None:
        run, items, _m = self.view.engine.snapshot()
        mine = self.view.job == ("t2i", self.scene.id)
        if mine and run is not None:
            done = sum(1 for i in items if i.finished)
            self.state.setText(f"作っています {done} / {len(items)}" if run.unfinished else f"出来ました ({done} 枚)")
            outs = [o["path"] for i in items for o in i.outputs]
            if outs != self._outputs:
                self._outputs = outs
                self.results.clear()
                for path in outs:
                    item = QListWidgetItem(Path(path).name)
                    item.setData(Qt.ItemDataRole.UserRole, path)
                    item.setSizeHint(self.results.gridSize())
                    self.results.addItem(item)
            if not run.unfinished:
                self.make.setEnabled(True)
        for n in range(self.results.count()):
            item = self.results.item(n)
            if item.icon().isNull():
                pix = self.view.thumbs.get(item.data(Qt.ItemDataRole.UserRole))
                if pix is not None:
                    item.setIcon(QIcon(_framed(pix, CAND_ICON)))

    def _use(self) -> None:
        item = self.results.currentItem()
        if item is None:
            return
        self.chosen = str(item.data(Qt.ItemDataRole.UserRole))
        self.accept()


# ----------------------------------------------------------------------
# プロンプトの履歴
# ----------------------------------------------------------------------
class HistoryDialog(QDialog):
    """シーンのプロンプトの履歴 (新しい順)。選んで「この文に戻す」。"""

    def __init__(self, scene: story.Scene, parent=None):
        super().__init__(parent)
        self.setWindowTitle("プロンプトの履歴")
        self.resize(860, 520)
        self.chosen: Optional[str] = None
        box = QVBoxLayout(self)
        hint = QLabel("元の文・読み取った文・盛った文・差し込んだ文・候補に送った文を残しています (新しい順)。"
                      "いちばん下がいちばん最初の文です。")
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {theme.TEXT_DIM};")
        box.addWidget(hint)
        row = QHBoxLayout()
        self.list = QListWidget()
        self.list.setFixedWidth(300)
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        row.addWidget(self.list)
        row.addWidget(self.text, 1)
        box.addLayout(row, 1)
        for h in reversed(scene.history):
            when = time.strftime("%m/%d %H:%M", time.localtime(float(h.get("time") or 0)))
            first = " ".join(str(h.get("text", "")).split())[:40]
            item = QListWidgetItem(f"{when}  {h.get('label', '')}\n{first}")
            item.setData(Qt.ItemDataRole.UserRole, str(h.get("text", "")))
            if str(h.get("text", "")) == scene.prompt:
                item.setForeground(QColor(OK_COLOR))
                item.setToolTip("今の文と同じ")
            self.list.addItem(item)
        self.list.currentItemChanged.connect(
            lambda cur, _p: self.text.setPlainText(cur.data(Qt.ItemDataRole.UserRole) if cur else ""))
        self.list.itemDoubleClicked.connect(lambda _i: self._use())
        buttons = QDialogButtonBox()
        use = buttons.addButton("この文に戻す", QDialogButtonBox.ButtonRole.AcceptRole)
        copy = buttons.addButton("コピー", QDialogButtonBox.ButtonRole.ActionRole)
        buttons.addButton("閉じる", QDialogButtonBox.ButtonRole.RejectRole)
        use.clicked.connect(self._use)
        copy.clicked.connect(self._copy)
        buttons.rejected.connect(self.reject)
        box.addWidget(buttons)
        if self.list.count():
            self.list.setCurrentRow(0)
        else:
            self.text.setPlainText("まだ履歴がありません (読み取る・盛る・差し込み・候補を作るで残ります)。")
            use.setEnabled(False)

    def _use(self) -> None:
        item = self.list.currentItem()
        if item is not None:
            self.chosen = str(item.data(Qt.ItemDataRole.UserRole))
            self.accept()

    def _copy(self) -> None:
        from PySide6.QtWidgets import QApplication
        QApplication.clipboard().setText(self.text.toPlainText())


# ----------------------------------------------------------------------
# 物語の画面
# ----------------------------------------------------------------------
class StoryView(QWidget):
    status = Signal(str)
    _llm_done = Signal(str, str, str)  # (シーンの id, 文, 失敗の理由)
    _translated = Signal(int, str, str, str)  # (頼んだ番号, 訳した文, 訳, 失敗の理由)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.engine = engine.Engine(self, run_file=None)  # 候補づくり (量産ラインとは別。続きからは書かない)
        self.engine.changed.connect(self._schedule)
        self.engine.finished.connect(self._on_finished)
        self.thumbs = ThumbCache(200, self)
        self.thumbs.changed.connect(lambda _p: self._schedule())
        self.story: Optional[story.Story] = None
        self.scene: Optional[story.Scene] = None
        self.job = None  # 候補づくりの印 ("cand" / "t2i", シーンの id)
        self._job_prompt = ""
        self._job_original = ""  # 候補づくりのときの元のプロンプト (候補に記録する)
        self._job_reference = ""
        self._job_last = ""
        self._before: dict[str, str] = {}  # シーンの id → 読み取る・盛るを始めたときの文 (うまくいったら履歴に残す)
        self._llm_busy = ""
        self._llm_done.connect(self._on_llm_done)
        self._translated.connect(self._on_translated)
        self._loading = False
        self._player = None

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 10)

        # 物語を選ぶ
        top = QHBoxLayout()
        self.story_box = QComboBox()
        self.story_box.setMinimumWidth(320)
        self.story_box.activated.connect(self._story_chosen)
        new_b = QPushButton("新しい物語...")
        new_b.clicked.connect(self._new_story)
        open_b = QPushButton("開く...")
        open_b.clicked.connect(self._open_story)
        self.play_b = QPushButton("▶ 通して再生")
        self.play_b.setStyleSheet(f"QPushButton {{ background: {CONT_COLOR}; color: black; font-weight: bold;"
                                  " padding: 5px 16px; } QPushButton:disabled { background: #3a3a3a; color: #777; }")
        self.play_b.setToolTip("採用した動画を、選んでいるシーンから続けて再生します。")
        self.play_b.clicked.connect(self._play)
        self.adopted_b = QPushButton("採用のフォルダを開く")
        self.adopted_b.setToolTip("採用した動画を 01_名前.mp4 の番号順に揃えたフォルダ (動画編集ソフトに渡す)。")
        self.adopted_b.clicked.connect(self._open_adopted)
        self.jrate_b = QPushButton("J-Rate で開く")
        self.jrate_b.setToolTip("選んでいるシーンの候補のフォルダを J-Rate で開きます。")
        self.jrate_b.clicked.connect(self._open_jrate)
        self.pick_b = QPushButton("J-Rate で選ぶ")
        self.pick_b.setToolTip("前に元画像などを選んだフォルダを J-Rate で開きます。画像を選んで右クリックの"
                               "「PrismLab に送る」で、新しいシーンや、選んでいるシーンの元画像などに入ります。")
        self.pick_b.clicked.connect(self._pick_in_jrate)
        top.addWidget(QLabel("物語"))
        top.addWidget(self.story_box)
        top.addWidget(new_b)
        top.addWidget(open_b)
        top.addStretch(1)
        for b in (self.pick_b, self.play_b, self.adopted_b, self.jrate_b):
            top.addWidget(b)
        root.addLayout(top)

        # 絵コンテ
        self.board_host = QWidget()
        self.board = QHBoxLayout(self.board_host)
        self.board.setContentsMargins(4, 4, 4, 4)
        self.board.setSpacing(0)
        scroll = QScrollArea()
        scroll.setWidget(self.board_host)
        scroll.setWidgetResizable(True)
        scroll.setFixedHeight(190)
        self.board_host.setAcceptDrops(True)
        self.board_host.dragEnterEvent = self._board_drag
        self.board_host.dragMoveEvent = self._board_drag
        self.board_host.dropEvent = self._board_drop
        self.board_host.setToolTip("画像をここにドロップすると、シーンチェンジのシーンを足します "
                                   "(カードの上ならそのあと、ほかのところなら最後に。何枚でも)。")
        scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        root.addWidget(scroll)

        # 選んだシーン
        self.detail = QWidget()
        detail = QHBoxLayout(self.detail)
        detail.setContentsMargins(0, 0, 0, 0)
        detail.addWidget(self._build_source(), 0)
        detail.addWidget(self._build_prompt(), 1)
        detail.addWidget(self._build_candidates(), 1)
        root.addWidget(self.detail, 1)
        self.empty = QLabel("「新しい物語...」で物語のフォルダを作るか、「開く...」で前に作った物語を開いてください。")
        self.empty.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 13px; padding: 30px;")
        root.addWidget(self.empty)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(300)
        self._timer.timeout.connect(self.refresh)
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(600)
        self._save_timer.timeout.connect(self._save)

        self._fill_stories()
        recent = story.recent()
        if recent:
            self.open_folder(recent[0], quiet=True)
        self.refresh(rebuild=True)

    # ------------------------------------------------------------------
    # 組み立て
    # ------------------------------------------------------------------
    @staticmethod
    def _panel(title: str) -> tuple[QFrame, QVBoxLayout]:
        frame = QFrame()
        frame.setStyleSheet("QFrame#panel { background: #1c1c1c; border: 1px solid #383838; border-radius: 8px; }")
        frame.setObjectName("panel")
        box = QVBoxLayout(frame)
        box.setContentsMargins(10, 8, 10, 10)
        head = QLabel(title)
        head.setStyleSheet(f"color: {theme.TEXT_DIM}; font-weight: bold; background: transparent;")
        box.addWidget(head)
        return frame, box

    def _build_source(self) -> QFrame:
        frame, box = self._panel("元画像")
        frame.setFixedWidth(280)
        seg = QHBoxLayout()
        seg.setSpacing(0)
        self.link_cont = QPushButton("⟶ 続き")
        self.link_cut = QPushButton("┃ シーンチェンジ")
        self.link_group = QButtonGroup(self)
        for n, b in enumerate((self.link_cont, self.link_cut)):
            b.setCheckable(True)
            self.link_group.addButton(b, n)
            seg.addWidget(b)
        self.link_group.idClicked.connect(self._link_clicked)
        box.addLayout(seg)
        self.source_view = QLabel()
        self.source_view.setFixedSize(256, 160)
        self.source_view.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.source_view.setWordWrap(True)
        self.source_view.setStyleSheet(f"background: #111; color: {theme.TEXT_DIM}; border-radius: 4px;")
        self.source_view.setAcceptDrops(False)
        box.addWidget(self.source_view)
        self.source_note = QLabel()
        self.source_note.setWordWrap(True)
        self.source_note.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px; background: transparent;")
        box.addWidget(self.source_note)
        self.pick_file = QPushButton("手元の画像...")
        self.pick_file.setToolTip("画像を選びます (この欄に画像をドラッグしても選べます)。")
        self.pick_file.clicked.connect(self._choose_source_file)
        self.pick_frame = QPushButton("前のシーンのコマ...")
        self.pick_frame.clicked.connect(self._choose_source_frame)
        self.pick_t2i = QPushButton("T2I で作る...")
        self.pick_t2i.clicked.connect(self._make_source_t2i)
        for b in (self.pick_file, self.pick_frame, self.pick_t2i):
            box.addWidget(b)
        # リファレンス画像 (主に顔) と ラストフレーム。ワークフローが対応するときだけ (縦の場所が足りないので横に並べる)
        pair = QHBoxLayout()
        pair.setSpacing(8)

        def column(title: str):
            col = QVBoxLayout()
            col.setSpacing(3)
            head = QLabel(title)
            head.setStyleSheet(f"color: {theme.TEXT_DIM}; font-weight: bold; background: transparent; margin-top: 8px;")
            view = QLabel()
            view.setFixedSize(124, 84)
            view.setAlignment(Qt.AlignmentFlag.AlignCenter)
            view.setWordWrap(True)
            view.setStyleSheet(f"background: #111; color: {theme.TEXT_DIM}; border-radius: 4px; font-size: 10px;")
            buttons = QHBoxLayout()
            buttons.setSpacing(3)
            col.addWidget(head)
            col.addWidget(view)
            col.addLayout(buttons)
            pair.addLayout(col)
            return view, buttons

        self.ref_view, ref_buttons = column("リファレンス (顔)")
        self.ref_pick = QPushButton("選ぶ...")
        self.ref_pick.clicked.connect(self._choose_reference)
        self.ref_clear = QPushButton("外す")
        self.ref_clear.clicked.connect(self._clear_reference)
        ref_buttons.addWidget(self.ref_pick)
        ref_buttons.addWidget(self.ref_clear)
        self.last_view, last_buttons = column("ラストフレーム")
        self.last_pick = QToolButton()
        self.last_pick.setText("選ぶ")  # ボタンの矢印でメニューと分かる
        self.last_pick.setToolTip("手元の画像か、次のシーンの元画像 (追いかける) を選びます。")
        self.last_pick.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.last_pick.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        last_menu = QMenu(self.last_pick)
        last_menu.addAction("手元の画像...", self._choose_last_frame)
        self.last_next_action = last_menu.addAction("次のシーンの元画像 (追いかける)", self._follow_next_last_frame)
        self.last_pick.setMenu(last_menu)
        self.last_clear = QPushButton("外す")
        self.last_clear.clicked.connect(self._clear_last_frame)
        last_buttons.addWidget(self.last_pick)
        last_buttons.addWidget(self.last_clear)
        box.addLayout(pair)
        self.ref_note = QLabel()  # リファレンスとラストフレームの説明 (短く。詳しいことは指を置くと出す)
        self.ref_note.setWordWrap(True)
        self.ref_note.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px; background: transparent;")
        box.addWidget(self.ref_note)
        box.addStretch(1)
        frame.setAcceptDrops(True)
        frame.dragEnterEvent = self._drag_enter
        frame.dropEvent = self._drop
        return frame

    def _build_prompt(self) -> QFrame:
        frame, box = self._panel("プロンプト")
        head1 = QLabel("元のプロンプト (自分で書く文。読み取る・盛る・差し込みでは書き換えません)")
        head1.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px; background: transparent;")
        box.addWidget(head1)
        self.original = QPlainTextEdit()
        self.original.setPlaceholderText("このシーンで起きること (例: 左から女性が歩いてきて右に消える)")
        self.original.setFixedHeight(76)
        self.original.textChanged.connect(self._original_changed)
        box.addWidget(self.original)
        mid = QHBoxLayout()
        self.add_original_b = QPushButton("↓ 元の文を足す")
        self.add_original_b.setToolTip("元のプロンプトを、下の送るプロンプトに入れます (空なら入れる、文があれば改行して足す)。")
        self.add_original_b.clicked.connect(self._add_original)
        self.to_english = QCheckBox("英語に")
        self.to_english.setToolTip("チェックすると、元のプロンプトを LLM で英語に直してから足します "
                                   "(接続先とモデルは「エンハンサーの設定」と同じ)。")
        self.to_english.setChecked(translate.remembered())
        self.to_english.toggled.connect(translate.remember)
        head2 = QLabel("盛ったあとのプロンプト = 送る文 (読み取る・差し込み・盛るの結果。候補を作るで送るのはこちら。"
                       "空なら元のプロンプトを送る)")
        head2.setWordWrap(True)
        head2.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px; background: transparent;")
        mid.addWidget(self.add_original_b)
        mid.addWidget(self.to_english)
        mid.addWidget(head2, 1)
        box.addLayout(mid)
        self.prompt = QPlainTextEdit()
        self.prompt.setPlaceholderText("「読み取る」で元画像の説明を入れる・「↓ 元の文を足す」・直接書く。Ctrl+Z で戻せます。")
        self.prompt.textChanged.connect(self._prompt_changed)
        self.prompt.undoAvailable.connect(lambda _a: self._schedule())
        self.prompt.setMinimumHeight(60)
        box.addWidget(self.prompt, 2)
        # MiniMax H3 のタグ (<d>…</d>、[Japanese] のセリフ) を色分けする
        self._highlighters = [highlight.TagHighlighter(self.original.document()),
                              highlight.TagHighlighter(self.prompt.document())]
        # 送るプロンプトの訳を見る (見るだけ。文が変わって少し止まったら LLM で日本語に訳す)
        r0 = QHBoxLayout()
        self.view_translation = QCheckBox("訳を見る")
        self.view_translation.setToolTip("送るプロンプトの日本語訳を下に出します (見るだけで、送る文は変わりません)。\n"
                                         "文を直して少し止まると訳し直します。接続先とモデルは「エンハンサーの設定」と同じ。")
        self.view_translation.setChecked(translate.remembered(translate.VIEW_FILE))
        self.view_translation.toggled.connect(self._translation_toggled)
        self.translation_state = QLabel()
        self.translation_state.setStyleSheet(f"color: {theme.TEXT_DIM}; background: transparent; font-size: 11px;")
        r0.addWidget(self.view_translation)
        r0.addWidget(self.translation_state, 1)
        box.addLayout(r0)
        self.translation = QPlainTextEdit()
        self.translation.setReadOnly(True)
        self.translation.setMinimumHeight(44)  # 送るプロンプトと高さを分け合う (2 : 1)
        self.translation.setPlaceholderText("送るプロンプトの訳")
        self.translation.setStyleSheet(f"QPlainTextEdit {{ background: #1b1b1b; color: {theme.TEXT_DIM}; }}")
        self._highlighters.append(highlight.TagHighlighter(self.translation.document()))
        self.translation.setVisible(self.view_translation.isChecked())
        box.addWidget(self.translation, 1)
        self._translate_timer = QTimer(self)
        self._translate_timer.setSingleShot(True)
        self._translate_timer.setInterval(TRANSLATE_DELAY_MS)
        self._translate_timer.timeout.connect(self._translate_now)
        self._translations: dict[str, str] = {}  # 訳した文 → 訳 (同じ文はもう一度訳さない)
        self._translate_seq = 0  # 頼んだ順の番号 (古い返事は捨てる)
        self._translating = ""  # いま訳している文
        self.prompt.textChanged.connect(self._schedule_translation)
        r1 = QHBoxLayout()
        self.reader_box = QComboBox()
        self.read_b = QPushButton("読み取る")
        self.read_b.setToolTip("元画像を LLM に見せて、説明を送るプロンプトに入れます (置き換わります。元に戻す / Ctrl+Z で戻せます)。")
        self.read_b.clicked.connect(self._read)
        r1.addWidget(self.reader_box, 1)
        r1.addWidget(self.read_b)
        box.addLayout(r1)
        r2 = QHBoxLayout()
        self.enhancer_box = QComboBox()
        self.enhance_b = QPushButton("盛る")
        self.enhance_b.setToolTip("送るプロンプト (空なら元のプロンプト) を LLM に盛らせて、送るプロンプトを置き換えます (元に戻す / Ctrl+Z)。")
        self.enhance_b.clicked.connect(self._enhance)
        r2.addWidget(self.enhancer_box, 1)
        r2.addWidget(self.enhance_b)
        box.addLayout(r2)
        r3 = QHBoxLayout()
        self.insert_b = QPushButton("差し込み...")
        self.insert_b.setToolTip("フォルダの .txt を名前順に 1 つ取り、送るプロンプトの終わりに足します (空なら元のプロンプトに足して入れる)。")
        self.insert_b.clicked.connect(self._insert)
        self.undo_b = QPushButton("元に戻す")
        self.undo_b.setToolTip("送るプロンプトを 1 つ前に戻します (Ctrl+Z と同じ。読み取る・盛る・差し込みも戻せます)。\n"
                               "開き直したあとは、直前の操作の前の文に戻します。")
        self.undo_b.clicked.connect(self._undo_llm)
        self.history_b = QPushButton("履歴...")
        self.history_b.setToolTip("このシーンのプロンプトの履歴 (元の文・読み取った文・盛った文・送った文) を見て、戻せます。")
        self.history_b.clicked.connect(self._show_history)
        self.llm_state = QLabel()
        self.llm_state.setStyleSheet(f"color: {theme.TEXT_DIM}; background: transparent;")
        r3.addWidget(self.insert_b)
        r3.addWidget(self.undo_b)
        r3.addWidget(self.history_b)
        r3.addWidget(self.llm_state, 1)
        box.addLayout(r3)
        # 欄の外で Ctrl+Z を押しても、送るプロンプトを戻す (欄の中では欄のもの)
        undo_key = QShortcut(QKeySequence.StandardKey.Undo, frame)
        undo_key.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        undo_key.activated.connect(self._undo_llm)
        return frame

    def _build_candidates(self) -> QFrame:
        frame, box = self._panel("候補")
        r1 = QHBoxLayout()
        self.workflow_box = QComboBox()
        self.workflow_box.currentIndexChanged.connect(self._gen_settings_changed)
        r1.addWidget(self.workflow_box, 1)
        box.addLayout(r1)
        r2 = QHBoxLayout()
        self.use_seconds = QCheckBox("長さ")
        self.seconds_box = QDoubleSpinBox()
        self.seconds_box.setRange(1, 60)
        self.seconds_box.setDecimals(1)
        self.seconds_box.setSuffix(" 秒")
        self.use_seconds.toggled.connect(self._gen_settings_changed)
        self.seconds_box.valueChanged.connect(self._gen_settings_changed)
        self.base_box = QCheckBox("ベースプロンプト")
        self.base_box.toggled.connect(self._gen_settings_changed)
        self.count_box = QSpinBox()
        self.count_box.setRange(1, 9)
        self.count_box.setPrefix("候補 ")
        self.count_box.setSuffix(" 本")
        self.count_box.valueChanged.connect(self._gen_settings_changed)
        for w in (self.use_seconds, self.seconds_box, self.base_box, self.count_box):
            r2.addWidget(w)
        r2.addStretch(1)
        box.addLayout(r2)
        r3 = QHBoxLayout()
        self.make_b = QPushButton("候補を作る")
        self.make_b.setStyleSheet(f"QPushButton {{ background: {theme.ACCENT}; color: black; font-weight: bold;"
                                  " padding: 5px 14px; } QPushButton:disabled { background: #3a3a3a; color: #777; }")
        self.make_b.clicked.connect(self._make_candidates)
        self.stop_b = QPushButton("中止")
        self.stop_b.clicked.connect(self._stop_job)
        self.gen_state = QLabel()
        self.gen_state.setStyleSheet(f"color: {theme.TEXT_DIM}; background: transparent;")
        r3.addWidget(self.make_b)
        r3.addWidget(self.stop_b)
        r3.addWidget(self.gen_state, 1)
        box.addLayout(r3)
        # 選んだ候補を元画像と並べる: 元画像 | 候補 (始め) | 候補 (最後のコマ)。動画はダブルクリックで再生
        compare = QHBoxLayout()
        compare.setSpacing(6)
        self.preview_src, self.preview, self.preview_last = QLabel(), QLabel(), QLabel()
        self.preview_target = QLabel()  # その候補で指定したラストフレーム (ファーストフレーム・ラストフレームのワークフロー)
        self.preview_caps = []
        for label, cap in ((self.preview_src, "元画像"), (self.preview, "候補"), (self.preview_last, "候補の最後のコマ"),
                           (self.preview_target, "指定したラストフレーム")):
            col = QVBoxLayout()
            col.setSpacing(2)
            label.setFixedHeight(150)
            label.setMinimumWidth(60)
            label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            label.setWordWrap(True)
            label.setStyleSheet(f"background: #111; color: {theme.TEXT_DIM}; border-radius: 4px; font-size: 11px;")
            caption = QLabel(cap)
            caption.setAlignment(Qt.AlignmentFlag.AlignCenter)
            caption.setStyleSheet(f"color: {theme.TEXT_DIM}; background: transparent; font-size: 11px;")
            col.addWidget(label)
            col.addWidget(caption)
            compare.addLayout(col, 1)
            self.preview_caps.append(caption)
        box.addLayout(compare)
        self.cands = QListWidget()
        self.cands.setViewMode(QListView.ViewMode.IconMode)
        self.cands.setIconSize(CAND_ICON)
        self.cands.setGridSize(CAND_ICON + QSize(14, 30))
        self.cands.setResizeMode(QListView.ResizeMode.Adjust)
        self.cands.setMovement(QListView.Movement.Static)
        self.cands.setMinimumHeight(140)
        self.cands.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)  # Ctrl / Shift で複数
        self.cands.installEventFilter(self)  # Del で削除
        self.cands.currentItemChanged.connect(lambda *_: self._show_candidate())
        self.cands.itemDoubleClicked.connect(self._play_candidate)
        self.cands.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.cands.customContextMenuRequested.connect(self._candidate_menu)
        box.addWidget(self.cands, 1)
        r4 = QHBoxLayout()
        self.adopt_b = QPushButton("採用して次へ")
        self.adopt_b.setStyleSheet(f"QPushButton {{ background: {OK_COLOR}; color: black; font-weight: bold;"
                                   " padding: 5px 14px; } QPushButton:disabled { background: #3a3a3a; color: #777; }")
        self.adopt_b.setToolTip("選んだ候補を採用して、次のシーンへ進みます (なければ「続き」で足します)。")
        self.adopt_b.clicked.connect(lambda: self._adopt(go_next=True))
        self.adopt_only = QPushButton("採用だけ")
        self.adopt_only.clicked.connect(lambda: self._adopt(go_next=False))
        self.frame_next_b = QPushButton("コマを選んで次へ...")
        self.frame_next_b.setToolTip("選んだ候補の好きなコマから、次のシーンを始めます (候補は採用します)。\n"
                                     "最後のコマなら「続き」、ほかのコマなら「シーンチェンジ」(前のシーンのコマ) になります。")
        self.frame_next_b.clicked.connect(self._frame_next)
        r4.addWidget(self.adopt_b)
        r4.addWidget(self.frame_next_b)
        r4.addWidget(self.adopt_only)
        r4.addStretch(1)
        box.addLayout(r4)
        return frame

    # ------------------------------------------------------------------
    # 物語を開く
    # ------------------------------------------------------------------
    def _fill_stories(self) -> None:
        self.story_box.blockSignals(True)
        self.story_box.clear()
        for folder in story.recent():
            self.story_box.addItem(f"{Path(folder).name}   ({folder})", folder)
        if self.story is not None:
            index = self.story_box.findData(self.story.folder)
            self.story_box.setCurrentIndex(max(index, 0))
        self.story_box.blockSignals(False)

    def open_folder(self, folder: str, quiet: bool = False) -> bool:
        try:
            st = story.Story.load(folder)
        except story.StoryError as exc:
            if not quiet:
                QMessageBox.warning(self, "物語を開く", str(exc))
            return False
        self._save()
        self.story = st
        self.scene = st.scenes[0]
        story.remember(folder)
        self._fill_stories()
        self.refresh(rebuild=True, load=True)  # シーンの中身 (プロンプト・ワークフロー・プリセットの欄) を入れる
        self.status.emit(f"物語「{st.name}」を開きました。")
        return True

    def _story_chosen(self, index: int) -> None:
        folder = self.story_box.itemData(index)
        if folder and (self.story is None or Path(folder) != Path(self.story.folder)):
            self.open_folder(str(folder))

    def _new_story(self) -> None:
        name = ask_text(self, "新しい物語", "物語の名前")
        if not name:
            return
        parent = QFileDialog.getExistingDirectory(self, "物語のフォルダを作る場所を選ぶ",
                                                  str(Path(self.story.folder).parent) if self.story else "")
        if not parent:
            return
        folder = Path(parent) / story.safe(name)
        try:
            story.Story.create(str(folder), name)
        except story.StoryError as exc:
            QMessageBox.warning(self, "新しい物語", str(exc))
            return
        self.open_folder(str(folder))

    def _open_story(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "物語のフォルダを選ぶ (story.json のあるフォルダ)",
                                                  str(Path(self.story.folder).parent) if self.story else "")
        if folder:
            self.open_folder(folder)

    def _save(self) -> None:
        if self.story is None:
            return
        try:
            self.story.save()
        except story.StoryError as exc:
            self.status.emit(str(exc))

    def _changed(self, rebuild: bool = False) -> None:
        """物語を直したとき: 少しあとで保存し、描き直す。"""
        self._save_timer.start()
        self.refresh(rebuild=rebuild)

    # ------------------------------------------------------------------
    # 絵コンテ
    # ------------------------------------------------------------------
    def _rebuild_board(self) -> None:
        while self.board.count():
            w = self.board.takeAt(0).widget()
            if w is not None:
                w.hide()
                w.deleteLater()
        self.scene_cards: list[SceneCard] = []
        if self.story is None:
            return
        for i, scene in enumerate(self.story.scenes):
            if i > 0:
                link = LinkButton(scene.link)
                link.clicked.connect(lambda _c=False, s=scene: self._toggle_link(s))
                self.board.addWidget(link)
            card = SceneCard()
            card.clicked.connect(lambda s=scene: self.select(s))
            card.menu_requested.connect(lambda pos, s=scene: self._scene_menu(s, pos))
            self.board.addWidget(card)
            self.scene_cards.append(card)
        add = AddButton("＋ シーン")
        add.setPopupMode(QToolButton.ToolButtonPopupMode.DelayedPopup)
        add.setFixedWidth(100)
        add.setToolTip("最後に「続き」でシーンを足します。")
        add.clicked.connect(lambda: self._add_scene(None))
        spacer = QWidget()
        spacer.setFixedWidth(16)
        self.board.addWidget(spacer)
        self.board.addWidget(add)
        self.board.addStretch(1)

    def select(self, scene: story.Scene) -> None:
        if scene is self.scene:
            return
        self._save()
        self.scene = scene
        self.refresh(rebuild=False, load=True)

    def _toggle_link(self, scene: story.Scene) -> None:
        if self._busy_here(scene):
            return
        try:
            self.story.set_link(scene, story.CUT if scene.link == story.CONT else story.CONT)
        except story.StoryError as exc:
            self.status.emit(str(exc))
        self._changed(rebuild=True)

    def _add_scene(self, after: Optional[story.Scene], link: str = story.CONT) -> story.Scene:
        try:
            scene = self.story.add_scene(after, link)
        except story.StoryError as exc:
            self.status.emit(str(exc))
            scene = self.story.scenes[-1]
        self.scene = scene
        self._changed(rebuild=True)
        self.refresh(load=True)
        return scene

    def _scene_menu(self, scene: story.Scene, pos) -> None:
        menu = QMenu(self)
        rename = menu.addAction("名前を変える...")
        add_after = menu.addAction("このあとにシーンを足す")
        menu.addSeparator()
        left = menu.addAction("◀ 左へ")
        right = menu.addAction("右へ ▶")
        i = self.story.index_of(scene)
        left.setEnabled(i > 0)
        right.setEnabled(i < len(self.story.scenes) - 1)
        menu.addSeparator()
        delete = menu.addAction("このシーンを外す")
        chosen = menu.exec(pos)
        if chosen is rename:
            name = ask_text(self, "シーンの名前", "名前", scene.name)
            if name is not None:
                scene.name = name.strip()
                self.story.sync_adopted()
                self._changed(rebuild=True)
        elif chosen is add_after:
            self._add_scene(scene)
        elif chosen in (left, right):
            self.story.move_scene(scene, -1 if chosen is left else 1)
            self._changed(rebuild=True)
        elif chosen is delete:
            if self._busy_here(scene):
                return
            if QMessageBox.question(self, "シーンを外す", f"シーン「{scene.label(i)}」を外しますか?\n"
                                    "候補のファイルはシーンのフォルダに残ります。",
                                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                    QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
                self.story.remove_scene(scene)
                self.scene = self.story.scenes[min(i, len(self.story.scenes) - 1)]
                self._changed(rebuild=True)
                self.refresh(load=True)

    # ------------------------------------------------------------------
    # 元画像
    # ------------------------------------------------------------------
    def _link_clicked(self, n: int) -> None:
        if self.scene is None or self.story.index_of(self.scene) == 0:
            return
        want = story.CONT if n == 0 else story.CUT
        if want != self.scene.link:
            self._toggle_link(self.scene)

    def _set_source(self, func) -> None:
        try:
            func()
        except story.StoryError as exc:
            QMessageBox.warning(self, "元画像", str(exc))
            return
        self.thumbs.forget(self.scene.source)
        self._changed(rebuild=True)

    def _choose_source_file(self) -> None:
        if self.scene is None:
            return
        exts = " ".join("*" + e for e in recipes.IMAGE_EXTS)
        path, _ = QFileDialog.getOpenFileName(self, "元画像を選ぶ", story.pick_folder(), f"画像 ({exts})")
        if path:
            story.remember_pick(path)
            self._set_source(lambda: self.story.set_source_file(self.scene, path))

    def _choose_source_frame(self) -> None:
        prev = self.story.previous(self.scene) if self.scene else None
        if prev is None or not prev.adopted:
            QMessageBox.information(self, "前のシーンのコマ", "前のシーンに採用した動画がありません。")
            return
        try:
            dialog = FrameDialog(prev.adopted, self)
        except story.StoryError as exc:
            QMessageBox.warning(self, "前のシーンのコマ", str(exc))
            return
        if dialog.exec():
            self._set_source(lambda: self.story.set_source_frame(self.scene, prev.adopted, dialog.frame))

    def _make_source_t2i(self) -> None:
        if self.scene is None:
            return
        dialog = T2IDialog(self, self.scene, self)
        if dialog.exec() and dialog.chosen:
            path = dialog.chosen
            self._set_source(lambda: self.story.set_source_file(self.scene, path, story.SOURCE_T2I))

    def _choose_reference(self) -> None:
        if self.scene is None:
            return
        exts = " ".join("*" + e for e in recipes.IMAGE_EXTS)
        path, _ = QFileDialog.getOpenFileName(self, "リファレンス画像 (主に顔) を選ぶ", story.pick_folder(),
                                              f"画像 ({exts})")
        if path:
            story.remember_pick(path)
            self._set_reference(path)

    def _set_reference(self, path: str) -> None:
        try:
            self.story.set_reference(self.scene, path)
        except story.StoryError as exc:
            QMessageBox.warning(self, "リファレンス画像", str(exc))
            return
        self.thumbs.forget(self.scene.reference)
        self._changed()

    def _clear_reference(self) -> None:
        if self.scene is not None and self.scene.reference:
            self.story.set_reference(self.scene, "")
            self._changed()

    def _choose_last_frame(self) -> None:
        if self.scene is None or self._busy_here(self.scene):
            return
        exts = " ".join("*" + e for e in recipes.IMAGE_EXTS)
        path, _ = QFileDialog.getOpenFileName(self, "ラストフレームを選ぶ", story.pick_folder(), f"画像 ({exts})")
        if path:
            story.remember_pick(path)
            self._set_last_frame(path)

    def _set_last_frame(self, path: str = "", follow_next: bool = False) -> None:
        try:
            self.story.set_last_frame(self.scene, path, follow_next)
        except story.StoryError as exc:
            QMessageBox.warning(self, "ラストフレーム", str(exc))
            return
        if self.scene.last_frame:
            self.thumbs.forget(self.scene.last_frame)
        self._changed()

    def _follow_next_last_frame(self) -> None:
        if self.scene is not None and not self._busy_here(self.scene):
            self._set_last_frame(follow_next=True)

    def _clear_last_frame(self) -> None:
        if self.scene is not None and self.scene.last_kind and not self._busy_here(self.scene):
            self._set_last_frame()

    def _drag_enter(self, event) -> None:
        urls = event.mimeData().urls() if event.mimeData().hasUrls() else []
        if len(urls) == 1 and urls[0].isLocalFile() and urls[0].toLocalFile().lower().endswith(recipes.IMAGE_EXTS):
            event.acceptProposedAction()

    def _drop(self, event) -> None:
        path = event.mimeData().urls()[0].toLocalFile()
        if self.scene is not None:
            event.acceptProposedAction()
            story.remember_pick(path)
            self._set_source(lambda: self.story.set_source_file(self.scene, path))

    # ------------------------------------------------------------------
    # プロンプト
    # ------------------------------------------------------------------
    def _original_changed(self) -> None:
        if self._loading or self.scene is None:
            return
        self.scene.original = self.original.toPlainText()
        self._save_timer.start()

    @staticmethod
    def _set_text(edit: QPlainTextEdit, text: str) -> None:
        """欄の文を置き換える。欄の編集として行うので、Ctrl+Z で戻せる。"""
        was = edit.isReadOnly()
        edit.setReadOnly(False)
        cursor = edit.textCursor()
        cursor.beginEditBlock()
        cursor.select(QTextCursor.SelectionType.Document)
        cursor.insertText(text)
        cursor.endEditBlock()
        edit.setTextCursor(cursor)
        edit.setReadOnly(was)

    def _set_prompt(self, text: str) -> None:
        """送るプロンプト (盛ったあとのプロンプト) を置き換える。Ctrl+Z / 元に戻す で戻せる。"""
        self._set_text(self.prompt, text)

    def _add_original(self) -> None:
        if self.scene is None or not self.scene.original.strip():
            return
        if self.to_english.isChecked():  # LLM で英語に直してから足す (_on_llm_done で足す)
            snap, text = translate.snapshot(), self.scene.original.strip()
            self._run_llm(ENGLISH, lambda: translate.to_english(snap, text))
            return
        self.scene.record("元の文を足す前の文", self.prompt.toPlainText(), before=True)
        self._set_prompt(inserts.join(self.prompt.toPlainText(), self.scene.original))

    # 訳を見る -------------------------------------------------------------
    def _translation_toggled(self, on: bool) -> None:
        translate.remember(on, translate.VIEW_FILE)
        self.translation.setVisible(on)
        if on:
            self._translate_now()
        else:
            self._translate_timer.stop()
            self.translation_state.setText("")

    def _schedule_translation(self) -> None:
        """送るプロンプトが変わった: 覚えている訳ならすぐ出し、なければ少し止まってから訳す。"""
        if not self.view_translation.isChecked():
            return
        text = self.prompt.toPlainText().strip()
        if not text or text in self._translations:
            self._translate_timer.stop()
            self._translate_now()
            return
        self.translation_state.setText("直し終わったら訳します…")
        self._translate_timer.start()

    def _translate_now(self) -> None:
        text = self.prompt.toPlainText().strip()
        self._translate_seq += 1
        if not text:
            self.translation.setPlainText("")
            self.translation_state.setText("")
            return
        if text in self._translations:
            self.translation.setPlainText(self._translations[text])
            self.translation_state.setText("")
            return
        seq, snap = self._translate_seq, translate.snapshot()
        self._translating = text
        self.translation_state.setText("訳しています…")

        def run():
            try:
                result, error = translate.to_japanese(snap, text), ""
            except Exception as exc:  # noqa: BLE001 - LLM の失敗は理由を出す
                result, error = "", str(exc)
            self._translated.emit(seq, text, result, error)

        threading.Thread(target=run, daemon=True).start()

    def _on_translated(self, seq: int, text: str, result: str, error: str) -> None:
        if not error:
            self._translations[text] = result
            while len(self._translations) > MAX_TRANSLATIONS:
                del self._translations[next(iter(self._translations))]
        if seq != self._translate_seq:  # 古い頼み (文がもう変わった)。訳は覚えておく
            return
        self._translating = ""
        if error:
            self.translation_state.setText(f"訳せませんでした: {error}")
            return
        self.translation.setPlainText(result)
        self.translation_state.setText("")

    def _prompt_changed(self) -> None:
        if self._loading or self.scene is None:
            return
        self.scene.prompt = self.prompt.toPlainText()
        self._save_timer.start()

    def _fill_llm_boxes(self) -> None:
        for box, presets in ((self.reader_box, reader.load_presets()), (self.enhancer_box, enhancer.load_presets())):
            current = box.currentData()
            box.blockSignals(True)
            box.clear()
            for i, p in enumerate(presets):
                if not p.is_empty:
                    box.addItem(p.label(i), p.name)
            index = box.findData(current)
            box.setCurrentIndex(max(index, 0))
            box.blockSignals(False)

    def _run_llm(self, what: str, work) -> None:
        if self._llm_busy:
            return
        scene_id = self.scene.id
        self._llm_busy = what
        self._before[scene_id] = self.prompt.toPlainText()
        self.refresh()

        def run():
            try:
                text, error = work(), ""
            except Exception as exc:  # noqa: BLE001 - LLM の失敗は理由を出す
                text, error = "", str(exc)
            self._llm_done.emit(scene_id, text, error)

        threading.Thread(target=run, daemon=True).start()

    def _on_llm_done(self, scene_id: str, text: str, error: str) -> None:
        what, self._llm_busy = self._llm_busy, ""
        scene = next((s for s in self.story.scenes if s.id == scene_id), None) if self.story else None
        before = self._before.pop(scene_id, "")
        if error or scene is None:
            self.status.emit(f"{what}に失敗しました: {error}" if error else "")
        elif what == ENGLISH:  # 英語に直した元の文を、送るプロンプトに足す (置き換えない)
            scene.record("元の文を足す前の文", before, before=True)
            current = self.prompt.toPlainText() if scene is self.scene else scene.prompt
            joined = inserts.join(current, text.strip())
            if scene is self.scene:
                self.prompt.setReadOnly(False)
                self._set_prompt(joined)  # Ctrl+Z で戻せるように
            else:
                scene.prompt = joined
            scene.record("英語にした元の文", text.strip())
            self._save_timer.start()
            self.status.emit("元の文を英語に直して足しました (元に戻す / Ctrl+Z で前の文に)。")
        else:
            label = "読み取り" if what == "読み取り" else "盛る"
            scene.record(f"{label}の前の文", before, before=True)
            if scene is self.scene:
                self.prompt.setReadOnly(False)
                self._set_prompt(text.strip())  # Ctrl+Z で戻せるように
            else:
                scene.prompt = text.strip()
            scene.record("読み取った文" if label == "読み取り" else "盛った文", text.strip())
            self._save_timer.start()
            self.status.emit(f"{what}ました。直してから「候補を作る」で送ります (元に戻す / Ctrl+Z で前の文に)。")
        self.refresh()

    def _read(self) -> None:
        if self.scene is None or not self.scene.source or not Path(self.scene.source).is_file():
            QMessageBox.information(self, "読み取る", "先に元画像を決めてください。")
            return
        found = [p for p in reader.load_presets() if not p.is_empty and p.name == self.reader_box.currentData()]
        if not found:
            QMessageBox.information(self, "読み取る", "読み取りプリセットが登録されていません (「読み取りの設定」)。")
            return
        snap, path = reader.snapshot(found[0]), Path(self.scene.source)
        self._run_llm("読み取り", lambda: reader.read_with(snap, path))

    def _enhance(self) -> None:
        text = self.prompt.toPlainText().strip() or (self.scene.original.strip() if self.scene else "")
        if not text:
            QMessageBox.information(self, "盛る", "盛る元の文がありません。")
            return
        found = [p for p in enhancer.load_presets() if not p.is_empty and p.name == self.enhancer_box.currentData()]
        if not found:
            QMessageBox.information(self, "盛る", "エンハンサーが登録されていません (「エンハンサーの設定」)。")
            return
        snap = enhancer.snapshot(found[0])
        self._run_llm("盛り", lambda: enhancer.enhance_with(snap, text))

    def _restore(self, text: str, what: str) -> None:
        """プロンプトを text に戻す。今の文は先に履歴に残す (何も失わないように)。"""
        s = self.scene
        if s is None or text == s.prompt:
            return
        s.record("戻す前の文", s.prompt)
        self._set_prompt(text)
        self._save_timer.start()
        self.status.emit(f"{what}。戻す前の文は「履歴...」に残っています。")
        self.refresh()

    def _undo_llm(self) -> None:
        """元に戻す / Ctrl+Z: 欄の編集を 1 つ戻す。戻すものがなければ (開き直したあと) 履歴の直前の操作の前の文へ。"""
        if self.scene is None or self._llm_busy:
            return
        if self.prompt.document().isUndoAvailable():
            self.prompt.undo()
            return
        target = self.scene.undo_target()
        if target is not None:
            self._restore(target, "前の文に戻しました")

    def _show_history(self) -> None:
        if self.scene is None:
            return
        if self.scene.prompt.strip():
            self.scene.record("今の文", self.scene.prompt)  # 手で直したものも、開いた時点で残す
        dialog = HistoryDialog(self.scene, self)
        if dialog.exec() and dialog.chosen is not None:
            self._restore(dialog.chosen, "履歴の文に戻しました")
        self._save_timer.start()

    def _insert(self) -> None:
        if self.scene is None:
            return
        last = store.read_text(store.app_file(INSERT_FOLDER_FILE)) or ""
        folder = QFileDialog.getExistingDirectory(self, "差し込みのフォルダを選ぶ", last.strip())
        if not folder:
            return
        try:
            picker = inserts.Picker([inserts.Slot(Path(folder).name, folder, True, False)])
            picked = picker.next()[0]
            picker.commit()
        except inserts.InsertError as exc:
            QMessageBox.warning(self, "差し込み", str(exc))
            return
        try:
            store.write_text(store.app_file(INSERT_FOLDER_FILE), folder)
        except OSError:
            pass
        self.scene.record("差し込みの前の文", self.prompt.toPlainText(), before=True)
        base = self.prompt.toPlainText() if self.prompt.toPlainText().strip() else self.scene.original
        self._set_prompt(inserts.join(base, picked["text"]))
        self.scene.record(f"差し込んだ文 ({picked['file']})", self.scene.prompt)
        self._save_timer.start()
        self.status.emit(f"差し込み: {picked['file']} を足しました。")

    # ------------------------------------------------------------------
    # 候補
    # ------------------------------------------------------------------
    def _gen_settings_changed(self, *_a) -> None:
        if self._loading or self.scene is None:
            return
        s = self.scene
        s.workflow = str(self.workflow_box.currentData() or "")
        s.seconds = self.seconds_box.value() if self.use_seconds.isChecked() else None
        s.base_prompt = self.base_box.isChecked()
        s.count = self.count_box.value()
        self.seconds_box.setEnabled(self.use_seconds.isChecked())
        self._save_timer.start()
        self._schedule()  # ワークフローでリファレンスの欄が変わる

    def _put_small(self, label: QLabel, path: str, empty: str, check: bool = False) -> None:
        """小さな欄に画像を出す (なければ empty の文)。"""
        pix = self.thumbs.get(path, check=check) if path else None
        if pix is not None and not pix.isNull():
            label.setPixmap(pix.scaled(label.size(), Qt.AspectRatioMode.KeepAspectRatio,
                                       Qt.TransformationMode.SmoothTransformation))
        else:
            label.setPixmap(QPixmap())
            label.setText(empty if not path else "")

    def _busy_job(self, scene: story.Scene) -> bool:
        """このシーンの候補を作っているか (知らせない)。"""
        return self.job is not None and self.job[1] == scene.id and self.engine.active

    def _busy_here(self, scene: story.Scene) -> bool:
        if self.job is not None and self.job[1] == scene.id and self.engine.active:
            self.status.emit("このシーンは候補を作っています。終わるか中止してから直してください。")
            return True
        return False

    def start_job(self, job, workflow: str, prompt: str, seconds, base: bool, count: int, source: str,
                  out_dir: str, reference: str = "", original: str = "", last_frame: str = "") -> bool:
        """候補 (または T2I の元画像) を作り始める。一度に 1 つだけ。"""
        run = self.engine.run
        if self.engine.active and run is not None and run.unfinished:
            QMessageBox.information(self, "候補を作る", "ほかのシーンの候補を作っています。終わってからにしてください。")
            return False
        machines = recipes.machines()
        if not machines:
            QMessageBox.warning(self, "候補を作る", "使う台が決まっていません (量産ラインの「台の設定...」で選んでください)。")
            return False
        if not workflow:
            QMessageBox.warning(self, "候補を作る", "ワークフローを選んでください (「生成の設定」で登録します)。")
            return False
        self.engine.set_machines(machines)
        recipe = recipes.Recipe(name=self.story.name, output_dir=out_dir, inherit=False,
                                stages=[recipes.Stage(recipes.GEN, workflow=workflow, seconds=seconds, base_prompt=base)])
        try:
            new = engine.Run.create(recipe, [Path(source)] * count)
        except (ValueError, comfy.ComfyError) as exc:
            QMessageBox.warning(self, "候補を作る", str(exc))
            return False
        for it in new.items:
            it.text = prompt or None  # 空ならワークフローに書かれたプロンプトのまま
            it.reference = reference
            it.last_frame = last_frame
        self.job, self._job_prompt = job, prompt
        self._job_original, self._job_reference, self._job_last = original, reference, last_frame
        self.engine.start(new)
        self.refresh()
        return True

    def _make_candidates(self) -> None:
        s = self.scene
        if s is None:
            return
        if not s.source or not Path(s.source).is_file():
            kind = kinds.kind_of(s.workflow)
            if kind.startswith("I") or not kind:
                QMessageBox.information(self, "候補を作る", "先に元画像を決めてください" +
                                        (" (「続き」は、前のシーンで採用すると入ります)。" if s.link == story.CONT else "。"))
                return
        self._gen_settings_changed()
        folder = self.story.scene_dir(s)
        folder.mkdir(parents=True, exist_ok=True)
        source = s.source if s.source and Path(s.source).is_file() else str(folder / "prompt.txt")
        if source.endswith("prompt.txt"):
            try:
                Path(source).write_text(s.send_text, encoding="utf-8")
            except OSError:
                pass
        reference = s.reference if s.reference and Path(s.reference).is_file() and kinds.has_reference(s.workflow) \
            else ""
        last_frame = ""
        if kinds.has_last_frame(s.workflow):
            last_frame = self.story.last_frame_of(s)
            if not last_frame:  # 渡さないと始めも終わりも元画像になるので、作らずに知らせる
                why = ("次のシーンの元画像がまだありません" if s.last_kind == story.LAST_NEXT
                       else "ラストフレームが決まっていません")
                QMessageBox.information(self, "候補を作る",
                                        f"このワークフローはラストフレームを指定するものですが、{why}。\n"
                                        "左の「ラストフレーム」の「選ぶ」で、手元の画像か次のシーンの元画像を選んでください。")
                return
            if s.last_kind == story.LAST_NEXT:
                # 次のシーンの元画像は書き直されることがあるので、作る時点の絵を写して使う (候補の記録もこれを指す)
                try:
                    last_frame = self.story.freeze_last_frame(s, last_frame)
                except story.StoryError as exc:
                    QMessageBox.warning(self, "候補を作る", str(exc))
                    return
        if self.start_job(("cand", s.id), s.workflow, s.send_text, s.seconds, s.base_prompt, s.count, source,
                          str(folder), reference=reference, original=s.original, last_frame=last_frame):
            s.record("候補に送った文", s.send_text)
        self._save()

    def _stop_job(self) -> None:
        if self.engine.active:
            self.engine.cancel_all()

    def _on_finished(self) -> None:
        run, items, _m = self.engine.snapshot()
        job = self.job
        if job is not None and job[0] == "cand":
            self.job = None  # T2I の元画像づくりは、窓が結果を拾うので印を残す
        if run is None or job is None:
            return
        if job[0] == "cand" and self.story is not None:
            scene = next((s for s in self.story.scenes if s.id == job[1]), None)
            if scene is not None:
                known = {c.path for c in scene.candidates}
                for it in items:
                    for o in it.outputs:
                        if o["path"] not in known and Path(o["path"]).exists():  # 作っている途中で消したものは戻さない
                            scene.candidates.append(story.Candidate(
                                o["path"], self._job_prompt, float(o.get("time") or time.time()),
                                self._job_original, self._job_reference, self._job_last))
                self._save()
                failed = [it.error for it in items if it.state == engine.FAILED]
                self.status.emit(f"シーン「{scene.label(self.story.index_of(scene))}」の候補が出来ました"
                                 + (f" (失敗 {len(failed)}: {failed[0].splitlines()[0]})" if failed else "。"))
        self.refresh(rebuild=True)

    def _cand_paths(self) -> list[str]:
        """選んでいるシーンの候補 (新しい順。作っている途中のものも)。"""
        if self.scene is None:
            return []
        paths = [c.path for c in sorted(self.scene.candidates, key=lambda c: c.time, reverse=True)]
        if self.job == ("cand", self.scene.id):
            _r, items, _m = self.engine.snapshot()
            fresh = [o["path"] for it in items for o in it.outputs if o["path"] not in paths]
            paths = fresh + paths
        return [p for p in paths if Path(p).exists()]

    def _show_candidate(self) -> None:
        item = self.cands.currentItem()
        path = str(item.data(Qt.ItemDataRole.UserRole)) if item else ""
        video = bool(path) and _is_video(path)
        src = self.scene.source if self.scene is not None and self.scene.source else ""
        has_src = bool(src) and Path(src).is_file()

        def put(label: QLabel, pix, empty: str) -> None:
            if pix is not None and not pix.isNull():
                label.setPixmap(pix.scaled(label.width(), label.height(), Qt.AspectRatioMode.KeepAspectRatio,
                                           Qt.TransformationMode.SmoothTransformation))
            else:
                label.setPixmap(QPixmap())
                label.setText("表示できません" if pix is not None else empty)  # null = 読めなかった

        put(self.preview_src, self.thumbs.get(src, check=True) if has_src else None,
            "読み込み中…" if has_src else "元画像なし")
        put(self.preview, self.thumbs.get(path) if path else None,
            "読み込み中…" if path else "候補を選ぶと、元画像と並べて出します (動画はダブルクリックで再生)")
        self.preview_last.setVisible(video)
        self.preview_caps[2].setVisible(video)
        if video:
            put(self.preview_last, self.thumbs.get(path, last=True), "読み込み中…")
        # その候補でラストフレームを指定していたら、候補の最後のコマと見比べられるように並べる
        cand = next((c for c in self.scene.candidates if c.path == path), None) if self.scene and path else None
        target = cand.last_frame if cand is not None and cand.last_frame and Path(cand.last_frame).is_file() else ""
        self.preview_target.setVisible(bool(target))
        self.preview_caps[3].setVisible(bool(target))
        if target:
            put(self.preview_target, self.thumbs.get(target), "読み込み中…")
        self.preview_caps[1].setText("候補 (始め)" if video else "候補")
        self.adopt_b.setEnabled(bool(path))
        self.adopt_only.setEnabled(bool(path))
        self.frame_next_b.setEnabled(bool(path) and _is_video(path) and Path(path).is_file())
        if path and self.scene is not None:
            self.adopt_only.setText("採用をやめる" if self.scene.adopted == path else "採用だけ")

    def _play_candidate(self, item: QListWidgetItem) -> None:
        path = str(item.data(Qt.ItemDataRole.UserRole))
        if _is_video(path):
            PlayDialog([(Path(path).name, path)], self).exec()
        elif Path(path).exists():
            os.startfile(path)  # noqa: S606

    def _candidate_menu(self, pos) -> None:
        item = self.cands.itemAt(pos)
        if item is None:
            return
        path = str(item.data(Qt.ItemDataRole.UserRole))
        if not item.isSelected():  # 選んでいないものを右クリックしたら、それだけを選ぶ
            self.cands.clearSelection()
            self.cands.setCurrentItem(item)
        count = len(self.cands.selectedItems())
        menu = QMenu(self)
        play = menu.addAction("再生 / 開く")
        folder = menu.addAction("フォルダを開く")
        menu.addSeparator()
        cand = next((c for c in self.scene.candidates if c.path == path), None) if self.scene else None
        back = menu.addAction("このときのプロンプトに戻す")
        back.setToolTip("この候補を作ったときの 元のプロンプト と 盛ったあとのプロンプト を欄に戻します (Ctrl+Z で戻せます)。")
        back.setEnabled(cand is not None and bool(cand.prompt or cand.original))
        from_frame = menu.addAction("このコマから次へ...")
        from_frame.setToolTip("この候補の好きなコマから、次のシーンを始めます (候補は採用します)。")
        from_frame.setEnabled(count == 1 and _is_video(path) and Path(path).is_file())
        menu.setToolTipsVisible(True)
        drop = menu.addAction("候補から外す (ファイルは残す)")
        delete = menu.addAction(f"削除 (ごみ箱へ){f' ― {count} 個' if count > 1 else ''}\tDel")
        chosen = menu.exec(self.cands.mapToGlobal(pos))
        if chosen is delete:
            self._delete_candidates()
            return
        if chosen is back and cand is not None:
            self._back_to_candidate(cand)
            return
        if chosen is from_frame:
            self._frame_next()
            return
        if chosen is play:
            self._play_candidate(item)
        elif chosen is folder:
            subprocess.Popen(["explorer", "/select,", path])  # noqa: S603,S607
        elif chosen is drop and self.scene is not None:
            self.scene.candidates = [c for c in self.scene.candidates if c.path != path]
            if self.scene.adopted == path:
                self.story.adopt(self.scene, path)
            self._changed(rebuild=True)

    def eventFilter(self, obj, event) -> bool:
        if obj is self.cands and event.type() == QEvent.Type.KeyPress and event.key() == Qt.Key.Key_Delete:
            if not event.isAutoRepeat():  # 押しっぱなしで続けて消えないように
                self._delete_candidates()
            return True
        return super().eventFilter(obj, event)

    @staticmethod
    def _captions(paths: list[Path]) -> list[Path]:
        """一緒に消す説明の .txt (同じ名前の別のファイルが残るときは残す。ui-standard 8 章)。"""
        out = []
        for p in paths:
            txt = p.with_suffix(".txt")
            if not txt.is_file() or txt in out:
                continue
            try:
                others = [q for q in p.parent.glob(p.stem + ".*") if q != txt and q not in paths]
            except OSError:
                continue
            if not others:
                out.append(txt)
        return out

    def _delete_candidates(self) -> None:
        """選んでいる候補のファイルをごみ箱へ。1 つなら確かめず、複数かごみ箱に入らないときは確かめる (ui-standard 7 章)。"""
        if self.scene is None or getattr(self, "_deleting", False):
            return
        items = self.cands.selectedItems() or ([self.cands.currentItem()] if self.cands.currentItem() else [])
        paths = [Path(str(i.data(Qt.ItemDataRole.UserRole))) for i in items]
        if not paths:
            return
        recyclable = all(recycle.can_recycle(p) for p in paths)
        if len(paths) > 1 or not recyclable:
            what = paths[0].name if len(paths) == 1 else f"選んでいる {len(paths)} 個の候補"
            text = f"{what}を削除 (ごみ箱へ) しますか？"
            if not recyclable:
                text = f"{what}を削除しますか？\n\nこの場所のファイルはごみ箱に入らず、完全に消えます。"
            if any(str(p) == self.scene.adopted for p in paths):
                text += "\n\n採用している候補も入っています (採用をやめます)。"
            if QMessageBox.question(self, "候補を削除", text) != QMessageBox.StandardButton.Yes:
                return
        first_row = min(self.cands.row(i) for i in items)
        for label in (self.preview, self.preview_last):  # 表示中のものを手放してから消す
            label.clear()
        self._deleting = True
        try:
            # サムネール (最後のコマ) を読んでいる途中なら、読み終わるのを待つ (読んでいるあいだは消せない)
            end = time.monotonic() + 3
            while self.thumbs.busy(paths) and time.monotonic() < end:
                QCoreApplication.processEvents(QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents, 50)
                time.sleep(0.02)
            error = recycle.send_to_recycle_bin(paths + self._captions(paths))
        finally:
            self._deleting = False
        gone = [p for p in paths if not p.exists()]
        scene = self.scene
        was_adopted = scene.adopted and any(str(p) == scene.adopted for p in gone)
        self.story.remove_candidates(scene, gone)
        self._save()
        self.refresh(rebuild=True)
        if self.cands.count():  # 消したあとは、同じ位置 (= 次) のものを選ぶ
            self.cands.setCurrentRow(min(first_row, self.cands.count() - 1))
        note = " (採用をやめました)" if was_adopted else ""
        if error and len(gone) < len(paths):
            self.status.emit(f"{error}  (残った {len(paths) - len(gone)} 個 / ごみ箱へ移動 {len(gone)} 個)")
        elif len(gone) == 1:
            self.status.emit(f"ごみ箱へ移動しました: {gone[0].name}{note}")
        elif gone:
            self.status.emit(f"ごみ箱へ移動しました: {len(gone)} 個{note}")

    def _back_to_candidate(self, cand: story.Candidate) -> None:
        """候補を作ったときのプロンプトを欄に戻す (途中から直して作り直すため)。今の文は履歴に残る。"""
        s = self.scene
        if s is None:
            return
        if cand.original and cand.original != s.original:
            s.record("戻す前の元のプロンプト", s.original)
            self._set_text(self.original, cand.original)
        if cand.prompt and cand.prompt != s.prompt:
            s.record("戻す前の文", s.prompt, before=True)
            self._set_prompt(cand.prompt)
        self._save_timer.start()
        self.status.emit(f"{Path(cand.path).name} を作ったときのプロンプトに戻しました (Ctrl+Z で戻せます)。")
        self.refresh()

    def _adopt(self, go_next: bool) -> None:
        item = self.cands.currentItem()
        if item is None or self.scene is None:
            return
        path = str(item.data(Qt.ItemDataRole.UserRole))
        if go_next and self.scene.adopted == path:
            pass  # 採用のまま次へ
        else:
            try:
                self.story.adopt(self.scene, path)
            except story.StoryError as exc:
                QMessageBox.warning(self, "採用", str(exc))
                return
        self._save()
        if go_next and self.scene.adopted:
            i = self.story.index_of(self.scene)
            if i + 1 < len(self.story.scenes):
                self.scene = self.story.scenes[i + 1]
                self.refresh(rebuild=True, load=True)
            else:
                self._add_scene(self.scene, story.CONT)
            return
        self.refresh(rebuild=True)

    def _frame_next(self) -> None:
        """候補の好きなコマから次のシーンを始める (候補を採用し、次のシーンの元画像をそのコマに)。"""
        item = self.cands.currentItem()
        if item is None or self.scene is None:
            return
        path = str(item.data(Qt.ItemDataRole.UserRole))
        if not _is_video(path) or not Path(path).is_file():
            return
        scene = self.scene
        i = self.story.index_of(scene)
        nxt = self.story.scenes[i + 1] if i + 1 < len(self.story.scenes) else None
        if nxt is not None and self._busy_here(nxt):
            return
        try:
            dialog = FrameDialog(path, self, "コマを選んで次へ")
        except story.StoryError as exc:
            QMessageBox.warning(self, "コマを選んで次へ", str(exc))
            return
        if not dialog.exec():
            return
        if nxt is not None and self.story.next_source_taken(scene, path):
            if QMessageBox.question(self, "コマを選んで次へ",
                                    f"次のシーン「{nxt.label(i + 1)}」には、もう元画像が入っています。\n"
                                    "選んだコマに置き換えますか? (候補はそのまま残ります)",
                                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                    QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
                return
        try:
            nxt = self.story.start_next_from_frame(scene, path, dialog.frame, last=dialog.is_last)
        except story.StoryError as exc:
            QMessageBox.warning(self, "コマを選んで次へ", str(exc))
            return
        self._save()
        self.scene = nxt
        self.thumbs.forget(nxt.source)
        self.status.emit(f"{Path(path).name} の {dialog.frame + 1} コマ目から、シーン「{nxt.label(i + 1)}」を始めます。")
        self.refresh(rebuild=True, load=True)

    # ------------------------------------------------------------------
    # 上の行
    # ------------------------------------------------------------------
    def _play(self) -> None:
        if self.story is None:
            return
        start = self.story.index_of(self.scene) if self.scene else 0
        playlist, prev_ok = [], False
        for i, s in enumerate(self.story.scenes):
            ok = bool(s.adopted) and Path(s.adopted).is_file() and _is_video(s.adopted)
            if i >= start and ok:
                # 続きのシーンで、前のシーンのクリップを直前に流すときは、同じ絵の 1 コマ目を飛ばす
                cont = s.link == story.CONT and prev_ok and bool(playlist) and \
                    s.source_from == self.story.scenes[i - 1].adopted
                playlist.append((s.label(i), s.adopted, cont))
            prev_ok = ok
        if not playlist:
            QMessageBox.information(self, "通して再生", "選んでいるシーンから先に、採用した動画がありません。")
            return
        PlayDialog(playlist, self).exec()

    def _open_adopted(self) -> None:
        if self.story is None:
            return
        self.story.sync_adopted()
        folder = self.story.adopted_dir()
        if folder.is_dir():
            os.startfile(str(folder))  # noqa: S606
        else:
            self.status.emit("まだ採用した動画がありません。")

    def _pick_in_jrate(self) -> None:
        """前に元画像などを選んだフォルダを J-Rate で開く (選んで「PrismLab に送る」で送り返してもらう)。"""
        from .line_view import find_jrate
        exe = find_jrate()
        if exe is None:
            self.status.emit("J-Rate が見つかりません (デスクトップの Application か、隣の J-Rate のフォルダに置いてください)。")
            return
        folder = story.pick_folder()
        subprocess.Popen([str(exe), folder] if folder else [str(exe)])  # noqa: S603
        self.status.emit("J-Rate で画像を選び、右クリックの「PrismLab に送る」で物語に送ってください。")

    @staticmethod
    def _image_urls(event) -> list[str]:
        urls = event.mimeData().urls() if event.mimeData().hasUrls() else []
        return [u.toLocalFile() for u in urls
                if u.isLocalFile() and u.toLocalFile().lower().endswith(recipes.IMAGE_EXTS)]

    def _board_drag(self, event) -> None:
        if self.story is not None and self._image_urls(event):
            event.acceptProposedAction()

    def _board_drop(self, event) -> None:
        paths = self._image_urls(event)
        if self.story is None or not paths:
            return
        event.acceptProposedAction()
        pos = event.position().toPoint()
        after = None
        for card, scene in zip(self.scene_cards, self.story.scenes):
            if card.geometry().contains(pos):
                after = scene
                break
        QTimer.singleShot(0, lambda: self._add_image_scenes(paths, after))

    def _add_image_scenes(self, paths: list, after: Optional[story.Scene] = None) -> str:
        """画像を 1 枚ずつ、シーンチェンジ・手元の画像のシーンにして足す (after のあと、なければ最後)。"""
        added = []
        error = ""
        for path in paths:
            scene = self.story.add_scene(after, story.CUT)
            try:
                self.story.set_source_file(scene, path)
            except story.StoryError as exc:
                self.story.remove_scene(scene)
                error = str(exc)
                break
            added.append(scene)
            after = scene
        if paths:
            story.remember_pick(paths[0])
        if added:
            self.scene = added[-1]
            self._changed(rebuild=True)
            self.refresh(load=True)
            self.status.emit(f"シーンを {len(added)} 個足しました (シーンチェンジ・手元の画像)。")
        if error:
            QMessageBox.warning(self, "シーンを足す", error)
        return error

    def receive(self, files: list, slot: str, sender: str = "") -> str:
        """ほかのアプリから送られたもの (spec 5 章)。入れられなければ理由を返す。"""
        if self.story is None:
            return "物語を開いていません。PrismLab の「物語」で物語を開いてから、もう一度送ってください。"
        found = [f for f in files if Path(f).is_file()]
        if not found:
            return "送られたファイルが見つかりません。"
        if slot == "scenes":
            images = [f for f in found if f.lower().endswith(recipes.IMAGE_EXTS)]
            if not images:
                return "送れるのは画像だけです。"
            self._save()
            return self._add_image_scenes(images) or ""
        if self.scene is None:
            return "シーンを選んでいません。"
        if self.job is not None and self.job[1] == self.scene.id and self.engine.active:
            return "選んでいるシーンは候補を作っています。終わるか中止してから、もう一度送ってください。"
        path = found[0]
        label = self.scene.label(self.story.index_of(self.scene))
        if slot == "original":
            text = inserts.read_file(Path(path))
            if not text:
                return f"{Path(path).name} の中身が空です。"
            old = self.original.toPlainText()
            self.scene.record("元のプロンプト (送られる前)", old)
            self._set_text(self.original, text)
            self._save_timer.start()
            self.status.emit(f"{sender or 'ほかのアプリ'} の {Path(path).name} を、シーン「{label}」の元のプロンプトに入れました"
                             " (Ctrl+Z で戻せます)。")
            return ""
        if not path.lower().endswith(recipes.IMAGE_EXTS):
            return "送れるのは画像だけです。"
        story.remember_pick(path)
        if slot == "source":
            try:
                self.story.set_source_file(self.scene, path)
            except story.StoryError as exc:
                return str(exc)
            self.thumbs.forget(self.scene.source)
            self._changed(rebuild=True)
            what = "元画像"
        elif slot == "reference":
            try:
                self.story.set_reference(self.scene, path)
            except story.StoryError as exc:
                return str(exc)
            self.thumbs.forget(self.scene.reference)
            self._changed()
            what = "リファレンス (顔)"
        elif slot == "last":
            try:
                self.story.set_last_frame(self.scene, path)
            except story.StoryError as exc:
                return str(exc)
            if self.scene.last_frame:
                self.thumbs.forget(self.scene.last_frame)
            self._changed()
            what = "ラストフレーム"
        else:
            return ""
        self.status.emit(f"{Path(path).name} を、シーン「{label}」の{what}にしました。")
        return ""

    def _open_jrate(self) -> None:
        from .line_view import find_jrate
        if self.story is None or self.scene is None:
            return
        folder = self.story.scene_dir(self.scene)
        exe = find_jrate()
        if exe is None or not folder.is_dir():
            self.status.emit("J-Rate が見つからないか、まだ候補がありません。")
            return
        subprocess.Popen([str(exe), str(folder)])  # noqa: S603

    # ------------------------------------------------------------------
    # 描き直し
    # ------------------------------------------------------------------
    def _schedule(self) -> None:
        if not self._timer.isActive():
            self._timer.start()

    def _load_scene(self) -> None:
        """選んだシーンの中身を欄に入れる (入れているあいだは、欄の変化をシーンに書かない)。"""
        s = self.scene
        self._loading = True
        try:
            self.original.setPlainText(s.original if s else "")
            self.prompt.setPlainText(s.prompt if s else "")  # シーンを変えたら、Ctrl+Z の戻り先もそのシーンから
            _fill_workflows(self.workflow_box, s.workflow if s else "")
            if s is not None and not s.workflow and self.workflow_box.count():
                # 初めは、ファイルのある I2V (なければファイルのある最初のもの) を選んでおく
                paths = [str(self.workflow_box.itemData(i)) for i in range(self.workflow_box.count())]
                usable = [i for i, path in enumerate(paths) if Path(path).is_file()]
                video = [i for i in usable if kinds.kind_of(paths[i]) == "I2V"]
                pick = (video or usable or [0])[0]
                self.workflow_box.setCurrentIndex(pick)
                s.workflow = paths[pick]
            self.use_seconds.setChecked(bool(s and s.seconds is not None))
            self.seconds_box.setValue(s.seconds if s and s.seconds else 5)
            self.seconds_box.setEnabled(bool(s and s.seconds is not None))
            self.base_box.setChecked(bool(s.base_prompt) if s else True)
            self.count_box.setValue(s.count if s else 3)
            self._fill_llm_boxes()
        finally:
            self._loading = False

    def refresh(self, rebuild: bool = False, load: bool = False) -> None:
        has_story = self.story is not None
        self.detail.setVisible(has_story)
        self.empty.setVisible(not has_story)
        for b in (self.play_b, self.adopted_b, self.jrate_b):
            b.setEnabled(has_story)
        if rebuild or not hasattr(self, "scene_cards"):
            self._rebuild_board()
        if not has_story:
            return
        if self.scene not in self.story.scenes:
            self.scene, load = self.story.scenes[0], True
        if load:
            self._load_scene()
        s = self.scene
        index = self.story.index_of(s)

        # 絵コンテ
        for i, (scene, card) in enumerate(zip(self.story.scenes, self.scene_cards)):
            card.set_selected(scene is s)
            card.name.setText(scene.label(i))
            shown = scene.adopted if scene.adopted else scene.source
            pix = self.thumbs.get(shown, check=True) if shown and Path(shown).exists() else None
            card.thumb.setPixmap(_framed(pix, SCENE_THUMB, OK_COLOR if scene.adopted else ""))
            busy = self.job == ("cand", scene.id) and self.engine.active
            if busy:
                text = "候補を作っています…"
            elif scene.adopted:
                text = "採用済み"
            elif scene.candidates:
                text = f"候補 {len(scene.candidates)} · 未採用"
            elif not scene.source:
                text = "元画像がまだ" if scene.link == story.CUT or index == 0 else "前のシーンの採用待ち"
            else:
                text = "候補なし"
            card.state.setText(text)

        # 元画像
        first = index == 0
        self.link_cont.setEnabled(not first)
        self.link_cut.setEnabled(not first)
        (self.link_cont if s.link == story.CONT and not first else self.link_cut).setChecked(True)
        cut = s.link == story.CUT or first
        for b in (self.pick_file, self.pick_t2i):
            b.setVisible(cut)
        self.pick_frame.setVisible(cut and not first)
        # 元画像はいつも同じ source.png に書き直すので、更新時刻を見て読み直す
        pix = self.thumbs.get(s.source, check=True) if s.source and Path(s.source).exists() else None
        if pix is not None and not pix.isNull():
            self.source_view.setPixmap(pix.scaled(self.source_view.size(), Qt.AspectRatioMode.KeepAspectRatio,
                                                  Qt.TransformationMode.SmoothTransformation))
        else:
            self.source_view.setPixmap(QPixmap())
            self.source_view.setText("前のシーンで採用すると、その最後のコマが入ります" if not cut else
                                     "画像を選ぶか、ここにドラッグ\n(T2I のワークフローなら元画像なしでも作れます)")
        self.source_note.setText(story.SOURCE_TITLES.get(s.source_kind, "") if s.source else "")
        can_ref = kinds.has_reference(s.workflow)
        has_ref = bool(s.reference) and Path(s.reference).is_file()
        self.ref_pick.setEnabled(can_ref)
        self.ref_clear.setEnabled(has_ref)
        self._put_small(self.ref_view, s.reference if has_ref else "", "なし" if can_ref else "対応なし")
        ref_note = ("リファレンス用の LoadImage に入れます。" if has_ref else
                    "リファレンスを選ばなければ、リファレンス用の LoadImage にも元画像を入れます。") if can_ref else \
            ("リファレンス: このワークフローは対応していません" + (" (選んだ画像は残っています)。" if has_ref else "。"))
        self.ref_view.setToolTip((s.reference + "\n\n" if has_ref else "") +
                                 "LoadImage のタイトルに「face」「reference」「顔」「参照」などを入れたワークフローで使えます。")
        # ラストフレーム (ファーストフレーム・ラストフレームを指定するワークフロー)
        can_last = kinds.has_last_frame(s.workflow)
        last = self.story.last_frame_of(s)
        follow = s.last_kind == story.LAST_NEXT
        nxt = self.story.scenes[index + 1] if index + 1 < len(self.story.scenes) else None
        self.last_pick.setEnabled(can_last and not self._busy_job(s))
        self.last_next_action.setEnabled(nxt is not None)
        self.last_clear.setEnabled(bool(s.last_kind) and not self._busy_job(s))
        if follow and not last:
            empty = "次のシーンがありません" if nxt is None else "次のシーンの元画像がまだ"
        else:
            empty = "なし" if can_last else "対応なし"
        self._put_small(self.last_view, last, empty, check=follow)
        if can_last:
            last_note = ("ラストフレーム: 次のシーンの元画像を使います (変わったら追いかけます)。" if follow else
                         "ラストフレーム用の LoadImage に入れます。" if last else
                         "ラストフレームを選んでから候補を作ります。")
            if follow and nxt is not None and nxt.link == story.CONT:
                last_note += " 次のシーンは「続き」なので、このシーンの採用の最後のコマです。"
        else:
            last_note = "ラストフレーム: このワークフローは対応していません" + \
                (" (選んだ画像は残っています)。" if s.last_kind else "。") if s.last_kind else ""
        self.last_view.setToolTip((last + "\n\n" if last else "") +
                                  "LoadImage のタイトルに「last」「end」「ラスト」「最後」などを入れたワークフロー"
                                  " (ファーストフレームとラストフレームを指定するもの) で使えます。")
        self.ref_note.setText("\n".join(t for t in (ref_note if (can_ref or has_ref) else "", last_note) if t))

        # プロンプト
        llm = bool(self._llm_busy)
        self.llm_state.setText(f"{self._llm_busy}中…" if llm else "")
        for b in (self.read_b, self.enhance_b, self.insert_b):
            b.setEnabled(not llm)
        self.undo_b.setEnabled(not llm and (self.prompt.document().isUndoAvailable() or s.undo_target() is not None))
        self.add_original_b.setEnabled(not llm and bool(s.original.strip()))
        self.to_english.setEnabled(not llm)
        self.history_b.setEnabled(not llm)
        self.prompt.setReadOnly(llm)

        # 候補
        run, items, _m = self.engine.snapshot()
        running = self.engine.active and run is not None and run.unfinished > 0
        mine = running and self.job is not None and self.job[1] == s.id
        if mine:
            done = sum(1 for i in items if i.finished)
            now = next((i.status for i in items if i.state == engine.RUNNING and i.status), "")
            self.gen_state.setText(f"作っています {done} / {len(items)}" + (f" ・ {now}" if now else ""))
        elif running:
            self.gen_state.setText("ほかのシーンの候補を作っています")
        else:
            self.gen_state.setText("")
        self.make_b.setEnabled(not running)
        self.stop_b.setVisible(mine)
        paths = self._cand_paths()
        current = self.cands.currentItem()
        keep = str(current.data(Qt.ItemDataRole.UserRole)) if current else ""
        key = (s.id, tuple(paths), s.adopted)
        if key != getattr(self, "_cands_key", None):
            self._cands_key = key
            self.cands.blockSignals(True)
            self.cands.clear()
            for path in paths:
                item = QListWidgetItem(("✔ 採用  " if path == s.adopted else "") + Path(path).name)
                item.setData(Qt.ItemDataRole.UserRole, path)
                item.setSizeHint(self.cands.gridSize())
                cand = next((c for c in s.candidates if c.path == path), None)
                tip = path
                if cand is not None:
                    tip += (f"\n\n元のプロンプト: {cand.original}" if cand.original else "") + \
                           (f"\n盛ったあとのプロンプト: {cand.prompt}" if cand.prompt else "") + \
                           (f"\nリファレンス: {Path(cand.reference).name}" if cand.reference else "") + \
                           (f"\nラストフレーム: {cand.last_frame}" if cand.last_frame else "")
                item.setToolTip(tip)
                self.cands.addItem(item)
                if path == keep or (not keep and path == s.adopted):
                    self.cands.setCurrentItem(item)
            self.cands.blockSignals(False)
        for n in range(self.cands.count()):
            item = self.cands.item(n)
            path = str(item.data(Qt.ItemDataRole.UserRole))
            if item.icon().isNull():
                pix = self.thumbs.get(path)
                if pix is not None:
                    item.setIcon(QIcon(_framed(pix, CAND_ICON, OK_COLOR if path == s.adopted else "")))
        self._show_candidate()

    def settings_changed(self) -> None:
        """生成・読み取り・エンハンサーの設定を変えたとき: 選ぶ欄を作り直す (選んでいたものは残す)。"""
        if self.story is not None:
            self.refresh(rebuild=True, load=True)

    def busy(self) -> bool:
        run = self.engine.run
        return self.engine.active and run is not None and run.unfinished > 0

    def shutdown(self) -> None:
        self._save()
        if self.busy():
            self.engine.discard()
        else:
            self.engine.stop_threads()
