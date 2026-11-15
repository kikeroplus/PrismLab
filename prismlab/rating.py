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


PERCENT = {1: 1, 2: 25, 3: 50, 4: 75, 5: 99}  # ★ → System.Rating (J-Rate と同じ)


def set_stars(path: Path, stars: int, tries: int = 3) -> bool:
    """★を書く (0 = 消す)。更新日時は変えない。書けなければ False (形式が対応しない・使用中など)。
    ネットワーク共有で書いた直後などは書けないことがあるので、少し待って何回か試す (J-Rate と同じ)。"""
    import os
    import time
    _ensure_com()
    try:
        import pythoncom
        from win32com.propsys import propsys, pscon
    except Exception:  # noqa: BLE001
        return False
    try:
        st = os.stat(path)
    except OSError:
        return False
    for attempt in range(tries):
        try:
            store = propsys.SHGetPropertyStoreFromParsingName(str(path), None, 2, propsys.IID_IPropertyStore)
            if stars > 0:
                value = propsys.PROPVARIANTType(PERCENT[max(1, min(5, stars))], pythoncom.VT_UI4)
            else:
                value = propsys.PROPVARIANTType(None, pythoncom.VT_EMPTY)
            store.SetValue(pscon.PKEY_Rating, value)
            store.Commit()
            del store
            try:
                os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))  # 更新日時は変えない
            except OSError:
                pass
            return True
        except Exception:  # noqa: BLE001 - 対応しない形式・使用中
            if attempt + 1 < tries:
                time.sleep(0.5)
    return False


def get_stars(path: Path) -> int:
    """★の数 (0〜5)。読めなければ 0。"""
    _ensure_com()
    try:
        from win32com.propsys import propsys, pscon
        store = propsys.SHGetPropertyStoreFromParsingName(str(path))
        return stars(store.GetValue(pscon.PKEY_Rating).GetValue())
    except Exception:  # noqa: BLE001 - 読めないファイル・対応しない形式は★なし
        return 0
