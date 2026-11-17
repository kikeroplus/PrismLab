"""量産ラインのレシピ (入口 + 左から右へ並べたステップ)。%APPDATA%\\PrismLab\\recipes.json に置く。

中身は値で覚える (ワークフローはパス)。読み取りプリセットとエンハンサーは名前で覚える (generation-standard 2 章)。
"""

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from gencommon import reader, store

RECIPES_FILE = "recipes.json"
LAST_FILE = "last_recipe.txt"
MACHINES_FILE = "machines.txt"  # 台 (1 行に 1 つ、接続先の URL)。最大 3 台
MAX_MACHINES = 3

READ, ENHANCE, GEN = "read", "enhance", "gen"
TEXT, INSERT = "text", "insert"  # 追加テキスト (入力した文を足す)・差し込み (フォルダの .txt を 1 枚ごとに順に足す)
STAGE_TITLES = {READ: "読み取り", ENHANCE: "盛る", TEXT: "追加テキスト", INSERT: "差し込み", GEN: "生成"}
STAGE_ORDER = (READ, ENHANCE, TEXT, INSERT, GEN)  # メニューに並べる順
ADD_TYPES = (TEXT, INSERT)  # プロンプトに文を足すステップ (盛るより前なら一緒に盛り、あとならそのまま付ける)
IMAGE_EXTS = reader.IMAGE_EXTS


@dataclass
class Stage:
    type: str = GEN
    reader: str = ""  # 読み取りプリセットの名前
    reread: bool = False  # 読み取り済み (.txt がある) でも読み直す
    enhancer: str = ""  # エンハンサーの名前
    workflow: str = ""  # ワークフローの JSON のパス
    seconds: Optional[float] = None  # 長さ (None = ワークフローのまま)
    base_prompt: bool = True  # ベースプロンプトを付ける
    text: str = ""  # 追加テキストの文
    folder: str = ""  # 差し込みのフォルダ
    bypass: bool = False  # バイパス (このステップを飛ばして、前のものをそのまま次に渡す)

    def to_dict(self) -> dict:
        return dict(self.__dict__)

    @staticmethod
    def from_dict(data: dict) -> "Stage":
        s = Stage()
        if not isinstance(data, dict):
            return s
        t = data.get("type")
        s.type = t if t in STAGE_TITLES else GEN
        s.reader = str(data.get("reader") or "")
        s.reread = bool(data.get("reread", False))
        s.enhancer = str(data.get("enhancer") or "")
        s.workflow = str(data.get("workflow") or "")
        sec = data.get("seconds")
        s.seconds = float(sec) if isinstance(sec, (int, float)) and not isinstance(sec, bool) and sec > 0 else None
        s.base_prompt = bool(data.get("base_prompt", True))
        s.text = str(data.get("text") or "")
        s.folder = str(data.get("folder") or "")
        s.bypass = bool(data.get("bypass", False))
        return s


# 入口: フォルダの画像 / 1 枚の画像 (LoadImage のように) / フォルダのテキスト (1 ファイル = 1 件の T2I) /
# ほかのアプリ (J-Rate・PromptWordCrafter) から送られたもの (画像か .txt。spec 5 章)
FOLDER, SINGLE, TEXTS, PICKED = "folder", "single", "texts", "picked"
SOURCES = (FOLDER, SINGLE, TEXTS, PICKED)
TEXT_EXT = ".txt"
MAX_REPEAT = 999


