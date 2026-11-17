"""乗せると拡大 (hover_preview.py) の動画: 始めのコマを出したあと、音なし・繰り返しで再生する。

    python tests\\test_hover_video.py
設定には触れない (一覧とプレビューだけを作る)。
"""

import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="prismlab_hovervideo_"))
sys.path.insert(0, str(ROOT.parent / "GenCommon"))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtCore import QEvent, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QColor, QImage, QMouseEvent  # noqa: E402
from PySide6.QtWidgets import QApplication, QListWidget, QListWidgetItem  # noqa: E402

from fakes import make_video  # noqa: E402
from prismlab.hover_preview import HoverPreview  # noqa: E402
from prismlab.thumbs import ThumbCache  # noqa: E402

app = QApplication(sys.argv)
ok = []


def check(cond, label):
    assert cond, label
    ok.append(label)
    print("OK  ", label)


def pump(seconds: float):
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        time.sleep(0.02)


clip = TMP / "clip.mp4"
make_video(clip, [(220, 30, 30)] * 8 + [(30, 50, 230)] * 8, size=(160, 96), fps=8)  # 赤 1 秒 → 青 1 秒
photo = TMP / "photo.png"
img = QImage(160, 96, QImage.Format.Format_RGB32)
img.fill(QColor(40, 160, 90))
img.save(str(photo))

view = QListWidget()
view.setViewMode(QListWidget.ViewMode.IconMode)
view.resize(400, 300)
for p in (clip, photo):
    item = QListWidgetItem(p.name)
    item.setData(Qt.ItemDataRole.UserRole, str(p))
    view.addItem(item)
small = ThumbCache(96)
hover = HoverPreview(view, lambda index: index.data(Qt.ItemDataRole.UserRole), small)
view.move(200, 600)
view.show()
view.activateWindow()
pump(0.5)
vp = view.viewport()


def move_to(row):
    center = QPointF(view.visualItemRect(view.item(row)).center())
    QApplication.sendEvent(vp, QMouseEvent(QEvent.Type.MouseMove, center, vp.mapToGlobal(center),
                                           Qt.MouseButton.NoButton, Qt.MouseButton.NoButton,
                                           Qt.KeyboardModifier.NoModifier))


def color_now():
    pix = hover.popup.image.pixmap()
    c = pix.toImage().pixelColor(pix.width() // 2, pix.height() // 2)
    return c.red(), c.blue()


check(view.isActiveWindow(), "一覧がアクティブ")
small.get(str(clip))
small.get(str(photo))
pump(1.0)
move_to(0)
pump(0.6)
check(hover.popup.isVisible() and "▶ 動画" in hover.popup.caption.text(), "名前の前に「動画」と出す")
check(hover._playing == str(clip), "動画は再生する")
check(hover._player.audioOutput() is None, "音は出さない")
seen = set()
end = time.time() + 6
while time.time() < end and len(seen) < 2:
    r, b = color_now()
    if r > 150 and b < 100:
        seen.add("赤")
    elif b > 150 and r < 100:
        seen.add("青")
    pump(0.05)
check(seen == {"赤", "青"}, f"コマが進む (見えた色: {seen})")
pix = hover.popup.image.pixmap()
check(max(pix.width(), pix.height()) > 300, f"大きく出す ({pix.size()})")

move_to(1)
pump(0.3)
check(hover.popup.isVisible() and "▶" not in hover.popup.caption.text() and not hover._playing,
      "画像へ移ったら再生を止める")
r, b = color_now()
check(r < 100 and b < 120, "画像の絵になっている (動画のコマが上書きしない)")

move_to(0)
pump(0.5)
check(hover._playing == str(clip), "動画へ戻ればまた再生")
QApplication.sendEvent(vp, QEvent(QEvent.Type.Leave))
pump(0.2)
check(not hover.popup.isVisible() and not hover._playing, "離れたら消して再生も止める")

view.close()
pump(0.2)
print(f"\nすべて OK ({len(ok)})")
shutil.rmtree(TMP, ignore_errors=True)
