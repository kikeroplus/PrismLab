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
MACHINES_OFF_FILE = "machines_off.txt"  # 休みの台 (1 行に 1 つ)。台の設定には残したまま、一時的に使わない
MAX_MACHINES = 3

READ, ENHANCE, GEN = "read", "enhance", "gen"
TEXT, INSERT = "text", "insert"  # 追加テキスト (入力した文を足す)・差し込み (フォルダの .txt を 1 枚ごとに順に足す)
# 覚える・思い出す (2026-11-18 利用者の希望): その時点のプロンプト (元の文・盛った文・足した文) を名前のメモに覚え、
# あとのステップで戻す。生成を通るとプロンプトは空に戻るので、T2I に送った文を次の I2V でも使う、などのため
SAVE, LOAD = "memo_save", "memo_load"
# 2026-11-18 利用者の希望 (提案した 5 つを全部): 最後のコマ (動画 → 最後のコマの画像)・増やす (そこから先を N 通りに)・
# ★を待つ (J-Rate で★を付けたものだけ先へ)・置き換え (プロンプトの語を置き換える・消す)。
# ワイルドカード ({a|b|c}) はステップではなく、追加テキストの書き方
FRAME, SPLIT, GATE, SUB = "last_frame", "split", "star_gate", "substitute"
# 英語に (2026-11-18 利用者の希望): いまのプロンプトを LLM で英語に訳す (サーバー・モデルはエンハンサーと同じ)
TRANSLATE = "to_english"
STAGE_TITLES = {READ: "読み取り", ENHANCE: "盛る", TEXT: "追加テキスト", INSERT: "差し込み", GEN: "生成",
                SAVE: "覚える", LOAD: "思い出す", FRAME: "最後のコマ", SPLIT: "増やす", GATE: "★を待つ", SUB: "置き換え",
                TRANSLATE: "英語に"}
STAGE_ORDER = (READ, ENHANCE, TRANSLATE, TEXT, INSERT, SUB, SAVE, LOAD, SPLIT, GATE, FRAME, GEN)  # メニューに並べる順
MAX_SPLIT = 20  # 増やすの数の上限
MEMO_TYPES = (SAVE, LOAD)
DEFAULT_MEMO = "メモ 1"
REPLACE, APPEND = "replace", "append"  # 思い出すとき: 今の文を置き換える / 今の文の後ろに足す
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
    memo: str = ""  # 覚える・思い出すのメモの名前
    mode: str = REPLACE  # 思い出すとき: 置き換える / 後ろに足す
    bypass: bool = False  # バイパス (このステップを飛ばして、前のものをそのまま次に渡す)
    # 一緒にバイパスにしたまとまりの印 (2026-11-19 利用者の希望)。読み取りを外すと元の文がなくなる盛るも一緒に入り、
    # どれかを外すとまとまりの全部が戻る。空 = 1 つだけで入れた
    bypass_group: str = ""
    count: int = 2  # 増やす: いくつに分けるか
    stars: int = 3  # ★を待つ: いくつ以上で先へ進めるか
    rules: str = ""  # 置き換え: 1 行に「元 => 先」(先がなければ消す)

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
        s.memo = str(data.get("memo") or "")
        s.mode = APPEND if data.get("mode") == APPEND else REPLACE
        s.bypass = bool(data.get("bypass", False))
        s.bypass_group = str(data.get("bypass_group") or "") if s.bypass else ""
        s.count = _int_in(data.get("count"), 2, 2, MAX_SPLIT)
        s.stars = _int_in(data.get("stars"), 3, 1, 5)
        s.rules = str(data.get("rules") or "")
        return s


