"""ワークフローの種類と色 (今の色分けを引き継ぐ: gencommon.set_cards.KIND_COLORS)。

種類の見分け方も set_cards と同じ: comfy.workflow_kind に、ControlNet を使う I2I は「I2I CN」。
"""

import threading
from pathlib import Path

from gencommon import comfy
from gencommon.diagram_dialog import LLM_LINE
from gencommon.set_cards import KIND_COLORS

__all__ = ["KIND_COLORS", "LLM_LINE", "TEXT_COLOR", "IMAGE_COLOR", "kind_of", "color_of", "is_video",
           "has_reference", "has_last_frame", "needs_source", "REF_COLOR"]

TEXT_COLOR = "#9aa3b3"  # 文字 (プロンプト) の札
IMAGE_COLOR = KIND_COLORS["I2I"]  # 入口の画像の札 (画像は I2I と同じ緑)
REF_COLOR = "#ffab91"  # リファレンス (顔) の札 (肌の色に近い淡いサーモン。ほかの種類の色と紛れないもの)

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


_ref_cache: dict = {}  # (何を見るか, パスの小文字, 更新日時) → その LoadImage があるか


def _has(what: str, test, workflow_path: str) -> bool:
    if not workflow_path:
        return False
    try:
        stamp = Path(workflow_path).stat().st_mtime
    except OSError:
        return False
    key = (what, workflow_path.lower(), stamp)
    with _lock:
        if key in _ref_cache:
            return _ref_cache[key]
    try:
        found = test(comfy.load_workflow(workflow_path))
    except (comfy.ComfyError, OSError, ValueError):
        found = False
    with _lock:
        _ref_cache[key] = found
    return found


def has_reference(workflow_path: str) -> bool:
    """リファレンス画像 (主に顔) を入れる LoadImage があるか (タイトルに reference / face / 顔 など。comfy.reference_nodes)。"""
    return _has("ref", comfy.has_reference, workflow_path)


def has_last_frame(workflow_path: str) -> bool:
    """ラストフレームを入れる LoadImage があるか (タイトルに last / end / ラスト など。comfy.last_frame_nodes)。"""
    return _has("last", comfy.has_last_frame, workflow_path)


def _has_source_node(wf: dict) -> bool:
    return any(not comfy.is_reference_node(n) and not comfy.is_last_frame_node(n) for n in comfy._load_image_nodes(wf))


def needs_source(workflow_path: str) -> bool:
    """元画像を入れる LoadImage (リファレンス用・ラストフレーム用でないもの) があるか。
    ないもの (T2I にリファレンスの顔だけ入れる など) は、リファレンスを渡せば元画像なしで動く。読めなければ True。"""
    if not workflow_path:
        return True
    return _has("source", _has_source_node, workflow_path) or not Path(workflow_path).is_file()


def color_of(kind: str) -> str:
    return KIND_COLORS.get(kind, "#777777")


def is_video(kind: str) -> bool:
    return kind.endswith("V")
