"""一覧のサムネールにカーソルを乗せると、上に大きく出す (量産ラインの「結果」)。

乗せて少し待つと出し、離れる・押す・ホイール・キー・ほかのウィンドウに移ると消す。
大きい絵は別のサムネール (PREVIEW_SIZE) を裏で作る。出来るまでは小さいサムネールを伸ばして出す。
動画は、始めのコマを出したうえで音なし・繰り返しで再生する (コマは QVideoSink で受けて同じ欄に描く。
ToolTip の窓に QVideoWidget を入れると描画が乱れやすいため)。
窓はフォーカスを取らず、マウスも素通りさせる (一覧の操作を邪魔しない)。
"""

from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import QEvent, QObject, QPoint, QRect, QSize, Qt, QTimer, QUrl
from PySide6.QtGui import QGuiApplication, QPixmap
from PySide6.QtWidgets import QAbstractItemView, QLabel, QVBoxLayout, QWidget

from . import theme
from .thumbs import MOVIE_EXTS, ThumbCache

PREVIEW_SIZE = 512
DELAY_MS = 300
GAP = 8


class _Popup(QWidget):
    def __init__(self, parent: QWidget):
        # 親を付ける (ToolTip の窓なので別の窓として出る)。親なしだと、アプリを閉じたあとに消されて落ちる
        super().__init__(parent, Qt.WindowType.ToolTip | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)  # 枠を描くため
        self.setObjectName("hoverPreview")
        self.setStyleSheet(f"#hoverPreview {{ background: #111; border: 1px solid {theme.ACCENT}; }}")
        box = QVBoxLayout(self)
        box.setContentsMargins(4, 4, 4, 4)
        box.setSpacing(2)
        self.image = QLabel()
        self.image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image.setStyleSheet("border: none;")
        self.caption = QLabel()
        self.caption.setStyleSheet(f"border: none; color: {theme.TEXT_DIM}; font-size: 11px;")
        box.addWidget(self.image)
        box.addWidget(self.caption)


class HoverPreview(QObject):
    """view (QListWidget など) の項目に乗せたら大きく出す。path_of(index) でその項目のファイルを返す (なければ空)。"""

    def __init__(self, view: QAbstractItemView, path_of: Callable, small: Optional[ThumbCache] = None):
        super().__init__(view)
        self.view = view
        self.path_of = path_of
        self.small = small
        self.big = ThumbCache(PREVIEW_SIZE, self)
        self.big.changed.connect(self._loaded)
        self.popup = _Popup(view)
        self.path = ""  # 出している (出そうとしている) ファイル
        self._size = QSize()  # 絵を出す大きさ
        self._player = None  # 動画を再生するもの (初めて動画に乗せたときに作る)
        self._sink = None
        self._playing = ""  # 再生している動画
        self._rect = QRect()  # その項目の場所 (view の viewport の座標)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(DELAY_MS)
        self._timer.timeout.connect(self._show)
        view.viewport().setMouseTracking(True)
        view.viewport().installEventFilter(self)
        view.installEventFilter(self)

    def hide(self) -> None:
        self._timer.stop()
        self.path = ""
        self._stop_video()
        self.popup.hide()

    def eventFilter(self, obj, event) -> bool:
        kind = event.type()
        if kind == QEvent.Type.MouseMove and obj is self.view.viewport():
            self._moved(event.position().toPoint())
        elif kind in (QEvent.Type.Leave, QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonDblClick,
                      QEvent.Type.Wheel, QEvent.Type.KeyPress, QEvent.Type.Hide, QEvent.Type.WindowDeactivate,
                      QEvent.Type.ContextMenu):
            self.hide()
        return False

    def _moved(self, pos: QPoint) -> None:
        index = self.view.indexAt(pos)
        path = str(self.path_of(index) or "") if index.isValid() else ""
        if not path or not Path(path).exists():
            self.hide()
            return
        if path == self.path:
            return
        self.path = path
        self._rect = self.view.visualRect(index)
        if self.popup.isVisible():
            self._show()  # 出ているあいだは、隣へ移ったらすぐ替える
        else:
            self._timer.start()

    def _loaded(self, path: str) -> None:
        if self.popup.isVisible() and path.lower() == self.path.lower():
            self._show()

    def _show(self) -> None:
        if not self.path or not self.view.isVisible() or not self.view.window().isActiveWindow():
            return
        pix = self.big.get(self.path)
        if pix is None or pix.isNull():  # 大きいものを作っているあいだは、小さいものを伸ばす
            small = self.small.get(self.path) if self.small is not None else None
            if small is None or small.isNull():
                if pix is not None:  # 大きいものが作れなかった (読めない)
                    self.hide()
                return
            pix = small
        video = Path(self.path).suffix.lower() in MOVIE_EXTS
        if not (video and self._playing == self.path):  # 再生が始まっていれば、コマのほうを出したままにする
            self._size = pix.size().scaled(QSize(PREVIEW_SIZE, PREVIEW_SIZE), Qt.AspectRatioMode.KeepAspectRatio)
            self.popup.image.setPixmap(pix.scaled(self._size, Qt.AspectRatioMode.KeepAspectRatio,
                                                  Qt.TransformationMode.SmoothTransformation))
        name = Path(self.path).name
        self.popup.caption.setText(("▶ 動画 · " if video else "") + name)
        self.popup.adjustSize()
        self._place()
        self.popup.show()
        self.popup.raise_()
        if video:
            self._play(self.path)
        else:
            self._stop_video()

    # ---- 動画 ----
    def _play(self, path: str) -> None:
        if self._playing == path:
            return
        if self._player is None:
            from PySide6.QtMultimedia import QMediaPlayer, QVideoSink
            self._player = QMediaPlayer(self)  # 音の出口を付けない = 音なし
            self._sink = QVideoSink(self)
            self._player.setVideoOutput(self._sink)
            self._player.setLoops(QMediaPlayer.Loops.Infinite)
            self._sink.videoFrameChanged.connect(self._frame)
        self._playing = path
        self._player.setSource(QUrl.fromLocalFile(path))
        self._player.play()

    def _stop_video(self) -> None:
        if self._player is not None and self._playing:
            self._playing = ""
            self._player.stop()
            self._player.setSource(QUrl())

    def _frame(self, frame) -> None:
        if not self._playing or self._playing != self.path or not self.popup.isVisible() or not frame.isValid():
            return
        image = frame.toImage()
        if image.isNull():
            return
        self.popup.image.setPixmap(QPixmap.fromImage(image).scaled(
            self._size, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))

    def _place(self) -> None:
        """項目の上に、左右は項目の真ん中にそろえて出す。上に入らなければ下、それでもだめなら画面の中に収める。"""
        item = QRect(self.view.viewport().mapToGlobal(self._rect.topLeft()), self._rect.size())
        screen = QGuiApplication.screenAt(item.center()) or QGuiApplication.primaryScreen()
        area = screen.availableGeometry()
        w, h = self.popup.width(), self.popup.height()
        x = item.center().x() - w // 2
        x = max(area.left(), min(x, area.right() - w))
        y = item.top() - GAP - h
        if y < area.top():
            y = item.bottom() + GAP
        y = max(area.top(), min(y, area.bottom() - h))
        self.popup.move(x, y)
