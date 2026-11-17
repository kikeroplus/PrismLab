"""サムネール (エクスプローラーと同じ Windows のサムネールを使う。動画もこれで取れる)。

ImageMimicArt の results.py の make_thumbnail を引き継いだもの。読み込みは裏のスレッドで行い、
出来たら loaded(パス, QImage) で知らせる。
"""

import ctypes
import os
from ctypes import wintypes
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, QTimer, Signal
from PySide6.QtGui import QImage, QImageReader, QPixmap

MOVIE_EXTS = {".mp4", ".m4v", ".webm", ".mov", ".mkv"}
SIIGBF_THUMBNAILONLY = 0x08
RETRY_MS = (1500, 3000, 6000, 12000, 24000)  # 読めなかったとき、読み直すまでの間 (出来たばかり・ネットワークで書きかけ)


def _is_video(path: Path) -> bool:
    return path.suffix.lower() in MOVIE_EXTS


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]


def _guid(text: str) -> _GUID:
    g = _GUID()
    ctypes.oledll.ole32.CLSIDFromString(text, ctypes.byref(g))
    return g


class _SIZE(ctypes.Structure):
    _fields_ = [("cx", ctypes.c_long), ("cy", ctypes.c_long)]


class _BITMAP(ctypes.Structure):
    _fields_ = [("bmType", ctypes.c_long), ("bmWidth", ctypes.c_long), ("bmHeight", ctypes.c_long),
                ("bmWidthBytes", ctypes.c_long), ("bmPlanes", wintypes.WORD),
                ("bmBitsPixel", wintypes.WORD), ("bmBits", ctypes.c_void_p)]


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long), ("biHeight", ctypes.c_long),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


_IID_IShellItemImageFactory = "{bcc18b79-ba16-442f-80c4-8a59c30c463b}"
_GetImageProto = ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, _SIZE, ctypes.c_int,
                                    ctypes.POINTER(wintypes.HBITMAP))
_ReleaseProto = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)


def init_thread_com() -> None:
    """サムネールを作るスレッドで最初に 1 回呼ぶ。"""
    try:
        ctypes.windll.ole32.CoInitializeEx(None, 0x2)  # COINIT_APARTMENTTHREADED
    except (AttributeError, OSError):
        pass


def _shell_thumbnail(path: Path, size: int) -> Optional[QImage]:
    try:
        shell32 = ctypes.windll.shell32
        gdi32 = ctypes.windll.gdi32
    except AttributeError:
        return None
    factory = ctypes.c_void_p()
    iid = _guid(_IID_IShellItemImageFactory)
    shell32.SHCreateItemFromParsingName.argtypes = [wintypes.LPCWSTR, ctypes.c_void_p,
                                                    ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p)]
    if shell32.SHCreateItemFromParsingName(str(path), None, ctypes.byref(iid), ctypes.byref(factory)) != 0:
        return None
    vtable = ctypes.cast(factory, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    hbitmap = wintypes.HBITMAP()
    try:
        get_image = _GetImageProto(vtable[3])
        try:
            # SIIGBF_RESIZETOFIT | SIIGBF_THUMBNAILONLY: サムネールが作れないときにアプリのアイコンを返させない
            # (出来たばかりのファイルで、関連付けアプリのアイコンがサムネールとして残ってしまったため。2026-11-18)
            if get_image(factory, _SIZE(size, size), SIIGBF_THUMBNAILONLY, ctypes.byref(hbitmap)) != 0:
                return None
        except OSError:
            return None
    finally:
        _ReleaseProto(vtable[2])(factory)
    if not hbitmap:
        return None
    try:
        bmp = _BITMAP()
        gdi32.GetObjectW.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p]
        if not gdi32.GetObjectW(hbitmap, ctypes.sizeof(bmp), ctypes.byref(bmp)):
            return None
        width, height = bmp.bmWidth, abs(bmp.bmHeight)
        header = _BITMAPINFOHEADER()
        header.biSize = ctypes.sizeof(header)
        header.biWidth = width
        header.biHeight = -height  # 上から下へ
        header.biPlanes = 1
        header.biBitCount = 32
        buffer = ctypes.create_string_buffer(width * height * 4)
        dc = ctypes.windll.user32.GetDC(None)
        try:
            gdi32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
                                        ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT]
            lines = gdi32.GetDIBits(dc, hbitmap, 0, height, buffer, ctypes.byref(header), 0)
        finally:
            ctypes.windll.user32.ReleaseDC(None, dc)
        if lines != height:
            return None
        raw = buffer.raw
        # アルファがすべて 0 なら不透明の画像として扱う (動画のサムネールなど)
        has_alpha = any(raw[i] for i in range(3, len(raw), 4 * 97))
        fmt = QImage.Format.Format_ARGB32_Premultiplied if has_alpha else QImage.Format.Format_RGB32
        return QImage(raw, width, height, width * 4, fmt).copy()
    finally:
        gdi32.DeleteObject.argtypes = [wintypes.HANDLE]
        gdi32.DeleteObject(hbitmap)


def make_thumbnail(path: Path, size: int) -> Optional[QImage]:
    """size × size に収まるサムネール。画像は自分で読み、読めないもの・動画は Windows に頼む。"""
    if not _is_video(path):
        reader = QImageReader(str(path))
        reader.setAutoTransform(True)
        original = reader.size()
        if original.isValid() and original.width() > 0 and original.height() > 0:
            reader.setScaledSize(original.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatio))
        image = reader.read()
        if not image.isNull():
            return image
    image = _shell_thumbnail(path, size)
    if image is not None and (image.width() > size or image.height() > size):
        image = image.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatio,
                             Qt.TransformationMode.SmoothTransformation)
    return image


