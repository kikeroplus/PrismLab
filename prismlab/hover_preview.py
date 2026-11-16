"""一覧のサムネールにカーソルを乗せると、横に大きく出す (量産ラインの「結果」)。

乗せて少し待つと出し、離れる・押す・ホイール・キー・ほかのウィンドウに移ると消す。
大きい絵は別のサムネール (PREVIEW_SIZE) を裏で作る。出来るまでは小さいサムネールを伸ばして出す。
窓はフォーカスを取らず、マウスも素通りさせる (一覧の操作を邪魔しない)。
"""

from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import QEvent, QObject, QPoint, QRect, QSize, Qt, QTimer
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
        size = pix.size().scaled(QSize(PREVIEW_SIZE, PREVIEW_SIZE), Qt.AspectRatioMode.KeepAspectRatio)
        self.popup.image.setPixmap(pix.scaled(size, Qt.AspectRatioMode.KeepAspectRatio,
                                              Qt.TransformationMode.SmoothTransformation))
        name = Path(self.path).name
        video = Path(self.path).suffix.lower() in MOVIE_EXTS
        self.popup.caption.setText(("▶ 動画 (始めのコマ) · " if video else "") + name)
        self.popup.adjustSize()
        self._place()
        self.popup.show()
        self.popup.raise_()

    def _place(self) -> None:
        """項目の右に出す。入らなければ左、それでもだめなら画面の中に収める。"""
        item = QRect(self.view.viewport().mapToGlobal(self._rect.topLeft()), self._rect.size())
        screen = QGuiApplication.screenAt(item.center()) or QGuiApplication.primaryScreen()
        area = screen.availableGeometry()
        w, h = self.popup.width(), self.popup.height()
        x = item.right() + GAP
        if x + w > area.right():
            x = item.left() - GAP - w
        x = max(area.left(), min(x, area.right() - w))
        y = item.center().y() - h // 2
        y = max(area.top(), min(y, area.bottom() - h))
        self.popup.move(x, y)
