"""量産ラインの画面の部品: 種類の札・段の札・台の札。色は kinds (今の色分け) から取る。"""

from typing import Optional

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QProgressBar, QSizePolicy, QToolButton,
                               QVBoxLayout, QWidget)

from . import theme

THUMB = QSize(56, 56)
CARD_WIDTH = 210
FAIL_COLOR = "#ef5350"
DOWN_COLOR = "#ef5350"
OK_DOT = "#66bb6a"


class KindChip(QLabel):
    """種類の札 (縁と文字を種類の色にした丸い札)。"""

    def __init__(self, text: str = "", color: str = "#777777", parent=None):
        super().__init__(parent)
        self.set(text, color)

    def set(self, text: str, color: str) -> None:
        self.setText(text)
        self.setVisible(bool(text))
        self.setStyleSheet(f"QLabel {{ color: {color}; border: 1.5px solid {color}; border-radius: 9px;"
                           f" padding: 0px 7px; font-weight: bold; font-size: 11px; background: transparent; }}")


class Thumbs(QWidget):
    """サムネールを 3 つまで横に並べる。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(4)
        self.labels = []
        for _ in range(3):
            label = QLabel()
            label.setFixedSize(THUMB)
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            label.setStyleSheet("QLabel { background: #161616; border-radius: 4px; }")
            row.addWidget(label)
            self.labels.append(label)
        row.addStretch(1)

    def set(self, pixmaps: list, failed: Optional[list] = None) -> None:
        failed = failed or []
        for i, label in enumerate(self.labels):
            pix = pixmaps[i] if i < len(pixmaps) else None
            border = f"border: 2px solid {FAIL_COLOR};" if i < len(failed) and failed[i] else ""
            label.setStyleSheet(f"QLabel {{ background: #161616; border-radius: 4px; {border} }}")
            if pix is None or pix.isNull():
                label.clear()
            else:
                label.setPixmap(pix.scaled(THUMB, Qt.AspectRatioMode.KeepAspectRatio,
                                           Qt.TransformationMode.SmoothTransformation))


class Card(QFrame):
    """段の札。クリックで clicked、右クリックで menu_requested(位置)。"""

    clicked = Signal()
    menu_requested = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedWidth(CARD_WIDTH)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._color = "#555555"
        self._active = False
        self.box = QVBoxLayout(self)
        self.box.setContentsMargins(10, 8, 10, 8)
        self.box.setSpacing(5)

        head = QHBoxLayout()
        head.setSpacing(6)
        self.title = QLabel()
        self.title.setStyleSheet("font-weight: bold; font-size: 13px; background: transparent;")
        self.chip = KindChip()
        head.addWidget(self.title)
        head.addStretch(1)
        head.addWidget(self.chip)
        self.box.addLayout(head)

        self.io = QLabel()
        self.io.setTextFormat(Qt.TextFormat.RichText)
        self.io.setStyleSheet("background: transparent; font-size: 11px;")
        self.box.addWidget(self.io)

        self.detail = QLabel()
        self.detail.setWordWrap(True)
        self.detail.setStyleSheet(f"background: transparent; color: {theme.TEXT_DIM}; font-size: 11px;")
        self.box.addWidget(self.detail)

        self.thumbs = Thumbs()
        self.box.addWidget(self.thumbs)

        self.bar = QProgressBar()
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(6)
        self.box.addWidget(self.bar)

        self.count = QLabel()
        self.count.setStyleSheet(f"background: transparent; color: {theme.TEXT_DIM}; font-size: 11px;")
        self.box.addWidget(self.count)
        self.box.addStretch(1)
        self._restyle()

    def set_color(self, color: str, active: bool = False) -> None:
        self._color, self._active = color, active
        self._restyle()

    def _restyle(self) -> None:
        width = 3 if self._active else 2
        self.setStyleSheet(
            f"Card {{ background: #262626; border: {width}px solid {self._color}; border-radius: 9px; }}"
            f"QProgressBar {{ background: #161616; border: none; border-radius: 3px; }}"
            f"QProgressBar::chunk {{ background: {self._color}; border-radius: 3px; }}")

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(event.position().toPoint()):
            self.clicked.emit()
        super().mouseReleaseEvent(event)

    def contextMenuEvent(self, event):
        self.menu_requested.emit(event.globalPos())


def io_html(parts: list) -> str:
    """[("画像", 色), "→", ("動画", 色)] を、色付きの文字で並べる。"""
    out = []
    for p in parts:
        if isinstance(p, tuple):
            out.append(f'<span style="color:{p[1]}; font-weight:bold">{p[0]}</span>')
        else:
            out.append(f'<span style="color:{theme.TEXT_DIM}">{p}</span>')
    return " ".join(out)


def arrow() -> QLabel:
    label = QLabel("→")
    label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    label.setFixedWidth(22)
    label.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 16px;")
    return label


class MachineCard(QFrame):
    """台の札 (状態の丸・名前・出来た数・いまの進み具合)。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumWidth(220)
        box = QVBoxLayout(self)
        box.setContentsMargins(10, 6, 10, 6)
        box.setSpacing(3)
        head = QHBoxLayout()
        self.dot = QLabel("●")
        self.name = QLabel()
        self.name.setStyleSheet("font-weight: bold; background: transparent;")
        self.done = QLabel()
        self.done.setStyleSheet(f"color: {theme.TEXT_DIM}; background: transparent;")
        head.addWidget(self.dot)
        head.addWidget(self.name, 1)
        head.addWidget(self.done)
        box.addLayout(head)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px; background: transparent;")
        box.addWidget(self.status)
        self.set_down(False)

    def set_down(self, down: bool, color: str = OK_DOT) -> None:
        self.dot.setStyleSheet(f"color: {DOWN_COLOR if down else color}; background: transparent;")
        style = "dashed" if down else "solid"
        self.setStyleSheet(f"MachineCard {{ background: #222222; border: 1.5px {style} #444444; border-radius: 8px; }}")


class AddButton(QToolButton):
    def __init__(self, text: str, parent=None):
        super().__init__(parent)
        self.setText(text)
        self.setFixedWidth(90)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.setStyleSheet("QToolButton { background: transparent; border: 1.5px dashed #555; border-radius: 9px;"
                           f" color: {theme.TEXT_DIM}; }}"
                           "QToolButton:hover { border-color: #888; color: white; }"
                           "QToolButton::menu-indicator { image: none; }")


def blank_pixmap() -> QPixmap:
    return QPixmap()
