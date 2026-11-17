"""サムネール (thumbs.py): 出来たばかりで読めなかったものを、アイコンのまま覚えずに少し待って読み直す。

    python tests\\test_thumbs.py
設定には触れない (一時フォルダの画像だけを読む)。
"""

import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="prismlab_thumbs_"))
sys.path.insert(0, str(ROOT.parent / "GenCommon"))
sys.path.insert(0, str(ROOT))

from PySide6.QtGui import QColor, QImage  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from prismlab import thumbs  # noqa: E402

app = QApplication(sys.argv)
thumbs.RETRY_MS = (300, 300, 300)  # テストでは短く
ok = []


def check(cond, label):
    assert cond, label
    ok.append(label)
    print("OK  ", label)


def pump(seconds):
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        time.sleep(0.02)


cache = thumbs.ThumbCache(64)
seen = []
cache.changed.connect(seen.append)

# 1. まだ書き終わっていない (中身が途中で切れている) 画像: 覚えずに読み直し、書き終われば本物のサムネール
good = TMP / "good.png"
img = QImage(200, 120, QImage.Format.Format_RGB32)
img.fill(QColor(30, 200, 60))
img.save(str(good))
late = TMP / "late.png"
late.write_bytes(good.read_bytes()[:40])  # 書きかけ
check(cache.get(str(late)) is None, "(頼んだ)")
pump(0.4)
check(cache.get(str(late)) is None and not cache.busy([late]), "読めなかったものは覚えず、待っているあいだはファイルを開いていない")
shutil.copy2(good, late)  # 書き終わった
end = time.time() + 5
pix = None
while time.time() < end and pix is None:
    pump(0.1)
    pix = cache.get(str(late))
check(pix is not None and not pix.isNull() and pix.toImage().pixelColor(5, 5).green() > 150,
      "書き終わったら本物のサムネール (アプリのアイコンにしない)")

# 2. 何度試しても読めないものは、読めないものとして覚える (頼み直し続けない)
broken = TMP / "broken.png"
broken.write_bytes(b"not a png")
cache.get(str(broken))
end = time.time() + 5
pix = None
while time.time() < end and pix is None:
    pump(0.1)
    pix = cache.get(str(broken))
check(pix is not None and pix.isNull(), "何度か試して読めなければ、読めないものとして覚える")

# 3. Windows のサムネールは、作れなければアイコンを返さず None
check(thumbs._shell_thumbnail(broken, 64) is None, "Windows に頼んでも、アイコンは返さない")

shutil.rmtree(TMP, ignore_errors=True)
print(f"\nすべて OK ({len(ok)})")
