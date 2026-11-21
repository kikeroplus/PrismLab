"""文のステップ (追加テキスト・差し込み・置き換え) の札に乗せると、全文と日本語訳を小窓で出す (2026-11-21 利用者の希望)。

訳は LLM (接続先・モデルは「盛る」と同じ。translate.to_japanese)。見るだけで、ステップの文は変えない。
同じ文の訳は覚えておき (アプリを閉じるまで)、もう一度は訳さない。日本語だけの文は訳さない。
乗せて少し待つと出し、離れる・押すと消す。窓はフォーカスを取らず、マウスも素通りさせる。
窓は札の横に出す (札に重ねると、Windows ではカーソルが窓に乗って札から離れたことになり、消えては出るを繰り返すため)。
札の全体ではなく、上の端から処理バーの上まで (見出し・入出力・文のところ) に乗せたときだけ出す (2026-11-21 利用者の希望)。
乗せているあいだは、カーソルがその範囲にあるかを短い間隔で見る (札の中の部品にもマウスの動きを取らせずに済む)。
"""

import re
import threading
from typing import Callable

from PySide6.QtCore import QEvent, QObject, QPoint, Qt, QTimer, Signal
from PySide6.QtGui import QCursor, QGuiApplication
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from . import theme, translate

DELAY_MS = 450
POLL_MS = 80  # 乗せているあいだ、カーソルの場所を見る間隔
GAP = 6  # 札と窓のあいだ
WIDTH = 380
MAX_CHARS = 1500  # 長すぎる文は、出すのも訳すのもここまで
MAX_KEEP = 100  # 覚えておく訳の数

_done: dict = {}  # 文 → 訳 (アプリを閉じるまで)


def needs_translation(text: str) -> bool:
    """訳すか: 英字が入っていれば訳す (日本語だけの文は訳さない)。"""
    return bool(re.search(r"[A-Za-z]{2,}", text))


