"""ファイルの★ (System.Rating) を読む。J-Rate が書くものと同じ (★1〜5 = 1 / 25 / 50 / 75 / 99)。"""

import threading
from pathlib import Path

_local = threading.local()


def _ensure_com() -> None:
    if getattr(_local, "com", False):
        return
    try:
        import pythoncom
        pythoncom.CoInitialize()
    except Exception:  # noqa: BLE001 - COM が使えなくても★なしとして動かす
        pass
    _local.com = True


def stars(value) -> int:
    """System.Rating の値 (0〜99) を★の数 (0〜5) にする。エクスプローラーと同じ区切り。"""
    try:
        v = int(value or 0)
    except (TypeError, ValueError):
        return 0
    if v <= 0:
        return 0
    if v < 13:
        return 1
    if v < 38:
        return 2
    if v < 63:
        return 3
    if v < 88:
        return 4
    return 5


def get_stars(path: Path) -> int:
    """★の数 (0〜5)。読めなければ 0。"""
    _ensure_com()
    try:
        from win32com.propsys import propsys, pscon
        store = propsys.SHGetPropertyStoreFromParsingName(str(path))
        return stars(store.GetValue(pscon.PKEY_Rating).GetValue())
    except Exception:  # noqa: BLE001 - 読めないファイル・対応しない形式は★なし
        return 0