def make_last_frame(path: Path, size: int) -> Optional[QImage]:
    """動画の最後のコマの、size × size に収まるサムネール (OpenCV)。
    数えたコマ数が当てにならない動画があるので、終わりの少し手前から終わりまで読み、最後に読めたものを使う
    (手前へ飛べなければ初めから読む)。ファイルは読み終えたらすぐ閉じる (ごみ箱へ送れるように)。"""
    try:
        import cv2
    except ImportError:
        return None
    cap = cv2.VideoCapture(str(path))
    try:
        if not cap.isOpened():
            return None
        count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        frame = None
        if count > 8:
            cap.set(cv2.CAP_PROP_POS_FRAMES, count - 8)
        while True:
            ok, img = cap.read()
            if not ok:
                break
            frame = img
        if frame is None and count > 8:  # 手前へ飛べなかった: 初めから読む
            cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            while True:
                ok, img = cap.read()
                if not ok:
                    break
                frame = img
    finally:
        cap.release()
    if frame is None:
        return None
    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    h, w, _ = rgb.shape
    image = QImage(rgb.data, w, h, 3 * w, QImage.Format.Format_RGB888).copy()
    return image.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)


LAST_SUFFIX = "|last"  # 最後のコマのサムネールを覚えるときのキーの印


class _Signals(QObject):
    loaded = Signal(str, QImage)


class _Job(QRunnable):
    def __init__(self, path: str, size: int, signals: _Signals, last: bool = False):
        super().__init__()
        self.path, self.size, self.signals, self.last = path, size, signals, last

    def run(self):
        init_thread_com()
        if self.last:
            image = make_last_frame(Path(self.path), self.size)
            self.signals.loaded.emit(self.path + LAST_SUFFIX, image if image is not None else QImage())
            return
        image = make_thumbnail(Path(self.path), self.size)
        self.signals.loaded.emit(self.path, image if image is not None else QImage())


class ThumbCache(QObject):
    """パス → QPixmap。まだなければ裏で作り、出来たら changed(パス) を出す。"""

    changed = Signal(str)

    def __init__(self, size: int = 160, parent=None):
        super().__init__(parent)
        self.size = size
        self._pixmaps: dict[str, QPixmap] = {}
        self._pending: set[str] = set()
        self._stamps: dict[str, Optional[tuple]] = {}  # get(check=True) で見た (更新時刻, 大きさ)
        self._stale: set[str] = set()
        self._tries: dict[str, int] = {}  # 読めなかった回数 (少し待って読み直す)
        self._resting: set[str] = set()  # 読み直すのを待っているもの (ファイルは開いていない)
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(2)
        self._signals = _Signals()
        self._signals.loaded.connect(self._on_loaded)

    def get(self, path: str, check: bool = False, last: bool = False) -> Optional[QPixmap]:
        """check=True: ファイルの更新時刻・大きさも見て、変わっていたら読み直す
        (物語の source.png のように、同じパスに書き直すファイル用。手元のファイルだけに使う)。
        last=True: 動画の最後のコマのサムネール (出来たら changed(パス + LAST_SUFFIX))。"""
        key = path.lower() + (LAST_SUFFIX if last else "")
        if check:
            try:
                st = os.stat(path)
                stamp = (st.st_mtime_ns, st.st_size)
            except OSError:
                stamp = None
            if self._stamps.get(key) != stamp:
                self._stamps[key] = stamp
                self._pixmaps.pop(key, None)
                if key in self._pending:
                    self._stale.add(key)  # 読んでいる途中のものは古いので、届いたら読み直す
        if key in self._pixmaps:
            return self._pixmaps[key]
        if key not in self._pending:
            self._pending.add(key)
            self._pool.start(_Job(path, self.size, self._signals, last))
        return None

    def busy(self, paths) -> bool:
        """そのファイルのサムネール (最後のコマも) をまだ作っているか (読んでいるあいだは消せない)。"""
        keys = {str(p).lower() for p in paths}
        keys |= {k + LAST_SUFFIX for k in keys}
        return bool(keys & (self._pending - self._resting))  # 読み直すのを待っているものは開いていない

    def forget(self, path: str) -> None:
        for key in (path.lower(), path.lower() + LAST_SUFFIX):
            self._pixmaps.pop(key, None)
            self._stamps.pop(key, None)
            if key in self._pending:
                self._stale.add(key)

    def _on_loaded(self, path: str, image: QImage):
        key = path.lower()
        self._pending.discard(key)
        if key in self._stale:  # 読んでいるあいだにファイルが変わった: 捨てて読み直してもらう
            self._stale.discard(key)
            self.changed.emit(path)
            return
        if image.isNull():
            # 読めなかった: 出来たばかり (ネットワークで書きかけなど) かもしれないので、覚えずに少し待って読み直す。
            # 何度か試してもだめなら、読めないものとして覚える
            tries = self._tries.get(key, 0)
            if tries < len(RETRY_MS):
                self._tries[key] = tries + 1
                self._pending.add(key)  # 待っているあいだは「作っている途中」(get で頼み直さない)
                self._resting.add(key)
                QTimer.singleShot(RETRY_MS[tries], lambda: self._retry(path))
                return
        self._tries.pop(key, None)
        self._pixmaps[key] = QPixmap.fromImage(image) if not image.isNull() else QPixmap()
        self.changed.emit(path)

    def _retry(self, path: str) -> None:
        key = path.lower()
        self._resting.discard(key)
        if key not in self._pending or key in self._pixmaps:
            return
        last = key.endswith(LAST_SUFFIX)
        real = path[:-len(LAST_SUFFIX)] if last else path
        self._pool.start(_Job(real, self.size, self._signals, last))
