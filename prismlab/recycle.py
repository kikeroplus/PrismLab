"""ごみ箱へ送る (自作アプリの標準 ui-standard 7 章)。ImageMimicArt の results.py と同じ処理。

- ネットワーク上のファイルはごみ箱に入らずに消える (can_recycle で見分けて、確かめてから消す)。
- 使用中で消えないときは 0.2 秒おきに送り直し (最大 3 秒)、それでも残ったら使っているアプリを添えて説明を返す。
"""

import ctypes
import os
import time
from ctypes import wintypes
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QCoreApplication, QEventLoop


def can_recycle(path: Path) -> bool:
    """ごみ箱に入るか (ネットワーク上のファイルは入らずに消える)。"""
    text = str(path)
    if text.startswith("\\\\"):
        return False
    drive = os.path.splitdrive(os.path.abspath(text))[0] + "\\"
    try:
        return ctypes.windll.kernel32.GetDriveTypeW(drive) != 4  # DRIVE_REMOTE
    except (AttributeError, OSError):
        return True


def captions(paths: list[Path]) -> list[Path]:
    """一緒に消す説明の .txt (同じ名前の別のファイルが残るときは残す。ui-standard 8 章)。"""
    out = []
    for p in paths:
        txt = p.with_suffix(".txt")
        if not txt.is_file() or txt in out:
            continue
        try:
            others = [q for q in p.parent.glob(p.stem + ".*") if q != txt and q not in paths]
        except OSError:
            continue
        if not others:
            out.append(txt)
    return out


class _SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("wFunc", wintypes.UINT),
        ("pFrom", wintypes.LPCWSTR),
        ("pTo", wintypes.LPCWSTR),
        ("fFlags", ctypes.c_uint16),
        ("fAnyOperationsAborted", wintypes.BOOL),
        ("hNameMappings", ctypes.c_void_p),
        ("lpszProgressTitle", wintypes.LPCWSTR),
    ]


class _RM_UNIQUE_PROCESS(ctypes.Structure):
    _fields_ = [("dwProcessId", wintypes.DWORD), ("ProcessStartTime", wintypes.FILETIME)]


class _RM_PROCESS_INFO(ctypes.Structure):
    _fields_ = [("Process", _RM_UNIQUE_PROCESS), ("strAppName", ctypes.c_wchar * 256),
                ("strServiceShortName", ctypes.c_wchar * 64), ("ApplicationType", ctypes.c_int),
                ("AppStatus", wintypes.ULONG), ("TSSessionId", wintypes.DWORD), ("bRestartable", wintypes.BOOL)]


def lockers(paths: list[Path]) -> list[str]:
    """そのファイルを開いているアプリの名前 (Windows の Restart Manager で調べる。分からなければ空)。"""
    names: list[str] = []
    try:
        rm = ctypes.WinDLL("rstrtmgr")
        session = wintypes.DWORD()
        if rm.RmStartSession(ctypes.byref(session), 0, ctypes.create_unicode_buffer(64)) != 0:
            return names
        try:
            files = (wintypes.LPCWSTR * len(paths))(*[str(p) for p in paths])
            if rm.RmRegisterResources(session, len(paths), files, 0, None, 0, None) != 0:
                return names
            needed, count, reasons = wintypes.UINT(), wintypes.UINT(0), wintypes.DWORD()
            if rm.RmGetList(session, ctypes.byref(needed), ctypes.byref(count), None, ctypes.byref(reasons)) != 234:
                return names  # ERROR_MORE_DATA でなければ、使っているものはない
            info = (_RM_PROCESS_INFO * needed.value)()
            count.value = needed.value
            if rm.RmGetList(session, ctypes.byref(needed), ctypes.byref(count), info, ctypes.byref(reasons)) != 0:
                return names
            for i in range(count.value):
                name = "PrismLab (このアプリ)" if info[i].Process.dwProcessId == os.getpid() else info[i].strAppName
                if name and name not in names:
                    names.append(name)
        finally:
            rm.RmEndSession(session)
    except (OSError, AttributeError):
        pass
    return names


def busy_message(left: list[Path]) -> str:
    """送り直しても消えなかったときの説明 (使っているアプリが分かれば出す)。"""
    what = left[0].name if len(left) == 1 else f"{len(left)} 個のファイル"
    apps = lockers(left)
    hint = (f"使用中です。使っているアプリ: {'、'.join(apps)}" if apps
            else "使用中か、消す権限がありません。ほかのアプリや PC で開いていないか確かめてください")
    return f"削除できません: {what} ({hint})"


def send_to_recycle_bin(paths: list[Path], timeout: float = 3.0) -> Optional[str]:
    """ごみ箱へ送る (ネットワーク上のものはその場で消える)。失敗したときは説明を返す (うまくいけば None)。

    動画はサムネールを作った直後などに、Windows がファイルを少しのあいだ開いていて失敗することがある
    (ネットワーク上で 0.5 秒ほど)。残ったものは 0.2 秒おきに送り直す (最大 timeout 秒。待つ間も画面は描く)。"""
    left = [p for p in paths if p.exists()]
    end = time.monotonic() + timeout
    error = None
    while left:
        error = _send_once(left)
        left = [p for p in left if p.exists()]
        if not left or error == "削除が中断されました" or time.monotonic() >= end:
            break
        wait_end = time.monotonic() + 0.2
        while time.monotonic() < wait_end:
            QCoreApplication.processEvents(QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents, 50)
            time.sleep(0.02)
    if left and error != "削除が中断されました":
        return busy_message(left)
    return None if not left else error


def _send_once(paths: list[Path]) -> Optional[str]:
    """1 回送る。再試行の画面は出さない (送り直しは send_to_recycle_bin がする)。"""
    if not paths:
        return None
    op = _SHFILEOPSTRUCTW()
    op.wFunc = 3  # FO_DELETE
    op.pFrom = "".join(str(p) + "\0" for p in paths) + "\0"
    # FOF_SILENT | FOF_NOCONFIRMATION | FOF_ALLOWUNDO | FOF_NOERRORUI
    op.fFlags = 0x0004 | 0x0010 | 0x0040 | 0x0400
    result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    if result != 0:
        return f"削除できませんでした (コード 0x{result:X})"
    if op.fAnyOperationsAborted:
        return "削除が中断されました"
    return None