@dataclass
class Recipe:
    name: str = ""
    folder: str = ""  # 入口のフォルダ
    min_stars: int = 0  # ★の絞り込み (0 = なし)
    output_dir: str = ""  # 空なら (入口のフォルダ)\(レシピ名)。1 枚のときは (画像のあるフォルダ)\(レシピ名)
    stages: list = field(default_factory=list)
    source: str = FOLDER  # 入口の種類
    image: str = ""  # 1 枚のときの画像
    repeat: int = 1  # 回数 (同じ元を何回通すか。どの入口でも)
    inherit: bool = True  # 元の★を、出来たファイルに引き継ぐ (generation-standard 6 章のオプション。既定はオン)
    picked: list = field(default_factory=list)  # 送られたファイル (画像だけか .txt だけ)。もう一度送ると置き換える
    picked_from: str = ""  # 送ったアプリ (J-Rate / PromptWordCrafter)
    picked_back: str = ""  # 送られる前の入口の種類 (「× 外す」で戻る)

    def to_dict(self) -> dict:
        return {"name": self.name, "folder": self.folder, "min_stars": self.min_stars,
                "output_dir": self.output_dir, "source": self.source, "image": self.image, "repeat": self.repeat,
                "inherit": self.inherit, "picked": list(self.picked), "picked_from": self.picked_from,
                "picked_back": self.picked_back,
                "stages": [s.to_dict() for s in self.stages]}

    @property
    def single(self) -> bool:
        return self.source == SINGLE

    @property
    def texts(self) -> bool:
        """テキストが入口 (フォルダのテキストか、送られた .txt)。"""
        return entry_is_text(self.source, self.picked)

    @property
    def is_picked(self) -> bool:
        return self.source == PICKED

    def picked_files(self) -> list[Path]:
        """送られたファイルのうち、今もあるもの (送られた順)。"""
        return [Path(p) for p in self.picked if Path(p).is_file()]

    @staticmethod
    def from_dict(data: dict) -> "Recipe":
        r = Recipe()
        if not isinstance(data, dict):
            return r
        r.name = str(data.get("name") or "")
        r.folder = str(data.get("folder") or "")
        try:
            r.min_stars = max(0, min(5, int(data.get("min_stars") or 0)))
        except (TypeError, ValueError):
            r.min_stars = 0
        r.output_dir = str(data.get("output_dir") or "")
        r.source = data.get("source") if data.get("source") in SOURCES else FOLDER
        r.inherit = bool(data.get("inherit", True))
        r.image = str(data.get("image") or "")
        picked = data.get("picked")
        r.picked = [str(p) for p in picked if p] if isinstance(picked, list) else []
        r.picked_from = str(data.get("picked_from") or "")
        r.picked_back = data.get("picked_back") if data.get("picked_back") in (FOLDER, SINGLE, TEXTS) else ""
        if r.source == PICKED and not r.picked:
            r.source = r.picked_back or FOLDER
        try:
            r.repeat = max(1, min(MAX_REPEAT, int(data.get("repeat") or 1)))
        except (TypeError, ValueError):
            r.repeat = 1
        stages = data.get("stages")
        r.stages = [Stage.from_dict(s) for s in stages] if isinstance(stages, list) else []
        return r

    def copy(self) -> "Recipe":
        return Recipe.from_dict(json.loads(json.dumps(self.to_dict())))

    def resolved_output_dir(self) -> str:
        if self.output_dir:
            return self.output_dir
        if self.is_picked:
            files = self.picked_files()
            base = str(files[0].parent) if files else ""
        else:
            base = str(Path(self.image).parent) if self.single and self.image else ("" if self.single else self.folder)
        if not base:
            return ""
        return str(Path(base) / safe_name(self.name or "PrismLab"))


def entry_is_text(source: str, picked: list) -> bool:
    """その入口がテキストか (フォルダのテキスト・送られた .txt)。"""
    if source == TEXTS:
        return True
    return source == PICKED and bool(picked) and all(str(p).lower().endswith(TEXT_EXT) for p in picked)


def line_problems(stages: list, skip: int = -1, text_entry: bool = False) -> set:
    """ラインが成り立たないところ (skip 番目もバイパスしたとして数える)。
    "nogen" = 生成のステップが 1 つも動かない。("enhance", i) = i 番目の盛るの前に、盛る元の文がない
    (読み取り・追加テキスト・差し込みのどれか。生成を通るとプロンプトは空に戻るので、直前の生成よりあとに要る)。
    ("needimage", i) = テキストの入口で、まだ画像がないのに i 番目が画像を要る (読み取り・I2I・I2V)。"""
    from . import kinds
    problems = set()
    has_gen = False
    has_text = text_entry  # テキストの入口は、初めから文がある
    has_image = not text_entry
    for i, s in enumerate(stages):
        if s.bypass or i == skip:
            continue
        if s.type == GEN:
            if not has_image and kinds.kind_of(s.workflow).startswith("I"):
                problems.add(("needimage", i))
            has_gen, has_text, has_image = True, False, True
        elif s.type == READ:
            if not has_image:
                problems.add(("needimage", i))
            has_text = True
        elif s.type in ADD_TYPES:
            has_text = True
        elif not has_text:
            problems.add(("enhance", i))
    if not has_gen:
        problems.add("nogen")
    return problems


