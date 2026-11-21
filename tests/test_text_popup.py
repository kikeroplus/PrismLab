"""文のステップ (追加テキスト・差し込み・置き換え) の札に乗せたときの、全文と日本語訳の小窓のテスト。
偽の LLM を使い、APPDATA を一時フォルダに差し替える (実際の設定に触れない)。

    python tests\\test_text_popup.py (撮った画像の置き場所)
"""

import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="prismlab_popup_"))
OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else TMP
os.environ["APPDATA"] = str(TMP / "appdata")
os.environ["QT_QPA_PLATFORM"] = os.environ.get("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(ROOT.parent / "GenCommon"))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtCore import QEvent, QPoint, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QColor, QCursor, QEnterEvent, QImage, QMouseEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from fakes import FakeLLM  # noqa: E402
from gencommon import settings, store  # noqa: E402
from prismlab import profile, recipes, text_popup, theme  # noqa: E402
from prismlab.main_window import MainWindow  # noqa: E402

app = QApplication(sys.argv)
theme.apply(app)
profile.setup()
assert str(store.APP_DIR).startswith(str(TMP))
llm = FakeLLM()
settings.set_llm_url(llm.url)

src = TMP / "src"
src.mkdir()
img = QImage(100, 100, QImage.Format.Format_RGB32)
img.fill(QColor("#888"))
img.save(str(src / "a.png"))
lines = TMP / "lines"
lines.mkdir()
(lines / "001.txt").write_text("\ufeffshe says hello", encoding="utf-8")  # BOM 付き (これで訳が出なかった)

S = recipes.Stage
rs = recipes.default_recipes()
rs[0].folder, rs[0].min_stars = str(src), 0
rs[0].stages = [S(recipes.TEXT, text="a girl in a red dress, smiling", label="服"),
                S(recipes.INSERT, folder=str(lines)),
                S(recipes.SUB, rules="red => blue\nsmiling =>"),
                S(recipes.TEXT, text="夕方の海辺"),
                S(recipes.ENHANCE, enhancer="x")]
recipes.save(rs)
w = MainWindow()
w.resize(1600, 800)
w.show()


def pump(t):
    end = time.time() + t
    while time.time() < end:
        app.processEvents()
        time.sleep(0.02)


def hover(card, y=12):
    """札の上の端から y のところに乗せる (既定は見出しのあたり)。"""
    QCursor.setPos(card.mapToGlobal(QPoint(20, y)))
    QApplication.sendEvent(card, QEnterEvent(QPointF(20, y), QPointF(20, y), QPointF(20, y)))
    pump(1.2)


def press(card):
    left = Qt.MouseButton.LeftButton
    QApplication.sendEvent(card, QMouseEvent(QEvent.Type.MouseButtonPress, QPointF(20, 12), QPointF(20, 12),
                                             left, left, Qt.KeyboardModifier.NoModifier))


def move(card, y):
    QCursor.setPos(card.mapToGlobal(QPoint(20, y)))
    pump(1.0)


def leave(card):
    QApplication.sendEvent(card, QEvent(QEvent.Type.Leave))
    pump(0.1)


pump(1)
lv = w.line
pop = lv.text_popup.popup
cards = lv.stage_cards

bar_top = cards[0].bar.geometry().top()
# 処理バーより下 (サムネール・◀ ▶ のあたり) では出さない
hover(cards[0], bar_top + 20)
assert not pop.isVisible()
# 上へ動かすと出し、下へ戻すと消す
move(cards[0], 12)
assert pop.isVisible()
move(cards[0], bar_top + 3)
assert not pop.isVisible()
move(cards[0], bar_top - 3)
assert pop.isVisible()
leave(cards[0])
# 押すと消え、離れるまでは出さない (設定の窓が開くため)
hover(cards[0])
assert pop.isVisible()
press(cards[0])
pump(1.0)
assert not pop.isVisible()
leave(cards[0])

hover(cards[0])
assert pop.isVisible()
assert pop.original.text() == "a girl in a red dress, smiling", pop.original.text()
assert pop.japanese.text() == "訳: a girl in a red dress, smiling", pop.japanese.text()
assert "服" in pop.head.text()
pop.grab().save(str(OUT / "popup_text.png"))
leave(cards[0])
assert not pop.isVisible()

# 同じ文は、もう一度は訳さない
calls = len(llm.calls)
hover(cards[0])
assert len(llm.calls) == calls and pop.japanese.text().startswith("訳: ")
assert "前に訳した" in pop.label.text(), pop.label.text()
# 窓は札に重ねない (右か左に出す)
g, c = pop.geometry(), cards[0].rect().translated(cards[0].mapToGlobal(cards[0].rect().topLeft()))
assert not g.intersects(c), (g, c)
leave(cards[0])

hover(cards[1])  # 差し込み: 次に使う .txt の中身
assert pop.original.text() == "she says hello" and "001.txt" in pop.head.text(), pop.head.text()
assert pop.japanese.text() == "訳: she says hello", pop.japanese.text()
leave(cards[1])

hover(cards[2])  # 置き換え: 決まりの全文
assert pop.original.text() == "red => blue\nsmiling =>", pop.original.text()
leave(cards[2])

calls = len(llm.calls)
hover(cards[3])  # 日本語だけの文は訳さない
assert pop.isVisible() and pop.original.text() == "夕方の海辺" and not pop.japanese.isVisibleTo(pop)
assert len(llm.calls) == calls
leave(cards[3])

hover(cards[4])  # 盛るには出さない
assert not pop.isVisible()
leave(cards[4])

# 押すと消える (設定の窓を開くため)
hover(cards[0])
assert pop.isVisible()
lv._rebuild_flow()
pump(0.3)
assert not pop.isVisible()

assert text_popup.needs_translation("a cat") and not text_popup.needs_translation("猫 1 匹")
llm.close()
print("test_text_popup: ok")
