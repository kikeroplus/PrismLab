"""MiniMax H3 用のタグの色分け (PromptWordCrafter の highlighter.py と同じ決まり。色は暗い配色に合わせる)。

<d> ～ </d> の間 (タグ自体を含む) を青い文字にする。さらに [Japanese] の次の文字から </d> の手前までを
セリフとして背景に色を付ける。どちらも複数行にまたがるブロックに対応する。
"""

from PySide6.QtGui import QColor, QSyntaxHighlighter, QTextCharFormat

TAG_OPEN = "<d>"
TAG_CLOSE = "</d>"
SPEECH_MARK = "[Japanese]"

TAG_TEXT_COLOR = QColor("#7fb0ff")  # 暗い背景で読める青
SPEECH_BACKGROUND = QColor("#5a4520")  # 暗い背景に合う琥珀色


class TagHighlighter(QSyntaxHighlighter):
    IN_TAG = 1
    IN_SPEECH = 2

    def __init__(self, document):
        super().__init__(document)
        self.tag_format = QTextCharFormat()
        self.tag_format.setForeground(TAG_TEXT_COLOR)
        self.speech_format = QTextCharFormat()
        self.speech_format.setBackground(SPEECH_BACKGROUND)

    def highlightBlock(self, text: str):
        previous = self.previousBlockState()
        if previous == -1:
            previous = 0
        tag_open = self._span(text, TAG_OPEN, TAG_CLOSE, bool(previous & self.IN_TAG), self.tag_format,
                              skip_open=False, include_close=True)
        speech_open = self._span(text, SPEECH_MARK, TAG_CLOSE, bool(previous & self.IN_SPEECH), self.speech_format,
                                 skip_open=True, include_close=False)
        self.setCurrentBlockState((self.IN_TAG if tag_open else 0) | (self.IN_SPEECH if speech_open else 0))

    def _span(self, text: str, open_token: str, close_token: str, continuing: bool, fmt: QTextCharFormat,
              skip_open: bool, include_close: bool) -> bool:
        """open_token から close_token までに fmt を付ける。行の終わりでまだ閉じていなければ True。"""
        pos = 0 if continuing else text.find(open_token)
        still_open = False
        while pos >= 0:
            start = 0 if continuing else (pos + len(open_token) if skip_open else pos)
            close = text.find(close_token, start)
            if close == -1:
                end, still_open = len(text), True
            else:
                end = close + len(close_token) if include_close else close
            if end > start:
                self.setFormat(start, end - start, fmt)
            if close == -1:
                break
            continuing = False
            pos = text.find(open_token, close + len(close_token))
        return still_open
