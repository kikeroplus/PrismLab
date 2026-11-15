"""EXE のアイコン (assets\\icon.ico) を、アプリのアイコン (prismlab.main.make_icon) から作る。

    python tools\\make_ico.py

make_icon は大きさごとに描き分けている (小さいときは線を太めに) ので、7 つの大きさをそれぞれ ICO に入れる。
"""

import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT.parent / "GenCommon"))
sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402
from PySide6.QtCore import QBuffer, QIODevice, QSize  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from prismlab.main import make_icon  # noqa: E402

SIZES = (16, 24, 32, 48, 64, 128, 256)


def main() -> None:
    app = QApplication.instance() or QApplication(sys.argv)  # noqa: F841 - QPixmap に要る
    icon = make_icon()
    images = []
    for size in SIZES:
        pix = icon.pixmap(QSize(size, size))
        buf = QBuffer()
        buf.open(QIODevice.OpenModeFlag.WriteOnly)
        pix.save(buf, "PNG")
        images.append(Image.open(io.BytesIO(bytes(buf.data()))).convert("RGBA"))
    out = ROOT / "assets" / "icon.ico"
    out.parent.mkdir(parents=True, exist_ok=True)
    # いちばん大きいものを元に、ほかの大きさは描き分けた絵をそのまま入れる
    images[-1].save(out, format="ICO", sizes=[(s, s) for s in SIZES], append_images=images[:-1])
    print("作りました:", out, sorted(Image.open(out).ico.sizes()))


if __name__ == "__main__":
    main()
