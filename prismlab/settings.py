"""画面まわりの設定 (窓の位置と大きさ)。%APPDATA%\\PrismLab\\ に小さなテキストファイルで置く (ui-standard 15 章)。"""

from gencommon import store

GEOMETRY_FILE = "window_geometry.txt"


def restore_geometry(window) -> None:
    text = store.read_text(store.app_file(GEOMETRY_FILE)) or ""
    try:
        x, y, w, h, maximized = (int(float(v)) for v in text.split(","))
    except ValueError:
        window.resize(1280, 860)
        return
    window.setGeometry(x, y, max(w, 800), max(h, 600))
    screen = window.screen().availableGeometry() if window.screen() else None
    if screen is not None and not screen.intersects(window.frameGeometry()):
        window.move(screen.topLeft())
    if maximized:
        window.showMaximized()


def save_geometry(window) -> None:
    g = window.normalGeometry()
    try:
        store.write_text(store.app_file(GEOMETRY_FILE),
                         f"{g.x()},{g.y()},{g.width()},{g.height()},{1 if window.isMaximized() else 0}")
    except OSError:
        pass
