"""PrismLab の、共通部品 (gencommon) に渡すアプリごとの決まり。

出来たファイルは「出力フォルダ\\(元の名前)_(印).(拡張子)」に保存する。印は動画なら video、
画像はワークフローの種類で t2i / i2i / cn。つないだ段では続けて付く (photo_cn_video.mp4)。
"""

from pathlib import Path

from gencommon import app, comfy

from . import kinds, theme

APP_NAME = "PrismLab"


def suffix_for(workflow_path: str, is_video: bool) -> str:
    if is_video:
        return "_video"
    return {"T2I": "_t2i", "I2I CN": "_cn"}.get(kinds.kind_of(workflow_path), "_i2i")


def _output_path(source: Path, request, is_video: bool, ext: str) -> Path:
    if not request.output_dir:
        raise comfy.ComfyError("出力フォルダが決まっていません。")
    return Path(request.output_dir) / (source.stem + suffix_for(request.workflow_path, is_video) + ext)


PROFILE = app.Profile(
    name=APP_NAME,
    reads=True,  # 元画像を ComfyUI に送る (ワークフローに Load Image があるとき) / 読み取りを使う
    source_word="元画像",
    unit="枚",
    prompt_word="説明プロンプト",
    prompt_where="段",
    column="元画像",
    output_path=_output_path,
    workflow_hint=(
        "段で読み取った説明 (盛ったときは盛ったもの) がワークフローのプロンプトに入ります "
        f"(文字欄に「{comfy.PROMPT_MARKER}」があればそこ。なければ prompt 入力、CLIPTextEncode の順)。"
        "「Load Image」ノードがあれば元画像も送ります。seed は毎回ランダムになります。"
    ),
    output_folder=False,
    colors=app.Colors(ok=theme.OK_COLOR, error=theme.ERROR_COLOR, hint=theme.HINT_COLOR,
                      paused="#ff9800", separator="#555555"),
)


def setup() -> list[str]:
    """起動したときに呼ぶ。戻り値は、共有の設定に移したときの知らせ。"""
    return app.setup(PROFILE)
