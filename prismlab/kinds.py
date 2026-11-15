"""ワークフローの種類と色 (今の色分けを引き継ぐ: gencommon.set_cards.KIND_COLORS)。

種類の見分け方も set_cards と同じ: comfy.workflow_kind に、ControlNet を使う I2I は「I2I CN」。
"""

import threading
from pathlib import Path

from gencommon import comfy
from gencommon.diagram_dialog import LLM_LINE
from gencommon.set_cards import KIND_COLORS

__all__ = ["KIND_COLORS", "LLM_LINE", "TEXT_COLOR", "IMAGE_COLOR", "kind_of", "color_of", "is_video"]

TEXT_COLOR = "#9aa3b3"  # 文字 (プロンプト) の札
IMAGE_COLOR = KIND_COLORS["I2I"]  # 入口の画像の札 (画像は I2I と同じ緑)

_cache: dict = {}  # (パスの小文字, 更新日時) → 種類 ("" = 読めない)
_lock = threading.Lock()


def kind_of(workflow_path: str) -> str:
    """"T2I" / "I2I" / "I2I CN" / "T2V" / "I2V"。読めなければ ""。ファイルが変われば読み直す。"""
    if not workflow_path:
        return ""
    try:
        stamp = Path(workflow_path).stat().st_mtime
    except OSError:
        return ""
    key = (workflow_path.lower(), stamp)
    with _lock:
        if key in _cache:
            return _cache[key]
    try:
        wf = comfy.load_workflow(workflow_path)
        kind = comfy.workflow_kind(wf)
        if kind == "I2I" and comfy.uses_controlnet(wf):
            kind = "I2I CN"
    except (comfy.ComfyError, OSError, ValueError):
        kind = ""
    with _lock:
        _cache[key] = kind
    return kind


def color_of(kind: str) -> str:
    return KIND_COLORS.get(kind, "#777777")


def is_video(kind: str) -> bool:
    return kind.endswith("V")
