"""サムネール (エクスプローラーと同じ Windows のサムネールを使う。動画もこれで取れる)。

ImageMimicArt の results.py の make_thumbnail を引き継いだもの。読み込みは裏のスレッドで行い、
出来たら loaded(パス, QImage) で知らせる。
"""

import ctypes
import os
from ctypes import wintypes
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, Signal
from PySide6.QtGui import QImage, QImageReader, QPixmap

MOVIE_EXTS = {".mp4", ".m4v", ".webm", ".mov", ".mkv"}


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
            get_image(factory, _SIZE(size, size), 0, ctypes.byref(hbitmap))  # SIIGBF_RESIZETOFIT
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


class _Signals(QObject):
    loaded = Signal(str, QImage)


class _Job(QRunnable):
    def __init__(self, path: str, size: int, signals: _Signals):
        super().__init__()
        self.path, self.size, self.signals = path, size, signals

    def run(self):
        init_thread_com()
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
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(2)
        self._signals = _Signals()
        self._signals.loaded.connect(self._on_loaded)

    def get(self, path: str, check: bool = False) -> Optional[QPixmap]:
        """check=True: ファイルの更新時刻・大きさも見て、変わっていたら読み直す
        (物語の source.png のように、同じパスに書き直すファイル用。手元のファイルだけに使う)。"""
        key = path.lower()
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
            self._pool.start(_Job(path, self.size, self._signals))
        return None

    def forget(self, path: str) -> None:
        key = path.lower()
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
        self._pixmaps[key] = QPixmap.fromImage(image) if not image.isNull() else QPixmap()
        self.changed.emit(path)