def _int_in(value, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return default


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


def line_problems(stages: list, skip=(), text_entry: bool = False) -> set:
    """ラインが成り立たないところ (skip の番号 (1 つか、まとめて) もバイパスしたとして数える)。
    "nogen" = 生成のステップが 1 つも動かない。("enhance", i) = i 番目の盛るの前に、盛る元の文がない
    (読み取り・追加テキスト・差し込みのどれか。生成を通るとプロンプトは空に戻るので、直前の生成よりあとに要る)。
    ("needimage", i) = テキストの入口で、まだ画像がないのに i 番目が画像を要る (読み取り・I2I・I2V)。
    ("memoempty", i) = i 番目の覚えるの時点で、覚える文がない。("norecall", i) = i 番目の思い出すより前に、
    その名前のメモを覚えるステップがない。("needvideo", i) = i 番目の最後のコマの前に、動画の生成がない。
    ("notext", i) = i 番目の置き換えの時点で、置き換える文がない。"""
    from . import kinds
    problems = set()
    has_gen = False
    has_text = text_entry  # テキストの入口は、初めから文がある
    has_image = not text_entry
    video = False  # いま流れているのが動画か (ワークフローの種類が分からなければ None = 確かめない)
    memos = set()  # ここまでに覚えたメモの名前 (生成を通っても残る)
    skip = {skip} if isinstance(skip, int) else set(skip)
    for i, s in enumerate(stages):
        if s.bypass or i in skip:
            continue
        if s.type == GEN:
            kind = kinds.kind_of(s.workflow)
            if not has_image and kind.startswith("I"):
                problems.add(("needimage", i))
            has_gen, has_text, has_image = True, False, True
            video = kinds.is_video(kind) if kind else None
        elif s.type == FRAME:
            if video is False:
                problems.add(("needvideo", i))
            video = False
        elif s.type == GATE:
            if not has_image:
                problems.add(("needimage", i))  # テキストには★がない
        elif s.type == SPLIT:
            pass
        elif s.type in (SUB, TRANSLATE):
            if not has_text:
                problems.add(("notext", i))
        elif s.type == READ:
            if not has_image:
                problems.add(("needimage", i))
            has_text = True
        elif s.type in ADD_TYPES:
            has_text = True
        elif s.type == SAVE:
            if not has_text:
                problems.add(("memoempty", i))
            memos.add(s.memo or DEFAULT_MEMO)
        elif s.type == LOAD:
            if (s.memo or DEFAULT_MEMO) not in memos:
                problems.add(("norecall", i))
            else:
                has_text = True
        elif not has_text:
            problems.add(("enhance", i))
    if not has_gen:
        problems.add("nogen")
    return problems


def bypass_set(stages: list, index: int, text_entry: bool = False) -> Optional[list]:
    """index 番目をバイパスにするとき、一緒にバイパスにするステップの番号 (index を含む、小さい順)。
    外すと動かなくなるあとのステップ (元の文がなくなる盛る・置き換え・英語に、文のない覚える、
    覚えていないメモの思い出す、動画のない最後のコマ) も一緒に入れる (2026-11-19 利用者の希望)。
    生成が 1 つもなくなる・画像の要るステップの画像がなくなる (テキストの入口) なら入れられない (None)。"""
    if not 0 <= index < len(stages) or stages[index].bypass:
        return None
    base = line_problems(stages, text_entry=text_entry)
    group = {index}
    while True:
        new = line_problems(stages, group, text_entry) - base
        if not new:
            return sorted(group)
        if "nogen" in new or any(p[0] == "needimage" for p in new if isinstance(p, tuple)):
            return None
        more = {p[1] for p in new if isinstance(p, tuple)} - group
        if not more:
            return None
        group |= more


def can_bypass(stages: list, index: int, text_entry: bool = False) -> bool:
    """index 番目をバイパスにできるか (困るあとのステップも一緒に入れて、ラインが成り立つ)。"""
    return bypass_set(stages, index, text_entry) is not None


def bypass_reason(stages: list, index: int, text_entry: bool = False) -> str:
    """バイパスにできない理由 (できるなら "")。"""
    if stages[index].bypass or can_bypass(stages, index, text_entry):
        return ""
    group = {index}  # 一緒に入れられるものを入れたうえで、残る問題を言う
    base = line_problems(stages, text_entry=text_entry)
    while True:
        new = line_problems(stages, group, text_entry) - base
        more = {p[1] for p in new if isinstance(p, tuple) and p[0] != "needimage"} - group
        if not more:
            break
        group |= more
    if "nogen" in new:
        return "生成のステップがなくなるので、バイパスにできません。"
    return "あとのステップに要る画像がなくなる (テキストの入口で、最初の T2I / T2V を外す) ので、バイパスにできません。"


def bypass_members(stages: list, index: int) -> list:
    """index 番目のバイパスを外すとき、一緒に戻すステップの番号 (同じまとまり。まとまりがなければ index だけ)。"""
    group = stages[index].bypass_group
    if not group:
        return [index]
    return [i for i, s in enumerate(stages) if s.bypass and s.bypass_group == group]


def new_bypass_group(stages: list, taken=()) -> str:
    """まとまりの印 (レシピの中と taken に重ならないもの)。"""
    used = {s.bypass_group for s in stages} | set(taken)
    n = 1
    while f"g{n}" in used:
        n += 1
    return f"g{n}"


def renumber_bypass_groups(pasted: list, stages: list) -> None:
    """貼り付けたステップのまとまりの印を、貼り付け先と重ならないものに付け直す (貼り付けたものの中では保つ)。"""
    mapping = {}
    for s in pasted:
        if s.bypass_group:
            if s.bypass_group not in mapping:
                mapping[s.bypass_group] = new_bypass_group(stages, mapping.values())
            s.bypass_group = mapping[s.bypass_group]


# ----------------------------------------------------------------------
# ワイルドカード ({a|b|c} を 1 件ごとに 1 つ選ぶ) と置き換え
# ----------------------------------------------------------------------
_WILD = re.compile(r"\{([^{}]*\|[^{}]*)\}")


def expand_wildcards(text: str, rng=None) -> str:
    """{a|b|c} を 1 つ選んだものにする (入れ子は内側から)。| のない {…} はそのまま。"""
    import random
    rng = rng or random
    for _ in range(100):  # 入れ子の深さ (念のため上限)
        new = _WILD.sub(lambda m: rng.choice(m.group(1).split("|")), text)
        if new == text:
            break
        text = new
    return text


def has_wildcards(text: str) -> bool:
    return bool(_WILD.search(text or ""))


RULE_SEPS = ("=>", "→")


def parse_rules(text: str) -> list[tuple[str, str]]:
    """置き換えの決まり: 1 行に「元 => 先」(「→」も可)。先がない行・区切りのない行は、元を消す。空行と # の行は飛ばす。"""
    rules = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        old, new = line, ""
        for sep in RULE_SEPS:
            if sep in line:
                old, new = (part.strip() for part in line.split(sep, 1))
                break
        if old:
            rules.append((old, new))
    return rules


def apply_rules(text: Optional[str], rules: list[tuple[str, str]]) -> Optional[str]:
    """置き換える (大文字・小文字は区別しない)。消したあとに残る「, ,」や続いた空白は詰める。"""
    if text is None:
        return None
    for old, new in rules:
        text = re.sub(re.escape(old), lambda _m, n=new: n, text, flags=re.IGNORECASE)
    text = re.sub(r"[ \t]*,(?:[ \t]*,)+", ",", text)  # , , → ,
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"^[ \t]*,[ \t]*|[ \t]*,[ \t]*$", "", text, flags=re.MULTILINE)  # 行の頭と終わりの ,
    return text.strip()


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


