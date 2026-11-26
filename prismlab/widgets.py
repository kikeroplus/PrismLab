"""量産ラインの画面の部品: 種類の札・ステップ・台の札。色は kinds (今の色分け) から取る。"""

from pathlib import Path
from typing import Optional

from PySide6.QtCore import QEvent, QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen, QPixmap, QPolygonF
from PySide6.QtWidgets import (QCheckBox, QFrame, QHBoxLayout, QLabel, QProgressBar, QScrollArea, QSizePolicy,
                               QToolButton, QVBoxLayout, QWidget)

from . import theme

THUMB = QSize(56, 56)
CARD_WIDTH = 210
FAIL_COLOR = "#ef5350"
DOWN_COLOR = "#ef5350"
OK_DOT = "#66bb6a"
BYPASS_COLOR = "#e0a000"  # バイパスのスイッチを入れたときの色


class HScrollArea(QScrollArea):
    """横に並べる欄 (ステップの並び・物語のシーン)。マウスホイールで横に送る (2026-11-19 利用者の希望)。
    縦のスクロールバーが出ているときは Shift+ホイールで縦に送る。横に送る分がなければ普通どおり縦。"""

    def wheelEvent(self, event) -> None:
        delta = event.angleDelta()
        step = delta.y() or delta.x()
        hbar, vbar = self.horizontalScrollBar(), self.verticalScrollBar()
        shift = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        if not step or hbar.maximum() <= hbar.minimum() or (shift and vbar.maximum() > vbar.minimum()):
            if shift and step and vbar.maximum() > vbar.minimum():
                vbar.setValue(vbar.value() - round(step / 120 * vbar.singleStep() * 3))
                event.accept()
            else:
                super().wheelEvent(event)
            return
        # 1 目盛り (120) で札の 3 分の 1 ほど進める
        hbar.setValue(hbar.value() - round(step / 120 * CARD_WIDTH / 3))
        event.accept()


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
    """サムネールを 3 つまで横に並べる。clickable にすると、クリックで clicked(番号) を出す (囲みのクリックにはしない)。"""

    clicked = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.clickable = False  # 先に決める (ラベルに付けた eventFilter が作っている途中から呼ばれる)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(4)
        self.labels = []
        for _ in range(3):
            label = QLabel()
            label.setFixedSize(THUMB)
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            label.setStyleSheet("QLabel { background: #161616; border-radius: 4px; }")
            label.installEventFilter(self)
            row.addWidget(label)
            self.labels.append(label)
        row.addStretch(1)

    def eventFilter(self, obj, event) -> bool:
        if self.clickable and obj in self.labels and event.type() in (QEvent.Type.MouseButtonPress,
                                                                       QEvent.Type.MouseButtonRelease):
            if event.type() == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.LeftButton:
                self.clicked.emit(self.labels.index(obj))
            return True  # 囲みのクリック (フォルダを選ぶ) にはしない
        return super().eventFilter(obj, event)

    def set(self, pixmaps: list, failed: Optional[list] = None, selected: int = -1,
            insets: Optional[list] = None) -> None:
        """insets: 右下に小さく重ねる絵 (動画の最後のコマ)。None の所は重ねない。"""
        failed = failed or []
        insets = insets or []
        for i, label in enumerate(self.labels):
            pix = pixmaps[i] if i < len(pixmaps) else None
            inset = insets[i] if i < len(insets) else None
            if pix is not None and not pix.isNull() and inset is not None and not inset.isNull():
                pix = with_inset(pix, inset, THUMB)
            if self.clickable:
                label.setCursor(Qt.CursorShape.PointingHandCursor if pix is not None else Qt.CursorShape.ArrowCursor)
            border = f"border: 2px solid {FAIL_COLOR};" if i < len(failed) and failed[i] else ""
            if i == selected:
                border = f"border: 2px solid {theme.ACCENT};"
            label.setStyleSheet(f"QLabel {{ background: #161616; border-radius: 4px; {border} }}")
            if pix is None or pix.isNull():
                label.clear()
            else:
                label.setPixmap(pix.scaled(THUMB, Qt.AspectRatioMode.KeepAspectRatio,
                                           Qt.TransformationMode.SmoothTransformation))