def can_bypass(stages: list, index: int, text_entry: bool = False) -> bool:
    """index 番目をバイパスにできるか (バイパスにしても、ラインに新しい問題が出ない)。"""
    return not stages[index].bypass and \
        line_problems(stages, index, text_entry) <= line_problems(stages, text_entry=text_entry)


def bypass_reason(stages: list, index: int, text_entry: bool = False) -> str:
    """バイパスにできない理由 (できるなら "")。"""
    if can_bypass(stages, index, text_entry) or stages[index].bypass:
        return ""
    new = line_problems(stages, index, text_entry) - line_problems(stages, text_entry=text_entry)
    if "nogen" in new:
        return "生成のステップがなくなるので、バイパスにできません。"
    if any(isinstance(p, tuple) and p[0] == "needimage" for p in new):
        return "あとのステップに要る画像がなくなるので、バイパスにできません。"
    return "あとの「盛る」の元になる文がなくなるので、バイパスにできません。"


def safe_name(name: str) -> str:
    """フォルダ名に使えない文字を _ にする。"""
    text = re.sub(r'[\\/:*?"<>|]', "_", name).strip().rstrip(".")
    return text or "PrismLab"


def default_recipes() -> list[Recipe]:
    return [
        Recipe("★から動画", min_stars=4, stages=[Stage(READ), Stage(ENHANCE), Stage(GEN)]),
        Recipe("実写クローン", stages=[Stage(GEN)]),
        Recipe("クローン → 動画", stages=[Stage(GEN), Stage(READ), Stage(ENHANCE), Stage(GEN)]),
    ]


def load() -> list[Recipe]:
    text = store.read_text(store.app_file(RECIPES_FILE))
    if text is None:
        return default_recipes()
    try:
        items = json.loads(text)
    except ValueError:
        return default_recipes()
    if not isinstance(items, list):
        return default_recipes()
    return [Recipe.from_dict(d) for d in items if isinstance(d, dict)]


def save(recipes: list[Recipe]) -> None:
    store.write_text(store.app_file(RECIPES_FILE),
                     json.dumps([r.to_dict() for r in recipes], ensure_ascii=False, indent=2))


def last_index() -> int:
    try:
        return int(store.read_text(store.app_file(LAST_FILE)) or "0")
    except ValueError:
        return 0


def set_last_index(index: int) -> None:
    store.write_text(store.app_file(LAST_FILE), str(index))


def _read_machines(name: str) -> list[str]:
    text = store.read_text(store.app_file(name)) or ""
    urls = []
    for line in text.splitlines():
        url = line.strip().rstrip("/")
        if url and url.lower() not in [u.lower() for u in urls]:
            urls.append(url)
    return urls[:MAX_MACHINES]


def machines() -> list[str]:
    """台の URL (最大 3 つ。小文字にはしない)。量産ラインと物語で共通 (空きは engine.POOL で分け合う)。"""
    return _read_machines(MACHINES_FILE)


def set_machines(urls: list[str]) -> None:
    store.write_text(store.app_file(MACHINES_FILE), "\n".join(u.strip().rstrip("/") for u in urls[:MAX_MACHINES]))


def natural_key(text: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", text)]


def entry_problem(stages: list, text_entry: bool) -> str:
    """始める前に知らせる、ラインの問題 (なければ "")。"""
    problems = line_problems(stages, text_entry=text_entry)
    if any(isinstance(p, tuple) and p[0] == "needimage" for p in problems):
        return ("テキストの入口には画像がないので、最初の生成は T2I か T2V にしてください "
                "(読み取り・I2I・I2V は、画像が出来たあとに置けます)。")
    return ""


def list_texts(folder: str) -> list[Path]:
    """フォルダの直下の .txt (名前の自然順。中身が空のものは除く)。"""
    from gencommon import inserts
    try:
        files = [p for p in Path(folder).iterdir() if p.is_file() and p.suffix.lower() == TEXT_EXT]
    except OSError:
        return []
    return [p for p in sorted(files, key=lambda p: natural_key(p.name)) if inserts.read_file(p)]


def repeat_sources(sources: list, times: int) -> list:
    """回数ぶん並べる。全部の元を 1 回ずつ → 2 回目 … の順 (途中で止めても、どの元も 1 本は出来ているように)。"""
    return [p for _ in range(max(1, times)) for p in sources]


def list_images(folder: str) -> list[Path]:
    """フォルダの直下の画像 (名前の自然順)。"""
    try:
        files = [p for p in Path(folder).iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTS]
    except OSError:
        return []
    return sorted(files, key=lambda p: natural_key(p.name))
