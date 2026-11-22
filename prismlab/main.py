import getpass
import json
import sys
from pathlib import Path

from PySide6.QtCore import QPointF, QTimer
from PySide6.QtGui import QColor, QIcon, QLinearGradient, QPainter, QPixmap, QPolygonF
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from . import profile, theme
from .main_window import MainWindow

# 2 つ目を起動したら、先に動いているほうを前に出して自分は終わる。
# 同じ量産を 2 つのアプリが取り合わないようにするため。
# J-Rate・PromptWordCrafter も、パイプ PrismLab-(ユーザー名) に JSON を書いて送ってくる (spec 5 章)。
_SERVER_NAME = f"PrismLab-{getpass.getuser()}"
SEND_ARG = "--send"  # PrismLab.exe --send (JSON のパス): 動いていないときに、送るものを持って起動する
# PrismLab.exe --background: 画面を出さずに起動する (ほかのアプリから頼まれた生成だけを裏で動かす。spec 6 章)。
# タスクトレイに出し、頼まれたものが終わって何も動いていなければ自分で終わる
BACKGROUND_ARG = "--background"
IDLE_CHECK_MS = 5000


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


def _send_arg() -> bytes:
    """--send で渡された JSON (読めなければ空)。読んだら消す (一時フォルダに送る側が書いたもの)。"""
    args = sys.argv[1:]
    if SEND_ARG not in args:
        return b""
    i = args.index(SEND_ARG)
    if i + 1 >= len(args):
        return b""
    path = Path(args[i + 1])
    try:
        data = path.read_bytes()
    except OSError:
        return b""
    try:
        path.unlink()
    except OSError:
        pass
    return data


def _notify_running_instance(payload: bytes = b"") -> bool:
    socket = QLocalSocket()
    socket.connectToServer(_SERVER_NAME)
    if not socket.waitForConnected(500):
        return False
    if sys.platform == "win32":
        # 前面に出るのを Windows に拒まれないよう、こちらから許可しておく
        import ctypes
        ctypes.windll.user32.AllowSetForegroundWindow(-1)  # ASFW_ANY
    if payload:
        # 送るものは 1 行 (改行で終わる)。受け側が読み終えて切るまで待つ (先に切ると、読まれずに落ちることがある)
        socket.write(payload.rstrip(b"\r\n") + b"\n")
        socket.flush()
        socket.waitForBytesWritten(1000)
        socket.waitForDisconnected(3000)
    else:
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
        if conn is None:
            return
        buffer = bytearray()
        handled = []

        def finish():
            if not handled:
                handled.append(True)
                handle(window, bytes(buffer))

        def read():
            buffer.extend(bytes(conn.readAll()))
            if b"\n" in buffer:
                # 送るものは 1 行。読み終えたらこちらから切る (送る側はそれを待っている)
                finish()
                conn.disconnectFromServer()

        def gone():
            buffer.extend(bytes(conn.readAll()))
            finish()  # 改行のないもの ("show" など)
            conn.deleteLater()

        conn.readyRead.connect(read)
        conn.disconnected.connect(gone)

    server.newConnection.connect(on_connection)
    return server


def handle(window: MainWindow, data: bytes) -> None:
    """送られてきたもの: "show" (前に出すだけ)、送る JSON (spec 5 章)、頼まれた生成 (spec 6 章。前には出さない)。"""
    text = data.decode("utf-8", "replace").strip()
    if not text:
        return  # 何も送らずに切った (動いているかを確かめただけ)。前には出さない
    if text == "show":
        window.bring_to_front()
        return
    try:
        message = json.loads(text)
    except ValueError:
        return
    if not isinstance(message, dict):
        return
    cmd = message.get("cmd")
    if cmd == "show":
        window.bring_to_front()
    elif cmd == "send":
        QTimer.singleShot(0, lambda: window.receive(message))
    elif cmd == "run":
        # その場で受け付ける (後回しにすると、裏で終わろうとしているときに受け付けずに消えることがある)
        window.external.receive(message)
    elif cmd == "run_cancel":
        QTimer.singleShot(0, lambda: window.external.cancel(str(message.get("id") or "")))


def main():
    app = QApplication(sys.argv)
    payload = _send_arg()
    if _notify_running_instance(payload):
        sys.exit(0)
    app.setApplicationName("PrismLab")
    app.setWindowIcon(make_icon())
    theme.apply(app)
    merged = profile.setup()  # 設定の置き場所を決め、初めてなら共有の設定に移す
    notice = ("共有の設定: " + "。".join(merged)) if merged else ""
    background = BACKGROUND_ARG in sys.argv[1:]
    app.setQuitOnLastWindowClosed(False)  # 画面を閉じても、頼まれた生成が動いていれば裏で続ける (タスクトレイ)
    window = MainWindow(notice)
    window.background_start = background
    server = _start_server(window)
    tray = _make_tray(app, window)
    if not background:
        window.show()

    def first():
        window.external.resume()  # 前に終わらなかった頼みは聞かずに続ける (頼んだアプリが進み具合を見ている)
        if not background:
            window.offer_resume()
        if payload:
            handle(window, payload)  # 続きから始めるかを聞いたあとで入れる

    def idle():
        # 画面を出していないときは、何も動いていなければ終わる
        if not window.isVisible() and not window.busy():
            server.close()  # これからの頼みは受けない (送る側は PrismLab を起動し直す)
            window.quit_quietly()
            tray.hide()
            app.exit(0)  # quit() は窓に「閉じる」を送り、閉じなければ終わらないので exit()
            return
        tray.setVisible(not window.isVisible())
        summary = window.external.summary()
        tray.setToolTip("PrismLab" + (f" - ほかのアプリの生成: {summary}" if summary else " - 裏で動いています"))

    QTimer.singleShot(0, first)
    timer = QTimer(app)
    timer.timeout.connect(idle)
    timer.start(IDLE_CHECK_MS)
    window.closed_to_tray.connect(lambda: (tray.show(), tray.showMessage(
        "PrismLab", "ほかのアプリから頼まれた生成が動いているので、裏で続けます。終わったら PrismLab も閉じます。",
        QSystemTrayIcon.MessageIcon.Information, 5000)))
    sys.exit(app.exec())


def _make_tray(app: QApplication, window: MainWindow) -> QSystemTrayIcon:
    """裏で動いているときのタスクトレイ (PrismLab を開く・止めて終わる)。"""
    tray = QSystemTrayIcon(app.windowIcon(), app)
    tray.setToolTip("PrismLab")
    menu = QMenu()
    menu.addAction("PrismLab を開く", window.bring_to_front)

    def stop():
        for rid in list(window.external.requests):
            window.external.cancel(rid)
        window.close()
        if not window.isVisible():
            window.quit_quietly()
            app.exit(0)

    menu.addAction("ほかのアプリの生成を止めて終わる", stop)
    tray.setContextMenu(menu)
    tray._menu = menu  # 消えないように持っておく
    tray.activated.connect(lambda reason: window.bring_to_front()
                           if reason == QSystemTrayIcon.ActivationReason.DoubleClick else None)
    return tray


if __name__ == "__main__":
    main()