class _Popup(QWidget):
    def __init__(self, parent: QWidget):
        super().__init__(parent, Qt.WindowType.ToolTip | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        self.setObjectName("textPopup")
        self.setStyleSheet(f"#textPopup {{ background: #161616; border: 1px solid {theme.ACCENT}; border-radius: 6px; }}"
                           "QLabel { background: transparent; border: none; }")
        self.setFixedWidth(WIDTH)
        box = QVBoxLayout(self)
        box.setContentsMargins(10, 8, 10, 8)
        box.setSpacing(6)
        self.head = QLabel()
        self.head.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        self.head.setWordWrap(True)
        self.head.setTextFormat(Qt.TextFormat.PlainText)
        self.original = QLabel()
        self.original.setWordWrap(True)
        self.original.setTextFormat(Qt.TextFormat.PlainText)
        self.original.setStyleSheet("font-size: 12px;")
        self.line = QLabel()
        self.line.setFixedHeight(1)
        self.line.setStyleSheet("background: #3a3a3a;")
        self.label = QLabel("日本語訳")
        self.label.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        self.japanese = QLabel()
        self.japanese.setWordWrap(True)
        self.japanese.setTextFormat(Qt.TextFormat.PlainText)
        self.japanese.setStyleSheet("font-size: 12px; color: #e8d9a8;")
        for w in (self.head, self.original, self.line, self.label, self.japanese):
            box.addWidget(w)


class TextPopup(QObject):
    """attach(札, 文を返す関数, 範囲の下の端を返す関数) した札に乗せると、全文と日本語訳を出す。
    文を返す関数は (見出し, 文) を返す (文が空なら出さない)。乗せたときに呼ぶので、そのときの中身を出す。
    下の端を返す関数は、札の中の y (それより上に乗せたときだけ出す)。なければ札の全体。"""

    # (頼みの番号, 訳, 失敗の理由)。文そのものは渡さない: Qt を通すと頭の BOM (\ufeff) が落ち、
    # 出している文と合わなくなって訳を出せなかった (2026-11-21。BOM 付きの差し込みの .txt で起きた)
    _translated = Signal(int, str, str)

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.popup = _Popup(parent)
        self._sources: dict = {}  # 札 → 文を返す関数
        self._zones: dict = {}  # 札 → 範囲の下の端 (札の中の y) を返す関数
        self._pressed = False  # 札を押した (離れるまで出さない。押すと設定の窓が開く)
        self._card = None  # 乗せている札
        self._text = ""  # 出している文
        self._busy: dict = {}  # 訳している文 → 頼みの番号
        self._asked: dict = {}  # 頼みの番号 → 文
        self._seq = 0
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(DELAY_MS)
        self._timer.timeout.connect(self._show)
        self._poll = QTimer(self)
        self._poll.setInterval(POLL_MS)
        self._poll.timeout.connect(self._check)
        self._translated.connect(self._on_translated)

    def attach(self, card: QWidget, source: Callable[[], tuple], zone: Callable[[], int] = None) -> None:
        self._sources[card] = source
        if zone is not None:
            self._zones[card] = zone
        card.installEventFilter(self)
        card.destroyed.connect(lambda _=None, c=card: self._forget(c))

    def _forget(self, card) -> None:
        self._sources.pop(card, None)
        self._zones.pop(card, None)
        if self._card is card:
            self.hide()

    def hide(self) -> None:
        """札から離れた: 見るのもやめる。"""
        self._poll.stop()
        self._card, self._pressed = None, False
        self._close()

    def _close(self) -> None:
        """窓だけ消す (札にはまだ乗っている)。"""
        self._timer.stop()
        self._text = ""
        self.popup.hide()

    def eventFilter(self, obj, event) -> bool:
        kind = event.type()
        if kind == QEvent.Type.Enter and obj in self._sources:
            if obj is not self._card:  # 同じ札への Enter (押したあとなどに来る) では、押した印を消さない
                self._card, self._pressed = obj, False
            self._poll.start()
            self._check()
        elif kind in (QEvent.Type.Leave, QEvent.Type.Hide) and obj is self._card:
            self.hide()
        elif kind in (QEvent.Type.MouseButtonPress, QEvent.Type.ContextMenu) and obj is self._card:
            self._pressed = True
            self._close()
        return False

    def in_zone(self, card: QWidget, pos: QPoint) -> bool:
        """札の中の pos が、出す範囲 (上の端から処理バーの上まで) にあるか。"""
        if not card.rect().contains(pos):
            return False
        zone = self._zones.get(card)
        return zone is None or pos.y() < zone()

    def _check(self) -> None:
        card = self._card
        if card is None or card not in self._sources or not card.isVisible():
            self.hide()
            return
        pos = card.mapFromGlobal(QCursor.pos())
        if not card.rect().contains(pos):
            self.hide()  # 離れたのに Leave が来なかった (並びを作り直したときなど)
            return
        if self._pressed or not self.in_zone(card, pos):
            self._close()
        elif not self.popup.isVisible() and not self._timer.isActive():
            self._timer.start()

    def _show(self) -> None:
        card = self._card
        source = self._sources.get(card)
        if card is None or source is None or not card.isVisible() or self._pressed:
            return
        if not self.in_zone(card, card.mapFromGlobal(QCursor.pos())):
            return
        try:
            head, text = source()
        except Exception:  # noqa: BLE001 - 読めなければ出さない
            return
        text = (text or "").replace("\ufeff", "").strip()  # BOM は文ではない
        if not text:
            return
        if len(text) > MAX_CHARS:
            text = text[:MAX_CHARS] + "…"
        self._text = text
        p = self.popup
        p.head.setText(head)
        p.head.setVisible(bool(head))
        p.original.setText(text)
        need = needs_translation(text)
        p.line.setVisible(need)
        p.label.setVisible(need)
        p.japanese.setVisible(need)
        if need:
            if text in _done:
                # 前に訳した文は LLM を呼ばずに出す (呼ばないことが分かるように書き添える)
                p.label.setText("日本語訳 (前に訳したもの)")
                p.japanese.setText(_done[text])
            else:
                p.label.setText("日本語訳")
                p.japanese.setText("訳しています…")
                self._start(text)
        p.adjustSize()
        self._place(card)
        p.show()
        p.raise_()

    def _place(self, card: QWidget) -> None:
        """札の右に出す (入らなければ左、それも入らなければ札の下か上)。札には重ねない。"""
        p = self.popup
        top_left = card.mapToGlobal(QPoint(0, 0))
        screen = QGuiApplication.screenAt(top_left) or QGuiApplication.primaryScreen()
        area = screen.availableGeometry()
        y = max(area.top(), min(top_left.y(), area.bottom() - p.height()))
        right = top_left.x() + card.width() + GAP
        left = top_left.x() - GAP - p.width()
        if right + p.width() <= area.right():
            p.move(right, y)
            return
        if left >= area.left():
            p.move(left, y)
            return
        x = max(area.left(), min(top_left.x(), area.right() - p.width()))
        below = top_left.y() + card.height() + GAP
        if below + p.height() <= area.bottom():
            p.move(x, below)
        else:
            p.move(x, max(area.top(), top_left.y() - GAP - p.height()))

    def _start(self, text: str) -> None:
        if text in self._busy:
            return
        self._seq += 1
        seq = self._seq
        self._busy[text], self._asked[seq] = seq, text
        snap = translate.snapshot()

        def run():
            try:
                result, error = translate.to_japanese(snap, text), ""
            except Exception as exc:  # noqa: BLE001 - LLM の失敗は理由を出す
                result, error = "", str(exc)
                if "接続できません" in error:
                    # サーバーが動いていないことが多い: 接続先と、見るところを出す
                    error += (f"\n接続先: {snap.get('url', '')}\n"
                              "LM Studio などの LLM のサーバーが動いているか確かめてください "
                              "(接続先は「エンハンサーの設定」で変えられます)。")
            self._translated.emit(seq, result, error)

        threading.Thread(target=run, daemon=True).start()

    def _on_translated(self, seq: int, result: str, error: str) -> None:
        text = self._asked.pop(seq, None)
        if text is None:
            return
        self._busy.pop(text, None)
        if not error:
            _done[text] = result
            while len(_done) > MAX_KEEP:
                del _done[next(iter(_done))]
        if text != self._text or not self.popup.isVisible():
            return  # もう別の札に移った (訳は覚えておく)
        self.popup.japanese.setText(result if not error else f"訳せませんでした: {error}")
        self.popup.adjustSize()
        if self._card is not None:
            self._place(self._card)
