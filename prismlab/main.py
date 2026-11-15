import getpass
import sys

from PySide6.QtCore import QPointF, QTimer
from PySide6.QtGui import QColor, QIcon, QLinearGradient, QPainter, QPixmap, QPolygonF
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication

from . import profile, theme
from .main_window import MainWindow

# 2 つ目を起動したら、先に動いているほうを前に出して自分は終わる。
# 同じ量産を 2 つのアプリが取り合わないようにするため。
_SERVER_NAME = f"PrismLab-{getpass.getuser()}"


def make_icon() -> QIcon:
    """プリズム (三角) と、そこから分かれる色 (種類の色: 青・緑・紫・橙・赤紫)。"""
    icon = QIcon()
    for size in (16, 24, 32, 48, 64, 128, 256):
        pix = QPixmap(size, size)
        pix.fill(QColor(0, 0, 0, 0))
        p = QPainter(pix)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        s = size / 64
        colors = ["#3d8fe0", "#2fa86a", "#8a4fd8", "#e0862c", "#d24f8e"]
        for i, c in enumerate(colors):
            y0 = 26 + i * 6
            p.setPen(QColor(c))
            pen = p.pen()
            pen.setWidthF(max(1.0, 4.2 * s))
            p.setPen(pen)
            p.drawLine(QPointF(34 * s, 32 * s), QPointF(62 * s, y0 * s))
        tri = QPolygonF([QPointF(20 * s, 8 * s), QPointF(38 * s, 52 * s), QPointF(2 * s, 52 * s)])
        grad = QLinearGradient(QPointF(0, 8 * s), QPointF(0, 52 * s))
        grad.setColorAt(0, QColor("#f4f4f4"))
        grad.setColorAt(1, QColor("#b8b8b8"))
        p.setBrush(grad)
        p.setPen(QColor("#5a5a5a"))
        p.drawPolygon(tri)
        p.end()
        icon.addPixmap(pix)
    return icon


def _notify_running_instance() -> bool:
    socket = QLocalSocket()
    socket.connectToServer(_SERVER_NAME)
    if not socket.waitForConnected(500):
        return False
    if sys.platform == "win32":
        # 前面に出るのを Windows に拒まれないよう、こちらから許可しておく
        import ctypes
        ctypes.windll.user32.AllowSetForegroundWindow(-1)  # ASFW_ANY
    socket.write(b"show")
    socket.flush()
    socket.waitForBytesWritten(500)
    socket.disconnectFromServer()
    return True


def _start_server(window: MainWindow) -> QLocalServer:
    server = QLocalServer(window)
    if not server.listen(_SERVER_NAME):
        QLocalServer.removeServer(_SERVER_NAME)
        server.listen(_SERVER_NAME)

    def on_connection():
        conn = server.nextPendingConnection()
        if conn is not None:
            conn.disconnected.connect(conn.deleteLater)
        window.bring_to_front()

    server.newConnection.connect(on_connection)
    return server


def main():
    app = QApplication(sys.argv)
    if _notify_running_instance():
        sys.exit(0)
    app.setApplicationName("PrismLab")
    app.setWindowIcon(make_icon())
    theme.apply(app)
    merged = profile.setup()  # 設定の置き場所を決め、初めてなら共有の設定に移す
    notice = ("共有の設定: " + "。".join(merged)) if merged else ""
    window = MainWindow(notice)
    _start_server(window)
    window.show()
    QTimer.singleShot(0, window.offer_resume)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