def all_machines() -> list[str]:
    """台の設定で選んである台の URL (休みの台も入れる。最大 3 つ。小文字にはしない)。"""
    return _read_machines(MACHINES_FILE)


def machines_off() -> set[str]:
    """一時的に使わない (休み) 台の URL の小文字。台の設定はそのままで、チェックを外した台 (2026-11-18 利用者の希望)。"""
    return {u.lower() for u in _read_machines(MACHINES_OFF_FILE)}


def machines() -> list[str]:
    """使う台の URL (休みの台を除く)。量産ラインと物語で共通 (空きは engine.POOL で分け合う)。"""
    off = machines_off()
    return [u for u in all_machines() if u.lower() not in off]


def set_machines(urls: list[str], off: Optional[set] = None) -> None:
    """台を保存する。off を渡せば休みの台も (渡さなければ、残った台の休みはそのまま)。"""
    urls = [u.strip().rstrip("/") for u in urls[:MAX_MACHINES]]
    store.write_text(store.app_file(MACHINES_FILE), "\n".join(urls))
    off = machines_off() if off is None else {u.lower() for u in off}
    store.write_text(store.app_file(MACHINES_OFF_FILE), "\n".join(u for u in urls if u.lower() in off))


def set_machine_on(url: str, on: bool) -> None:
    """台を使う / 休みにする (台の設定はそのまま)。"""
    off = machines_off()
    if on:
        off.discard(url.lower())
    else:
        off.add(url.lower())
    set_machines(all_machines(), off)


def no_machines_reason() -> str:
    """使う台がないときの知らせ。"""
    if all_machines():
        return "使う台がありません (どの台も休みです。台の札の「使う」にチェックを入れてください)。"
    return "使う台が決まっていません (「台の設定...」で選んでください)。"


def natural_key(text: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", text)]


def entry_problem(stages: list, text_entry: bool) -> str:
    """始める前に知らせる、ラインの問題 (なければ "")。"""
    problems = line_problems(stages, text_entry=text_entry)
    if any(isinstance(p, tuple) and p[0] == "needimage" for p in problems):
        return ("テキストの入口には画像がないので、最初の生成は T2I か T2V にしてください "
                "(読み取り・I2I・I2V は、画像が出来たあとに置けます)。")
    missing = sorted({stages[p[1]].memo or DEFAULT_MEMO for p in problems if isinstance(p, tuple) and p[0] == "norecall"})
    if missing:
        return (f"「思い出す」のメモ ({' / '.join(missing)}) を、その前の「覚える」で覚えていません "
                "(同じ名前の「覚える」を前に置いてください)。")
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