class Card(QFrame):
    """ステップの囲み (入口・読み取り・盛る・生成)。クリックで clicked、右クリックで menu_requested(位置)。"""

    clicked = Signal()
    menu_requested = Signal(object)
    bypass_toggled = Signal(bool)
    move_requested = Signal(int)  # 下の ◀ ▶ を押した (-1 = 左へ、+1 = 右へ)
    file_dropped = Signal(str)  # accept_files() したときに、落とされた画像のパス

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedWidth(CARD_WIDTH)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._color = "#555555"
        self._active = False
        self._bypassed = False
        self.box = QVBoxLayout(self)
        self.box.setContentsMargins(10, 8, 10, 8)
        self.box.setSpacing(5)

        head = QHBoxLayout()
        head.setSpacing(6)
        self.title = QLabel()
        self.title.setStyleSheet("font-weight: bold; font-size: 13px; background: transparent;")
        self.chip = KindChip()
        self.bypass = QToolButton()  # バイパスのスイッチ (入れられるステップだけに出す)
        self.bypass.setText("↷")  # 絵文字の ⏭ は青い四角になるので、文字の矢印
        self.bypass.setCheckable(True)
        self.bypass.setCursor(Qt.CursorShape.PointingHandCursor)
        self.bypass.setToolTip("バイパス: このステップを飛ばして、前のものをそのまま次に渡します")
        self.bypass.setStyleSheet(
            "QToolButton { background: transparent; border: 1.5px solid #555; border-radius: 9px;"
            f" color: {theme.TEXT_DIM}; padding: 0px 6px; font-size: 13px; font-weight: bold; }}"
            "QToolButton:hover { border-color: #999; color: white; }"
            f"QToolButton:checked {{ background: {BYPASS_COLOR}; border-color: {BYPASS_COLOR}; color: black; }}")
        self.bypass.toggled.connect(self.bypass_toggled)
        self.bypass.hide()
        head.addWidget(self.title)
        head.addStretch(1)
        head.addWidget(self.bypass)
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

        # 左へ・右へ (2026-11-21 利用者の希望。右クリックの「◀ 左へ」「右へ ▶」と同じ)。札の下の端にそろえる
        moves = QHBoxLayout()
        moves.setSpacing(4)
        self.move_left, self.move_right = QToolButton(), QToolButton()
        for b, text, tip, step in ((self.move_left, "◀", "このステップを左へ", -1),
                                   (self.move_right, "▶", "このステップを右へ", 1)):
            b.setText(text)
            b.setToolTip(tip)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setFixedSize(26, 18)
            b.setStyleSheet(
                "QToolButton { background: transparent; border: 1px solid #444; border-radius: 4px;"
                f" color: {theme.TEXT_DIM}; font-size: 10px; padding: 0px; }}"
                "QToolButton:hover { border-color: #999; color: white; background: #333; }"
                "QToolButton:disabled { border-color: #333; color: #444; }")
            b.clicked.connect(lambda _=False, s=step: self.move_requested.emit(s))
        moves.addWidget(self.move_left)
        moves.addStretch(1)
        moves.addWidget(self.move_right)
        self.moves = QWidget()
        self.moves.setStyleSheet("background: transparent;")
        self.moves.setLayout(moves)
        moves.setContentsMargins(0, 0, 0, 0)
        self.moves.hide()  # ステップだけに出す (入口には出さない)
        self.box.addWidget(self.moves)
        self._restyle()

    def set_color(self, color: str, active: bool = False) -> None:
        self._color, self._active = color, active
        self._restyle()

    def accept_files(self, exts: tuple, folders: bool = False) -> None:
        """エクスプローラー・J-Rate から画像を 1 枚 (folders なら、フォルダ 1 つも) 落とせるようにする。"""
        self._exts = tuple(e.lower() for e in exts)
        self._folders = folders
        self.setAcceptDrops(True)

    def _dropped_path(self, event) -> str:
        urls = event.mimeData().urls() if event.mimeData().hasUrls() else []
        if len(urls) != 1 or not urls[0].isLocalFile():
            return ""
        path = urls[0].toLocalFile()
        if getattr(self, "_folders", False) and Path(path).is_dir():
            return path
        return path if path.lower().endswith(getattr(self, "_exts", ())) else ""

    def dragMoveEvent(self, event):
        if self._dropped_path(event):
            event.acceptProposedAction()

    def dragEnterEvent(self, event):
        if self._dropped_path(event):
            event.acceptProposedAction()

    def dropEvent(self, event):
        path = self._dropped_path(event)
        if path:
            event.acceptProposedAction()
            self.file_dropped.emit(path)

    def set_bypassed(self, on: bool) -> None:
        """バイパスの見た目 (点線の灰色の縁・薄い文字)。スイッチの状態も合わせる (信号は出さない)。"""
        self._bypassed = on
        self.bypass.blockSignals(True)
        self.bypass.setChecked(on)
        self.bypass.blockSignals(False)
        self.title.setStyleSheet("font-weight: bold; font-size: 13px; background: transparent;"
                                 + (" color: #777; text-decoration: line-through;" if on else ""))
        self._restyle()

    def _restyle(self) -> None:
        width = 3 if self._active else 2
        line = f"{width}px dashed #666" if self._bypassed else f"{width}px solid {self._color}"
        self.setStyleSheet(
            f"Card {{ background: {'#1e1e1e' if self._bypassed else '#262626'}; border: {line}; border-radius: 9px; }}"
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


ARROW_WIDTH = 46  # ステップのあいだの矢印の幅 (流れの線が見えるように、前の 22 より広く)
LANE_TOP = 30  # 流れの線の 1 段目の高さ (札の「画像 + 文字 → 動画」の行のあたり)
LANE_STEP = 13  # 段のあいだ


class InsertArrow(QToolButton):
    """ステップのあいだの矢印。流れるもの (flow.Gap) があれば、画像・動画・文字・メモ・リファレンスを色の線で描く
    (2026-11-21 利用者の希望)。マウスを乗せると「＋」を重ね、押すと挿入するステップの種類を選ぶメニューを出す。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setText("")
        self.setFixedWidth(ARROW_WIDTH)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.setToolTip("ここにステップを挿入")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet("QToolButton { background: transparent; border: none; padding: 0px; }")
        # setMenu を使うと ▼ の場所を取られて矢印が欠けるので、押したときに自分でメニューを出す
        self._menu = None
        self._gap = None
        self._hover = False
        self.clicked.connect(self._popup)

    def set_menu(self, menu) -> None:
        self._menu = menu

    def set_gap(self, gap, tip: str = "") -> None:
        """流れるもの (flow.Gap) と説明。"""
        self._gap = gap
        self.setToolTip((tip + "\n\n" if tip else "") + "押すと、ここにステップを挿入します")
        self.update()

    def _popup(self) -> None:
        if self._menu is not None:
            self._menu.popup(self.mapToGlobal(self.rect().center()))

    def enterEvent(self, event) -> None:
        self._hover = self.isEnabled()
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, event) -> None:
        from . import flow
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w = self.width()
        gap = self._gap
        if gap is None:
            # 流れが分からないとき (前の見た目): 灰色の → だけ
            p.setPen(QColor(theme.TEXT_DIM if self.isEnabled() else "#555"))
            f = p.font()
            f.setPixelSize(16)
            p.setFont(f)
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "→")
        else:
            x0, x1 = 3, w - 3
            for n, lane in enumerate(flow.LANES):
                y = LANE_TOP + n * LANE_STEP
                if lane in gap.gone:
                    _cross(p, x0 + 4, y, "#9e9e9e")  # 直前のステップで消えた
                line = gap.lines.get(lane)
                if line is None:
                    continue
                color = QColor(line.color)
                start = x0 + (11 if lane in gap.gone else 0)
                end = x1 - 11 if line.cut else x1 - 1
                # 点線・破線は「使っていない (バイパス)」に見えるので使わない (2026-11-21 利用者の指摘)。
                # メモは二重線 (文字を取っておいている)、ほかは実線
                pen = QPen(color, {"thick": 6.0, "double": 1.6}.get(line.style, 3.0))
                pen.setCapStyle(Qt.PenCapStyle.FlatCap)
                p.setPen(pen)
                tip = end - (0 if line.cut else 7)
                if line.style == "double":
                    p.drawLine(QPointF(start, y - 2.5), QPointF(tip, y - 2.5))
                    p.drawLine(QPointF(start, y + 2.5), QPointF(tip, y + 2.5))
                else:
                    p.drawLine(QPointF(start, y), QPointF(tip, y))
                if line.new:
                    p.setPen(Qt.PenStyle.NoPen)
                    p.setBrush(color)
                    p.drawEllipse(QPointF(start + 2, y), 4, 4)
                if line.cut:
                    _cross(p, x1 - 5, y, "#ef5350")  # 次には入らずに捨てる
                else:
                    p.setPen(Qt.PenStyle.NoPen)
                    p.setBrush(color)
                    head = 4.5 if line.style != "thick" else 6
                    p.drawPolygon(QPolygonF([QPointF(end - 8, y - head), QPointF(end, y), QPointF(end - 8, y + head)]))
            if gap.copies > 1:
                p.setPen(QColor(theme.TEXT_DIM))
                f = p.font()
                f.setPixelSize(10)
                f.setBold(True)
                p.setFont(f)
                p.drawText(QRectF(0, LANE_TOP - 22, w, 12), Qt.AlignmentFlag.AlignCenter, f"×{gap.copies}")
        if self._hover:
            # 挿入の印 (線の下、札の真ん中の高さ)
            cy = max(LANE_TOP + len(flow.LANES) * LANE_STEP + 12, self.height() / 2)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor("#3a3a3a"))
            p.drawEllipse(QPointF(w / 2, cy), 11, 11)
            p.setPen(QColor("white"))
            f = p.font()
            f.setPixelSize(16)
            f.setBold(True)
            p.setFont(f)
            p.drawText(QRectF(0, cy - 11, w, 22), Qt.AlignmentFlag.AlignCenter, "＋")
        p.end()


def _cross(p: QPainter, x: float, y: float, color: str) -> None:
    """消える印の ✕。"""
    pen = QPen(QColor(color), 2.2)
    p.setPen(pen)
    p.drawLine(QPointF(x - 4, y - 4), QPointF(x + 4, y + 4))
    p.drawLine(QPointF(x - 4, y + 4), QPointF(x + 4, y - 4))


def flow_legend() -> str:
    """流れの線の見方 (並びの上に出す)。"""
    from . import kinds

    def seg(color, style, label):
        return (f'<span style="color:{color}; font-weight:bold">'
                f'{"══" if style == "double" else "━━"}</span>'
                f'<span style="color:{theme.TEXT_DIM}"> {label}</span>')
    items = [seg(kinds.IMAGE_COLOR, "solid", "画像"),
             f'<span style="color:{kinds.color_of("I2V")}; font-size:15px; font-weight:900">━━</span>'
             f'<span style="color:{theme.TEXT_DIM}"> 動画 (太い線)</span>',
             seg(kinds.TEXT_COLOR, "solid", "文字"),
             seg(kinds.TEXT_COLOR, "double", "メモ (二重線)"),
             seg(kinds.REF_COLOR, "solid", "リファレンス"),
             f'<span style="color:{theme.TEXT_DIM}">● 作る・変える　'
             f'<span style="color:#ef5350">✕</span> 使わずに捨てる　✕ 使い終わって消える　'
             f'(線の色は生成の種類の色。矢印にマウスを乗せると説明)</span>']
    return "&nbsp;&nbsp;&nbsp;".join(items)


def use_label(on: bool) -> str:
    """台の「使う」チェックの文字。外しても「使う」のままだと紛らわしいので、今の状態を出す (2026-11-20 利用者の希望)。"""
    return "使う" if on else "使わない"


class MachineCard(QFrame):
    """台の札 (状態の丸・名前・出来た数・使うのチェック・いまの進み具合)。"""

    use_toggled = Signal(bool)  # 「使う」のチェックを人が変えた (休みにする / 戻す)

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
        self.use = QCheckBox("使う")
        self.use.setStyleSheet("background: transparent;")
        self.use.setToolTip("外すと、この台を一時的に使いません (台の設定には残します)。"
                            "いま動いている件は最後まで作り、新しい件は送りません。")
        self.use.clicked.connect(self.use_toggled)  # 人が押したときだけ (set_on では出さない)
        self.use.toggled.connect(lambda on: self.use.setText(use_label(on)))
        head.addWidget(self.dot)
        head.addWidget(self.name, 1)
        head.addWidget(self.done)
        head.addWidget(self.use)
        box.addLayout(head)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px; background: transparent;")
        box.addWidget(self.status)
        self.set_down(False)

    def set_on(self, on: bool, shown: bool = True) -> None:
        """「使う」のチェックを合わせる (shown=False で隠す。台が決まっていない札など)。"""
        self.use.setVisible(shown)
        self.use.setChecked(on)
        self.use.setText(use_label(on))
        self.name.setEnabled(on)  # 休みの台は名前を薄く

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


INSET_RATIO = 0.45  # 重ねる小さな絵の大きさ (枠に対して)


def with_inset(main: QPixmap, inset: QPixmap, size: QSize) -> QPixmap:
    """main を size に収めて真ん中に置き、右下に inset (最後のコマ) を小さく重ねた絵。
    小さな絵には黒い縁と白い細線を付けて、元の絵と見分けやすくする。"""
    out = QPixmap(size)
    out.fill(QColor("#161616"))
    painter = QPainter(out)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    big = main.scaled(size, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
    painter.drawPixmap((size.width() - big.width()) // 2, (size.height() - big.height()) // 2, big)
    box = QSize(max(8, int(size.width() * INSET_RATIO)), max(8, int(size.height() * INSET_RATIO)))
    small = inset.scaled(box, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
    x, y = size.width() - small.width() - 2, size.height() - small.height() - 2
    painter.fillRect(x - 2, y - 2, small.width() + 4, small.height() + 4, QColor("#000000"))
    painter.drawPixmap(x, y, small)
    painter.setPen(QColor("#e0e0e0"))
    painter.drawRect(x - 1, y - 1, small.width() + 1, small.height() + 1)
    painter.end()
    return out
