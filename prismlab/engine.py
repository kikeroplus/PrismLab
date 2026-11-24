"""量産ラインを動かす部分 (画面とは別)。

- 入口の 1 枚につき 1 件 (Item)。件はステップを左から順に通る。
- 読み取り・盛る (LLM) は 1 つのスレッドで、生成は台ごとに 2 つのスレッドで進める。
  どのスレッドも「次のステップが自分の種類で、待機中の件」を前から順に取る (終わった台から次の 1 件を取る)。
- 台に繋がらなくなったら、その台の件は待機に戻し、ほかの台に回す。繋がらない台は 30 秒ごとに確かめる。
- 状態が変わるたびに %APPDATA%\\PrismLab\\line_run.json に書く (続きから始めるため)。
"""

import itertools
import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import QObject, Signal

from gencommon import comfy, enhancer, inserts, llm, reader, settings, store

from . import kinds, rating, recipes, translate

RUN_FILE = "line_run.json"
TRIAL_DIR = "試作"  # 1 枚で試すときの出力 (出力フォルダの下)
SLOTS_PER_MACHINE = 2  # 1 台に同時に送る件数 (generation-standard 6 章)
FAIL_LIMIT = 3  # ある台でだけ、これだけ続けて失敗したら、その台を外す
DOWN_RECHECK = 30.0  # 繋がらない台を確かめ直す間隔 (秒)
SEEN_SECONDS = 3.0  # これより前に空きを求められていない台は、譲る先にしない (繋がらない・外した台)
# 同じワークフローを同じ台に寄せる (2026-11-18 利用者の希望。画像と動画が混じるレシピで、ComfyUI がモデルを読み直す回数を
# 減らす)。緩い重み付け: 台が空いたら、待っている生成の先頭から AFFINITY_LOOKAHEAD 件のうち、その台が最後に動かした
# ワークフローと同じものを先に取る。先頭の件は AFFINITY_MAX_SKIPS 回まで後回しにしたら、次は必ず取る (待たせすぎない)
# 「英語に」で LLM に繋がらなかったら、訳さずに先へ進める (2026-11-23 利用者の希望: LLM が落ちていても、
# LLM の要らないレシピは止めない)。繋がらなかったあと、これだけのあいだは確かめずに飛ばす (件ごとに待たないように)
LLM_DOWN_SKIP = 60.0
ENGLISH_SKIPPED = "LLM に繋がらないので、英語にせずに送りました"
AFFINITY_LOOKAHEAD = 6
AFFINITY_MAX_SKIPS = 3
_last_workflow: dict = {}  # 台の URL の小文字 → 最後に送ったワークフローのパスの小文字 (量産ラインと物語で共通)
_last_lock = threading.Lock()


def last_workflow(url: str) -> str:
    with _last_lock:
        return _last_workflow.get(url.lower(), "")


def _set_last_workflow(url: str, workflow: str) -> None:
    with _last_lock:
        _last_workflow[url.lower()] = workflow.lower()

WAITING, RUNNING, DONE, FAILED, CANCELED = "waiting", "running", "done", "failed", "canceled"
# ★を待つ: GATED = ★が付くのを待っている (J-Rate で★を付けると先へ)、SKIPPED = ★待ちを終えたときに★がなく、そこで終えた
GATED, SKIPPED = "gated", "skipped"
STATE_TEXT = {WAITING: "待機中", RUNNING: "実行中", DONE: "完了", FAILED: "失敗", CANCELED: "取り消し",
              GATED: "★待ち", SKIPPED: "★なし"}
FRAME_DIR = "コマ"  # 最後のコマのステップが書く画像の置き場 (出力フォルダの下)
UP, DOWN, OFF = "up", "down", "off"


@dataclass
class Item:
    source: str
    current: str = ""  # 次のステップに渡すファイル (生成のステップを通るたびに、出来たファイルに変わる)
    stage: int = 0  # 次にやるステップの番号
    state: str = WAITING
    text: Optional[str] = None  # 元のプロンプト (読み取った説明)。None = まだない
    origin_text: Optional[str] = None  # テキストの入口: 始めたときのテキストの中身 (記録に残す。生成を通っても消さない)
    enhanced: Optional[str] = None  # 盛ったプロンプト
    read_name: str = ""  # 今の text を読み取った読み取りプリセットの名前
    # [{"stage": ステップ, "path": パス, "time": 時刻, "from": 元のファイル, "prompt": 読み取った文, "enhanced": 盛った文}]
    outputs: list = field(default_factory=list)
    # 足した文 (追加テキスト・差し込み)。GenRequest.inserts の形 {slot: -1, name, folder, before, file, text}。
    # before = 盛る前に足したもの (このあとの盛るに渡す)。生成を通ると空に戻す
    adds: list = field(default_factory=list)
    reference: str = ""  # リファレンス画像 (主に顔)。ワークフローにリファレンス用の LoadImage があるときだけ使う
    last_frame: str = ""  # ラストフレーム。ワークフローにラストフレーム用の LoadImage があるときだけ使う
    memos: dict = field(default_factory=dict)  # 覚えたプロンプト (メモの名前 → {"text", "enhanced", "adds"})。生成を通っても残す
    plan: dict = field(default_factory=dict)  # 差し込みの割り当て (ステップの番号の文字 → 枠の形)。始めたときに決める
    error: str = ""
    error_stage: int = -1
    # 知らせ (失敗ではない)。いまは「LLM に繋がらないので英語にせずに進めた」だけ (2026-11-23 利用者の希望)
    warning: str = ""
    machine: str = ""  # 頼み済みの台の URL (prompt_id とセット)
    prompt_id: str = ""
    # 増やすで分かれた枝の番号 (0 から)。差し込みの割り当て (plan) を枝ごとに引くのに使う
    branch: int = 0
    # この件の出力フォルダ (空なら量産の出力フォルダ)。ほかのアプリから頼まれた生成で、そのアプリの決まりの場所に出すとき
    # (PromptWordCrafter・MuseFactory: テキストのフォルダの「生成結果」、J-Rate: 元画像と同じフォルダ。spec 6 章)
    out_dir: str = ""
    status: str = ""  # 進み具合の文 (保存しない)
    at: str = ""  # いま動いている台の URL (保存しない)
    skips: int = 0  # 同じワークフローを寄せるために後回しにした回数 (保存しない)

    def to_dict(self) -> dict:
        data = dict(self.__dict__)
        data.pop("status", None)
        data.pop("at", None)
        data.pop("skips", None)
        return data

    @staticmethod
    def from_dict(data: dict) -> Optional["Item"]:
        if not isinstance(data, dict) or not data.get("source"):
            return None
        it = Item(str(data["source"]))
        for key in ("current", "error", "warning", "machine", "prompt_id", "read_name", "reference", "last_frame",
                    "out_dir"):
            setattr(it, key, str(data.get(key) or ""))
        it.current = it.current or it.source
        for key in ("text", "enhanced", "origin_text"):
            value = data.get(key)
            setattr(it, key, value if isinstance(value, str) else None)
        try:
            it.branch = max(0, int(data.get("branch") or 0))
            it.stage = int(data.get("stage") or 0)
            it.error_stage = int(data.get("error_stage", -1))
        except (TypeError, ValueError):
            return None
        state = data.get("state")
        it.state = state if state in STATE_TEXT else WAITING
        if it.state == RUNNING:
            it.state = WAITING  # 閉じたとき実行中だったもの (頼み済みなら prompt_id で続きを待つ)
        adds = data.get("adds")
        it.adds = [d for d in adds if isinstance(d, dict)] if isinstance(adds, list) else []
        memos = data.get("memos")
        it.memos = {str(k): v for k, v in memos.items() if isinstance(v, dict)} if isinstance(memos, dict) else {}
        plan = data.get("plan")
        it.plan = {str(k): v for k, v in plan.items() if isinstance(v, dict)} if isinstance(plan, dict) else {}
        outs = data.get("outputs")
        it.outputs = [o for o in outs if isinstance(o, dict) and o.get("path")] if isinstance(outs, list) else []
        return it

    @property
    def finished(self) -> bool:
        return self.state in (DONE, FAILED, CANCELED, SKIPPED)


@dataclass
class Machine:
    url: str
    name: str = ""
    state: str = UP
    reason: str = ""
    done: int = 0
    fail_streak: int = 0
    last_check: float = 0.0
    checking: bool = False

    @property
    def label(self) -> str:
        return self.name or self.url


def stage_snapshot(stage: recipes.Stage) -> dict:
    """始めたときのステップの写し (途中で設定を変えても、始めたときの中身で続ける)。
    名前で覚えている読み取りプリセット・エンハンサーは、ここで中身を写す。見つからなければ ValueError。"""
    snap = {"type": stage.type}
    if stage.bypass:
        # バイパス: 中身は使わないので確かめない (見せるための名前だけ写す)
        snap["bypass"] = True
        if stage.type == recipes.READ:
            snap["reader"] = {"name": stage.reader}
        elif stage.type == recipes.ENHANCE:
            snap["enhancer"] = {"name": stage.enhancer}
        elif stage.type == recipes.TEXT:
            snap["text"], snap["front"], snap["label"] = stage.text, stage.front, stage.label
        elif stage.type == recipes.INSERT:
            snap["folder"], snap["name"] = stage.folder, Path(stage.folder).name if stage.folder else ""
            snap["front"], snap["label"] = stage.front, stage.label
        elif stage.type in recipes.MEMO_TYPES:
            snap["memo"], snap["mode"] = stage.memo or recipes.DEFAULT_MEMO, stage.mode
        elif stage.type in (recipes.FRAME, recipes.SPLIT, recipes.GATE, recipes.SUB, recipes.TRANSLATE):
            snap["count"], snap["stars"], snap["rules"] = stage.count, stage.stars, stage.rules
        elif stage.type in recipes.PICK_TYPES:
            snap["image"], snap["folder"], snap["shuffle"] = stage.image, stage.folder, stage.shuffle
        else:
            snap["workflow"] = stage.workflow
            names = [w.name for w in comfy.workflows() if stage.workflow and w.value.lower() == stage.workflow.lower()]
            snap["workflow_name"] = (names[0] if names and names[0] else Path(stage.workflow).stem)                 if stage.workflow else ""
            snap["kind"] = kinds.kind_of(stage.workflow)
            snap["seconds"] = None
        return snap
    if stage.type == recipes.READ:
        found = [p for p in reader.load_presets() if not p.is_empty and p.name == stage.reader]
        if not found:
            raise ValueError(f"読み取りプリセット「{stage.reader or '(未選択)'}」が登録にありません。")
        snap["reader"] = reader.snapshot(found[0])
        snap["reread"] = stage.reread
    elif stage.type == recipes.ENHANCE:
        found = [p for p in enhancer.load_presets() if not p.is_empty and p.name == stage.enhancer]
        if not found:
            raise ValueError(f"エンハンサー「{stage.enhancer or '(未選択)'}」が登録にありません。")
        snap["enhancer"] = enhancer.snapshot(found[0])
    elif stage.type == recipes.TEXT:
        if not stage.text.strip():
            raise ValueError("追加テキストのステップの文が空です。")
        snap["text"], snap["front"], snap["label"] = stage.text.strip(), stage.front, stage.label
    elif stage.type == recipes.INSERT:
        try:
            inserts.list_files(stage.folder)
        except inserts.InsertError as exc:
            raise ValueError(f"差し込みのステップ: {exc}") from exc
        snap["folder"], snap["name"] = stage.folder, Path(stage.folder).name
        snap["front"], snap["label"] = stage.front, stage.label
    elif stage.type in recipes.MEMO_TYPES:
        snap["memo"], snap["mode"] = stage.memo or recipes.DEFAULT_MEMO, stage.mode
    elif stage.type == recipes.FRAME:
        pass
    elif stage.type == recipes.TRANSLATE:
        # サーバー・モデルはエンハンサーと同じ (始めたときのものを使い続ける)
        snap.update(translate.snapshot())
    elif stage.type == recipes.SPLIT:
        snap["count"] = max(2, min(recipes.MAX_SPLIT, int(stage.count)))
    elif stage.type == recipes.GATE:
        snap["stars"] = max(1, min(5, int(stage.stars)))
    elif stage.type in recipes.PICK_ONE:
        title = recipes.STAGE_TITLES[stage.type]
        if not stage.image:
            raise ValueError(f"{title} のステップに画像が選ばれていません。")
        if not Path(stage.image).is_file():
            raise ValueError(f"{title} のステップの画像が見つかりません: {stage.image}")
        snap["image"] = stage.image
    elif stage.type in recipes.PICK_TYPES:
        if not recipes.list_images(stage.folder):
            raise ValueError(f"{recipes.STAGE_TITLES[stage.type]} のフォルダに画像がありません: {stage.folder or '(未選択)'}")
        snap["folder"], snap["shuffle"] = stage.folder, stage.shuffle
    elif stage.type == recipes.SUB:
        rules = recipes.parse_rules(stage.rules)
        if not rules:
            raise ValueError("置き換えのステップに、置き換える語がありません。")
        snap["rules"] = [list(r) for r in rules]
    else:
        if not stage.workflow:
            raise ValueError("生成のステップにワークフローが選ばれていません。")
        wf = comfy.load_workflow(stage.workflow)  # 読めなければ ComfyError
        kind = kinds.kind_of(stage.workflow)
        names = [p.name for p in comfy.workflows() if p.value.lower() == stage.workflow.lower() and p.name]
        snap["workflow"] = stage.workflow
        snap["workflow_name"] = names[0] if names else Path(stage.workflow).stem
        snap["kind"] = kind
        snap["seconds"] = stage.seconds if comfy.get_duration(wf) is not None else None
        snap["base"] = comfy.base_prompt_for(kinds.is_video(kind)) if stage.base_prompt else ""
    return snap


def plan_key(stage: int, branch: int) -> str:
    """差し込みの割り当ての鍵 (枝 0 は前と同じ "ステップの番号")。"""
    return str(stage) if branch == 0 else f"{stage}/{branch}"


PLANNED_TYPES = (recipes.INSERT, recipes.REF_FOLDER, recipes.IMG_FOLDER)  # 始めたときに 1 件ごとの割り当てを決めるステップ


def make_picker(snap: dict):
    """割り当てを引くもの (差し込み: フォルダの .txt、リファレンス (フォルダ): フォルダの画像)。
    引けなければ inserts.InsertError か ValueError。"""
    if snap["type"] in (recipes.REF_FOLDER, recipes.IMG_FOLDER):
        return recipes.RefPicker(str(snap.get("folder") or ""), bool(snap.get("shuffle")))
    return inserts.Picker([inserts.Slot(snap["name"], snap["folder"], True, False)])


def next_stage(stages: list, index: int) -> int:
    """index のあとで、バイパスしていない最初のステップの番号 (なければ len(stages))。"""
    i = index + 1
    while i < len(stages) and stages[i].get("bypass"):
        i += 1
    return i


class Run:
    """1 回の量産 (始めたときのレシピの写し・ステップの写し・件)。"""

    def __init__(self, recipe: dict, stages: list, output_dir: str, items: list, started: float = 0.0,
                 paused: bool = False, trial: bool = False):
        self.pickers: list = []  # 差し込みの位置 (始めたときに書く。保存しない)
        self.trial = trial  # 試作 (1 枚で試す)。出力は (出力フォルダ)\試作
        self.recipe = recipe
        self.stages = stages
        self.output_dir = output_dir
        self.items: list[Item] = items
        self.started = started or time.time()
        self.paused = paused

    @staticmethod
    def create(recipe: recipes.Recipe, sources: list[Path], trial: bool = False) -> "Run":
        stages = [stage_snapshot(s) for s in recipe.stages]
        if not stages:
            raise ValueError("ステップが 1 つもありません。")
        first = next_stage(stages, -1)
        if first >= len(stages):
            raise ValueError("バイパスしていないステップが 1 つもありません。")
        out = recipe.resolved_output_dir()
        if not out:
            raise ValueError("出力フォルダが決まっていません。")
        items = [Item(str(p), current=str(p), stage=first) for p in sources]
        if recipe.texts:
            # テキストの入口: ファイルの中身が元の文 (読み取ったものと同じ扱い)
            texts = {}
            for it in items:
                if it.source not in texts:
                    texts[it.source] = inserts.read_file(Path(it.source))
                it.text = texts[it.source] or None
                it.origin_text = it.text
        pickers = []
        branches = 1  # そこまでの「増やす」で、1 件が何本に分かれているか
        for i, snap in enumerate(stages):
            if snap.get("bypass"):
                continue
            if snap["type"] == recipes.SPLIT:
                branches *= int(snap["count"])
                continue
            if snap["type"] not in PLANNED_TYPES:
                continue
            # 差し込み・リファレンス (フォルダ): 1 枚ごとに次のファイルを割り当てる (続きから始めても変わらない)。
            # 増やすで分かれたあとなら、枝ごとに別のファイル (枝 0 は "i"、枝 k は "i/k")
            try:
                picker = make_picker(snap)
                for it in items:
                    for k in range(branches):
                        it.plan[plan_key(i, k)] = picker.next()[0]
            except (inserts.InsertError, ValueError) as exc:
                raise ValueError(str(exc)) from exc
            pickers.append(picker)
        if trial:
            out = str(Path(out) / TRIAL_DIR)
            for picker in pickers:
                picker.discard()  # 試作では位置を進めない (量産は同じファイルから始まる)
            pickers = []
        run = Run(recipe.to_dict(), stages, out, items, trial=trial)
        run.pickers = pickers
        return run

    def to_dict(self) -> dict:
        return {"recipe": self.recipe, "stages": self.stages, "output_dir": self.output_dir,
                "started": self.started, "paused": self.paused, "trial": self.trial,
                "items": [i.to_dict() for i in self.items]}

    @staticmethod
    def from_dict(data) -> Optional["Run"]:
        if not isinstance(data, dict) or not isinstance(data.get("stages"), list):
            return None
        items = [i for i in (Item.from_dict(d) for d in data.get("items") or []) if i is not None]
        try:
            started = float(data.get("started") or 0)
        except (TypeError, ValueError):
            started = 0.0
        return Run(data.get("recipe") or {}, data["stages"], str(data.get("output_dir") or ""), items,
                   started, bool(data.get("paused")), bool(data.get("trial")))

    @property
    def unfinished(self) -> int:
        return sum(1 for i in self.items if not i.finished)

    @property
    def name(self) -> str:
        return str(self.recipe.get("name") or "")


def load_run() -> Optional[Run]:
    text = store.read_text(store.app_file(RUN_FILE))
    if not text:
        return None
    try:
        return Run.from_dict(json.loads(text))
    except ValueError:
        return None


class _Client:
    """台の空きを求めるエンジン 1 つ (量産ライン・物語・ほかのアプリの頼み 1 件ごと)。"""

    def __init__(self, first: bool):
        self.first = first  # 先にする側 (物語)
        self.free = 0  # 生成を待っている件のうち、どの台にも頼んでいないもの
        self.pinned: dict[str, int] = {}  # 台の URL の小文字 → その台に頼み済みで続きを待つ件数
        self.seen: dict[str, float] = {}  # 台 → このエンジンが最後にその台の空きを見に来た時刻 (その台を使える)
        self.served = 0.0  # 最後に空きをもらった時刻 (同じ数を使っているときは、長く待っている側が先)
        self.wake: Optional[Callable[[], None]] = None


class SlotPool:
    """台の空き (1 台に同時に SLOTS_PER_MACHINE 件まで) を、PrismLab の中のエンジン全部
    (量産ライン・物語・ほかのアプリの頼み) で分け合う。

    2026-11-17 利用者の決定: 物語が先 (両方が待っているときは、空いた台を物語に渡す)。
    2026-11-23 利用者の希望: 後から頼んだ側 (例: 量産ラインが台を埋めているときの PWC) をずっと待たせない。
    台に空きが出たら、その台を使える・生成を待っている側のうち、**使っている数がいちばん少ない側**に渡す
    (同じなら、長く待っている側)。台が 2 台・量産ラインが 4 件使っているところに PWC が頼むと、次に空いた空きは PWC に行き、
    やがて 2 件ずつになる (PWC の 1 件目は、その台の待ち行列の 2 番目に入る)。追い出しはしない (動いているものは最後まで)。
    空きが出たら待っている側を起こすので、台を空けたらすぐに使う。"""

    def __init__(self, slots: int = SLOTS_PER_MACHINE):
        self.slots = slots
        self._lock = threading.Lock()
        self._used: dict[str, int] = {}  # 台の URL の小文字 → 使っている数 (全員で)
        self._held: dict[tuple, int] = {}  # (エンジン, 台) → そのエンジンが使っている数
        self._clients: dict[int, _Client] = {}
        self.peak: dict[str, int] = {}  # 台ごとの同時に使った最大の数 (テストで見る)

    # ------------------------------------------------------------------
    def _client(self, key: int, first: bool = False) -> _Client:
        c = self._clients.get(key)
        if c is None:
            c = self._clients[key] = _Client(first)
        return c

    def set_demand(self, key: int, first: bool, free: int, pinned: dict, wake=None) -> None:
        """そのエンジンで生成を待っている件数を知らせる (free: どの台にも頼んでいない件、pinned: 台 → 頼み済みの件)。"""
        with self._lock:
            c = self._client(key, first)
            c.first = first
            c.free = max(0, free)
            c.pinned = {u.lower(): n for u, n in pinned.items() if n > 0}
            if wake is not None:
                c.wake = wake

    def forget(self, key: int) -> None:
        """終わったエンジン (使っている空きがなければ外す)。"""
        with self._lock:
            if not any(n for (k, _u), n in self._held.items() if k == key):
                self._clients.pop(key, None)

    def alive(self, key: int, url: str) -> None:
        """そのエンジンはその台を使える (エンジンのスレッドが見回るたびに知らせる)。"""
        with self._lock:
            self._client(key).seen[url.lower()] = time.monotonic()

    def _held_total(self, key: int) -> int:
        return sum(n for (k, _u), n in self._held.items() if k == key)

    def _wants(self, c: _Client, url: str, now: float) -> bool:
        return (c.free > 0 or c.pinned.get(url, 0) > 0) and now - c.seen.get(url, -1e9) <= SEEN_SECONDS

    def _winner(self, url: str, now: float) -> Optional[int]:
        """その台に次の空きを渡す相手 (ロックを持って呼ぶ)。"""
        cands = [(k, c) for k, c in self._clients.items() if self._wants(c, url, now)]
        if any(c.first for _k, c in cands):
            cands = [(k, c) for k, c in cands if c.first]
        if not cands:
            return None
        return min(cands, key=lambda kc: (self._held_total(kc[0]), kc[1].served))[0]

    def take(self, key: int, url: str, pinned: bool = False) -> bool:
        """空きをもらう。もらえなければ False。
        pinned: その台に頼み済みの件 (ComfyUI の待ち行列に入っているので、空きがあれば順番は見ない)。
        そうでなければ、その台の次の番がこのエンジンのときだけ。さらに、このエンジンが使える台のうち
        使っている数がもっと少ない台でも次の番なら、そちらに譲る (2 台あるのに 1 台に 2 件たまり、もう 1 台が遊ばないように)。"""
        k = url.lower()
        now = time.monotonic()
        with self._lock:
            c = self._client(key)
            c.seen[k] = now
            used = self._used.get(k, 0)
            if used >= self.slots:
                return False
            if not pinned:
                if self._winner(k, now) != key:
                    return False
                for other, t in c.seen.items():
                    if other != k and now - t <= SEEN_SECONDS and self._used.get(other, 0) < used \
                            and self._winner(other, now) == key:
                        return False
            self._used[k] = used + 1
            self._held[(key, k)] = self._held.get((key, k), 0) + 1
            c.served = now
            self.peak[k] = max(self.peak.get(k, 0), used + 1)
            return True

    def put_back(self, key: int, url: str) -> None:
        k = url.lower()
        with self._lock:
            self._used[k] = max(0, self._used.get(k, 0) - 1)
            n = self._held.get((key, k), 0) - 1
            if n > 0:
                self._held[(key, k)] = n
            else:
                self._held.pop((key, k), None)
            wakes = [c.wake for c in self._clients.values() if c.wake is not None]
        for wake in wakes:  # 空きが出た: 待っている側をすぐに起こす (ロックの外で)
            try:
                wake()
            except RuntimeError:  # 消えたエンジン
                pass

    def used(self, url: str) -> int:
        with self._lock:
            return self._used.get(url.lower(), 0)


POOL = SlotPool()  # PrismLab の中で 1 つ (量産ライン・物語・ほかのアプリの頼みが共有する)
_keys = itertools.count(1)


def _cancel_prompts(pairs: list) -> None:
    """(台の URL, prompt_id) を ComfyUI の待ち行列から外す (繋がらなければそのまま)。"""
    for url, pid in pairs:
        try:
            comfy.cancel_prompt(url, pid)
        except Exception:  # noqa: BLE001
            pass


def _sent_body(text: Optional[str], enhanced: Optional[str], adds: list) -> str:
    """その状態で生成に送る文 (ベースプロンプトは除く。GenRequest.full_prompt と同じ組み立て)。"""
    head = enhanced if enhanced is not None else inserts.wrap(text, adds, True)
    return inserts.wrap(head, adds, False)


def needs_english(text) -> bool:
    """英語に訳す必要があるか (ASCII 以外の文字 (日本語など) が入っている)。"""
    return bool(text) and any(ord(c) > 127 and c.isalpha() for c in str(text))


def _cancellable(func: Callable[[], str], cancel: threading.Event) -> str:
    """LLM の呼び出し (最大 3 分かかる) を別のスレッドで動かし、待つ間もキャンセルを効かせる。"""
    box: dict = {}

    def work():
        try:
            box["value"] = func()
        except Exception as exc:  # noqa: BLE001 - 呼んだ側で失敗として扱う
            box["error"] = exc

    t = threading.Thread(target=work, daemon=True)
    t.start()
    while t.is_alive():
        if cancel.wait(0.3):
            raise comfy.GenerationCancelled()
    if "error" in box:
        raise box["error"]
    return box.get("value", "")


class Engine(QObject):
    """量産を動かす。changed は状態が変わったとき (どのスレッドからでも出す)、finished はすべて終わったとき。"""

    changed = Signal()
    finished = Signal()

    def __init__(self, parent=None, run_file: Optional[str] = RUN_FILE, first: bool = False,
                 pool: Optional[SlotPool] = None):
        super().__init__(parent)
        self.run_file = run_file  # 続きから始めるための書き出し先 (None = 書かない。物語の候補づくりなど)
        self.first = first  # 台の空きを先にもらう側 (物語。量産ラインと両方が待っているとき)
        self.pool = pool or POOL  # 台の空き (PrismLab の中のエンジン全部で共有)
        self._key = next(_keys)  # 台の空きの置き場での、このエンジンの番号
        self.run: Optional[Run] = None
        self.machines: list[Machine] = []
        self.llm_status = ""
        self._cond = threading.Condition()
        self._threads: dict = {}  # ("llm",) / (台の URL の小文字, n) → スレッド (終わるときに自分で外す)
        self._stop = False
        self._cancels: dict[int, threading.Event] = {}  # id(Item) → キャンセル
        self._save_failed = False
        self._passed: Optional[Item] = None  # _pick が後回しにした先頭の件 (ロックを持って使う)
        self._english: dict = {}  # 英語に: 訳した文の覚え (元の文 → 訳。LLM のスレッドだけが使う)
        self._llm_down_until = 0.0  # 英語に: LLM に繋がらなかった。この時刻までは確かめずに訳さずに進める

    # ------------------------------------------------------------------
    # 外から呼ぶもの (画面のスレッド)
    # ------------------------------------------------------------------
    @property
    def active(self) -> bool:
        with self._cond:
            return bool(self._threads)

    def set_machines(self, urls: list[str]) -> None:
        names = {p.value.rstrip("/").lower(): p.name for p in comfy.connections() if p.value}
        with self._cond:
            old = {m.url.lower(): m for m in self.machines}
            result = []
            for url in urls[:recipes.MAX_MACHINES]:
                m = old.get(url.lower()) or Machine(url)
                m.name = names.get(url.lower(), m.name)
                if m.state == OFF:  # 台の設定を開き直したら、外した台も戻す
                    m.state, m.reason, m.fail_streak = UP, "", 0
                result.append(m)
            self.machines = result
            self._unpin_missing()
            if self._threads and not self._stop:
                self._ensure_threads()
            self._cond.notify_all()
        self.changed.emit()

    def start(self, run: Run) -> None:
        """始める (続きから始めるときも)。"""
        with self._cond:
            if self.run is not None and any(i.state == RUNNING for i in self.run.items):
                raise RuntimeError("量産が動いています。")
            self.run = run
            self._english = {}  # 訳した文の覚えは、この量産のあいだだけ (モデルを変えたら訳し直す)
            for picker in run.pickers:  # 差し込みの位置を書く (ほかのアプリと共有)
                picker.commit()
            run.pickers = []
            self._stop = False
            self._unpin_missing()
            self._ensure_threads()
            self._cond.notify_all()
        self._save()
        self.changed.emit()

    def show(self, run: Optional[Run]) -> None:
        """動かさずに見せる (終わった量産の結果)。"""
        with self._cond:
            if self._threads:
                return
            self.run = run
        self.changed.emit()

    def set_paused(self, paused: bool) -> None:
        with self._cond:
            if self.run is None:
                return
            self.run.paused = paused
            self._cond.notify_all()
        self._save()
        self.changed.emit()

    def cancel_all(self) -> None:
        with self._cond:
            if self.run is None:
                return
            for it in self.run.items:
                if it.state in (WAITING, GATED):
                    it.state = CANCELED
            for ev in self._cancels.values():
                ev.set()
            self._cond.notify_all()
        self._save()
        self._check_finished()
        self.changed.emit()

    def retry_failed(self) -> int:
        """失敗したものを、失敗したステップからやり直す。戻り値は件数。"""
        with self._cond:
            if self.run is None:
                return 0
            count = 0
            for it in self.run.items:
                if it.state == FAILED:
                    it.state, it.error, it.error_stage = WAITING, "", -1
                    count += 1
            if count:
                self._stop = False
                self._ensure_threads()
            self._cond.notify_all()
        if count:
            self._save()
            self.changed.emit()
        return count

    def stop_threads(self) -> None:
        """閉じるとき: スレッドに終わるよう伝える (ComfyUI 側の実行は止めない)。"""
        with self._cond:
            self._stop = True
            self._cond.notify_all()

    def discard(self) -> None:
        """捨てて閉じる / 中断する: 残りを取り消し、頼み済みのものは ComfyUI の待ち行列から外す。"""
        with self._cond:
            run = self.run
            pinned = [] if run is None else [(i.machine, i.prompt_id) for i in run.items
                                             if not i.finished and i.prompt_id and i.machine]
            if run is not None:
                for it in run.items:
                    if not it.finished:
                        it.state = CANCELED
            self._stop = True
            for ev in self._cancels.values():
                ev.set()
            self._cond.notify_all()
        for url, pid in pinned:
            try:
                comfy.cancel_prompt(url, pid)
            except Exception:  # noqa: BLE001 - 繋がらなければそのまま
                pass
        self._save()

    def update_stage(self, index: int, snap: dict) -> None:
        """量産中にステップの中身を直す (2026-11-19 利用者の希望)。これからそのステップに来る件から効き、通った件はそのまま。
        並び・種類・バイパス・ワークフローは変えられない (ワークフローのファイルは量産中は直さない運用)。
        差し込みのフォルダが変われば、まだそこを通っていない件の割り当てを新しいフォルダで引き直す。
        増やすの数が変われば、そのあとの差し込みで足りなくなる枝の割り当てを足す。合わなければ ValueError。"""
        run = self.run
        if run is None or not 0 <= index < len(run.stages):
            raise ValueError("量産が動いていません。")
        old = run.stages[index]
        if old["type"] != snap.get("type") or bool(old.get("bypass")) != bool(snap.get("bypass")):
            raise ValueError("量産中は、ステップの種類とバイパスは変えられません。")
        if old["type"] == recipes.GEN and str(old.get("workflow", "")).lower() != str(snap.get("workflow", "")).lower():
            raise ValueError("量産中は、ワークフローは変えられません。")
        stages = list(run.stages)
        stages[index] = snap
        redo = set()  # 割り当てを引き直す差し込み
        fill = set()  # 足りない枝だけ足す差し込み
        live = [j for j, s in enumerate(stages) if s["type"] in PLANNED_TYPES and not s.get("bypass")]
        if snap["type"] == recipes.INSERT and not snap.get("bypass") and snap.get("folder") != old.get("folder"):
            redo.add(index)
        if snap["type"] in (recipes.REF_FOLDER, recipes.IMG_FOLDER) and not snap.get("bypass") and (
                snap.get("folder") != old.get("folder") or bool(snap.get("shuffle")) != bool(old.get("shuffle"))):
            redo.add(index)
        if snap["type"] == recipes.SPLIT and not snap.get("bypass") and int(snap.get("count") or 2) != int(old.get("count") or 2):
            fill.update(j for j in live if j > index)
        try:
            pickers = {j: make_picker(stages[j]) for j in redo | fill}
        except (inserts.InsertError, ValueError) as exc:
            raise ValueError(str(exc)) from exc
        with self._cond:
            if self.run is not run:
                raise ValueError("量産が変わりました。")
            planned = []  # (件, 鍵, 割り当て)。全部引けてから入れる (途中で失敗したら何も変えない)
            try:
                for it in run.items:
                    if it.state in (DONE, CANCELED, SKIPPED):
                        continue
                    for j, picker in pickers.items():
                        if it.stage > j or (it.stage == j and it.state == RUNNING):
                            continue  # もう通った (通っている) 差し込み
                        # ここから j までの増やすで、この件が何本に分かれるか
                        branches = 1
                        for k in range(it.stage, j):
                            if stages[k]["type"] == recipes.SPLIT and not stages[k].get("bypass"):
                                branches *= int(stages[k].get("count") or 2)
                        for c in range(branches):
                            key = plan_key(j, it.branch * branches + c)
                            if j in redo or key not in it.plan:
                                planned.append((it, key, picker.next()[0]))
            except inserts.InsertError as exc:
                raise ValueError(str(exc)) from exc
            for it, key, slot in planned:
                it.plan[key] = slot
            run.stages = stages
            self._cond.notify_all()
        for picker in pickers.values():
            if run.trial:
                picker.discard()  # 試作では位置を進めない
            else:
                picker.commit()
        self._save()
        self.changed.emit()

    def in_use(self, paths) -> set[str]:
        """paths のうち、終わっていない件が次のステップに渡すもの (小文字のパス)。消すと続きが作れない。"""
        keys = {str(p).lower() for p in paths}
        with self._cond:
            if self.run is None:
                return set()
            return {it.current.lower() for it in self.run.items if not it.finished and it.current.lower() in keys}

    def forget_outputs(self, paths) -> None:
        """消したファイルを結果から外す (件の outputs から除いて保存する)。"""
        keys = {str(p).lower() for p in paths}
        if not keys:
            return
        with self._cond:
            if self.run is None:
                return
            for it in self.run.items:
                it.outputs = [o for o in it.outputs if str(o.get("path") or "").lower() not in keys]
        self._save()
        self.changed.emit()

    def snapshot(self) -> tuple[Optional[Run], list[Item], list[Machine]]:
        """画面に出すための写し。"""
        with self._cond:
            if self.run is None:
                return None, [], [Machine(**m.__dict__) for m in self.machines]
            items = [Item(**{**i.__dict__, "outputs": list(i.outputs)}) for i in self.run.items]
            return self.run, items, [Machine(**m.__dict__) for m in self.machines]

    # ------------------------------------------------------------------
    # スレッド
    # ------------------------------------------------------------------
    def _ensure_threads(self) -> None:
        """足りないスレッドを始める (ロックを持って呼ぶ)。終わるスレッドはロックを持ったまま自分を外すので、
        終わりかけのスレッドを当てにして始めない、ということは起きない。"""
        wanted = [("llm",)] + [(m.url.lower(), n) for m in self.machines for n in range(SLOTS_PER_MACHINE)]
        for key in wanted:
            if key in self._threads:
                continue
            if key == ("llm",):
                t = threading.Thread(target=self._llm_loop, args=(key,), daemon=True, name="prismlab-llm")
            else:
                url = next(m.url for m in self.machines if m.url.lower() == key[0])
                t = threading.Thread(target=self._gen_loop, args=(key, url), daemon=True,
                                     name=f"prismlab-gen-{url}-{key[1]}")
            self._threads[key] = t
            t.start()

    def _exit(self, key) -> None:
        """スレッドが終わるとき (ロックを持って呼ぶ)。"""
        self._threads.pop(key, None)

    def _unpin_missing(self) -> None:
        """頼み済みの台が今の台にない (外した・休みにした) ときは、ほかの台で頼み直す。
        その台の待ち行列に残った頼みは取り消す (休みの台で同じものを作らないように。動いている件はそのまま)。"""
        if self.run is None:
            return
        urls = {m.url.lower() for m in self.machines}
        drop = []
        for it in self.run.items:
            if it.machine and it.machine.lower() not in urls:
                if it.state == WAITING and it.prompt_id:
                    drop.append((it.machine, it.prompt_id))
                it.machine, it.prompt_id = "", ""
        if drop:
            threading.Thread(target=_cancel_prompts, args=(drop,), daemon=True).start()

    def _machine(self, url: str) -> Optional[Machine]:
        for m in self.machines:
            if m.url.lower() == url.lower():
                return m
        return None

    def _pick(self, gen: bool, url: str = "") -> Optional[Item]:
        """次にやる件。生成なら、その台が最後に動かしたワークフローと同じものを少しだけ先にする
        (後回しにした先頭の件は self._passed に入れておき、本当に取ったときに数える)。"""
        self._passed = None
        run = self.run
        if run is None or run.paused:
            return None
        candidates = []
        for it in run.items:
            if it.state != WAITING or it.stage >= len(run.stages):
                continue
            is_gen = run.stages[it.stage]["type"] == recipes.GEN
            if gen != is_gen:
                continue
            if not gen:
                return it
            if it.machine:
                if it.machine.lower() != url.lower():
                    continue  # ほかの台に頼み済み (その台で続きを待つ)
                return it  # この台に頼み済み: 先に続きを待つ
            candidates.append(it)
            if len(candidates) >= AFFINITY_LOOKAHEAD:
                break
        if not candidates:
            return None
        first = candidates[0]
        last = last_workflow(url)
        if last and first.skips < AFFINITY_MAX_SKIPS:
            for it in candidates:
                if str(run.stages[it.stage].get("workflow", "")).lower() == last:
                    if it is not first:
                        self._passed = first
                    return it
        return first

    def _llm_loop(self, key) -> None:
        while True:
            with self._cond:
                while True:
                    if self._stop:
                        self._exit(key)
                        return
                    it = self._pick(gen=False)
                    if it is not None:
                        it.state = RUNNING
                        cancel = self._cancels.setdefault(id(it), threading.Event())
                        break
                    self._cond.wait(1.0)
            self._run_llm(it, cancel)

    def _gen_loop(self, key, url: str) -> None:
        while True:
            check = False
            with self._cond:
                while True:
                    if self._stop:
                        self._exit(key)
                        return
                    m = self._machine(url)
                    if m is None:
                        self._exit(key)
                        return  # 台の設定で外された
                    if m.state == DOWN and not m.checking and time.time() - m.last_check >= DOWN_RECHECK:
                        m.checking = check = True
                        break
                    self._publish()
                    if m.state == UP:
                        self.pool.alive(self._key, url)
                        it = self._pick(gen=True, url=url)
                        # 台の空きはエンジン全部で分け合う (1 台に合わせて 2 件まで。物語が先、あとは使っている数の少ない側)
                        if it is not None and self.pool.take(self._key, url, pinned=bool(it.machine)):
                            if self._passed is not None:
                                self._passed.skips += 1
                            _set_last_workflow(url, str(self.run.stages[it.stage].get("workflow", "")))
                            it.state = RUNNING
                            it.at = url
                            cancel = self._cancels.setdefault(id(it), threading.Event())
                            self._publish()
                            break
                    self._cond.wait(1.0)
            if check:
                self._recheck(m)
                continue
            try:
                self._run_gen(it, m, cancel)
            finally:
                with self._cond:
                    self._publish()
                self.pool.put_back(self._key, url)

    def _recheck(self, m: Machine) -> None:
        ok = comfy.test_connection(m.url) is None
        with self._cond:
            m.checking = False
            m.last_check = time.time()
            if ok and m.state == DOWN:
                m.state, m.reason = UP, ""
            self._cond.notify_all()
        self.changed.emit()

    # ------------------------------------------------------------------
    # ステップ
    # ------------------------------------------------------------------
    def _set_status(self, it: Item, text: str) -> None:
        it.status = text
        self.changed.emit()

    def _run_llm(self, it: Item, cancel: threading.Event) -> None:
        stage = self.run.stages[it.stage]
        try:
            if stage["type"] == recipes.READ:
                path = Path(it.current)
                text = None
                if not stage.get("reread"):
                    cached = reader.read_caption(path)
                    text = cached.strip() if cached and cached.strip() else None
                if text is None:
                    self._set_status(it, "読み取っています...")
                    self.llm_status = f"読み取り: {path.name}"
                    text = _cancellable(lambda: reader.read_with(stage["reader"], path), cancel)
                    try:
                        reader.write_caption(path, text)
                    except OSError:
                        pass  # 書けなくても、この量産では使う
                with self._cond:
                    it.text, it.enhanced = text, None
                    it.read_name = str(stage["reader"].get("name", ""))
            elif stage["type"] in recipes.ADD_TYPES:
                self._add_text(it, stage)
            elif stage["type"] in recipes.MEMO_TYPES:
                self._memo(it, stage)
            elif stage["type"] == recipes.FRAME:
                self._last_frame(it)
            elif stage["type"] == recipes.SPLIT:
                self._split(it, stage)
            elif stage["type"] in recipes.REF_TYPES:
                self._reference(it, stage)
            elif stage["type"] in recipes.IMG_TYPES:
                self._load_image(it, stage)
            elif stage["type"] == recipes.SUB:
                self._substitute(it, stage)
            elif stage["type"] == recipes.TRANSLATE:
                self._to_english(it, stage, cancel)
            elif stage["type"] == recipes.GATE:
                if not self._gate_pass(it, stage):
                    with self._cond:
                        it.state, it.status, it.at = GATED, "", ""
                        self._cancels.pop(id(it), None)
                        self._cond.notify_all()
                    self._save()
                    self._check_finished()
                    self.changed.emit()
                    return
            else:
                source = inserts.wrap(it.text, it.adds, True)  # 読み取った文 + 盛る前に足した文 (前に足すものは前)
                if not source.strip():
                    raise llm.LLMError("盛る元の文がありません。前に読み取り・追加テキスト・差し込みのステップを置いてください。")
                self._set_status(it, "AI で盛っています...")
                self.llm_status = f"盛る: {Path(it.current).name}"
                enhanced = _cancellable(lambda: enhancer.enhance_with(stage["enhancer"], source), cancel)
                if not enhanced.strip():
                    raise llm.LLMError("盛った結果が空でした。")
                with self._cond:
                    it.enhanced = enhanced
            self._advance(it)
        except comfy.GenerationCancelled:
            self._finish(it, CANCELED)
        except Exception as exc:  # noqa: BLE001 - LLM・ファイルの失敗はその件の失敗
            self._finish(it, FAILED, str(exc))
        finally:
            self.llm_status = ""

    def _add_text(self, it: Item, stage: dict) -> None:
        """追加テキスト・差し込みの文を足す。まだ盛っていなければ盛る前 (このあとの盛るに渡す)、盛ったあとならそのまま付ける。
        front なら、それまでの文の前に足す (組み立ては inserts.wrap)。"""
        before = it.enhanced is None
        if stage["type"] == recipes.TEXT:
            # {a|b|c} は 1 件ごとに 1 つ選ぶ (選んだ文を記録に残す)
            add = {"slot": -1, "name": recipes.stage_title(stage), "folder": "", "before": before, "file": "",
                   "text": recipes.expand_wildcards(str(stage.get("text") or ""))}
        else:
            planned = it.plan.get(plan_key(it.stage, it.branch)) or it.plan.get(str(it.stage))
            if not planned:
                raise ValueError("差し込みのファイルが割り当てられていません (始め直してください)。")
            add = {"slot": -1, "name": recipes.stage_title(stage), "folder": str(planned.get("folder") or ""),
                   "before": before,
                   "file": str(planned.get("file") or ""), "text": str(planned.get("text") or "")}
        if stage.get("front"):
            add["front"] = True
        with self._cond:
            it.adds.append(add)

    def _memo(self, it: Item, stage: dict) -> None:
        """覚える: その時点のプロンプト (元の文・盛った文・足した文) をメモに写す。
        思い出す: メモから戻す。置き換えるならそっくり戻し、後ろに足す (前に足す) なら、今の状態で送る文の後ろ (前) に、
        メモの状態で送る文をつなげる (それを元の文にする。あとに盛るを置けば一緒に盛る)。"""
        name = str(stage.get("memo") or recipes.DEFAULT_MEMO)
        with self._cond:
            if stage["type"] == recipes.SAVE:
                it.memos[name] = {"text": it.text, "enhanced": it.enhanced, "adds": [dict(a) for a in it.adds]}
                return
            memo = it.memos.get(name)
            if memo is None:
                raise ValueError(f"メモ「{name}」を覚えていません (前に「覚える」を置いてください)。")
            text, enhanced = memo.get("text"), memo.get("enhanced")
            adds = [dict(a) for a in memo.get("adds") or [] if isinstance(a, dict)]
            has_now = it.text is not None or it.enhanced is not None or bool(it.adds)
            if stage.get("mode") in (recipes.APPEND, recipes.PREPEND) and has_now:
                now, mine = _sent_body(it.text, it.enhanced, it.adds), _sent_body(text, enhanced, adds)
                it.text = inserts.join(mine, now) if stage.get("mode") == recipes.PREPEND else inserts.join(now, mine)
                it.enhanced, it.adds = None, []
            else:
                it.text, it.enhanced, it.adds = text, enhanced, adds

    def _reference(self, it: Item, stage: dict) -> None:
        """リファレンス: このあとの生成に渡すリファレンス画像を決める (生成を通っても残る。次のリファレンスで置き換え)。
        ワークフローにリファレンス用の LoadImage がなければ、渡しても使われない。"""
        if stage["type"] == recipes.REF_IMAGE:
            path = str(stage.get("image") or "")
        else:
            planned = it.plan.get(plan_key(it.stage, it.branch)) or it.plan.get(str(it.stage))
            if not planned:
                raise ValueError("リファレンスの画像が割り当てられていません (始め直してください)。")
            path = str(planned.get("file") or "")
        if not path or not Path(path).is_file():
            raise ValueError(f"リファレンスの画像が見つかりません: {path or '(なし)'}")
        with self._cond:
            it.reference = path

    def _load_image(self, it: Item, stage: dict) -> None:
        """画像: 流れている画像を、決めた 1 枚 (フォルダなら割り当てた 1 枚) に差し替える。文はそのまま。"""
        if stage["type"] == recipes.IMG_ONE:
            path = str(stage.get("image") or "")
        else:
            planned = it.plan.get(plan_key(it.stage, it.branch)) or it.plan.get(str(it.stage))
            if not planned:
                raise ValueError("画像が割り当てられていません (始め直してください)。")
            path = str(planned.get("file") or "")
        if not path or not Path(path).is_file():
            raise ValueError(f"画像が見つかりません: {path or '(なし)'}")
        with self._cond:
            it.current = path

    def _last_frame(self, it: Item) -> None:
        """最後のコマ: 動画の最後のコマを (出力フォルダ)\\コマ に PNG で書き、次のステップにはその画像を渡す。"""
        from . import story  # OpenCV は使うときに読む
        video = Path(it.current)
        if video.suffix.lower() not in (".mp4", ".webm", ".mov", ".mkv", ".avi", ".gif"):
            raise ValueError(f"最後のコマ: 動画ではありません ({video.name})。前に動画の生成を置いてください。")
        self._set_status(it, "最後のコマを取り出しています...")
        dest = Path(self.run.output_dir) / FRAME_DIR / f"{video.stem}_last.png"
        story.save_frame(str(video), dest, -1)
        with self._cond:
            it.current = str(dest)

    def _split(self, it: Item, stage: dict) -> None:
        """増やす: この件を count 本に分ける (ここまでの文・メモ・出来たファイルはそのまま。
        ここまでに出来た結果は元の件にだけ残す)。分けたものは元の件のすぐあとに並べる。"""
        count = max(2, int(stage.get("count") or 2))
        with self._cond:
            run = self.run
            nxt = next_stage(run.stages, it.stage)
            clones = []
            for c in range(1, count):
                clone = Item(it.source, current=it.current, stage=nxt,
                             state=DONE if nxt >= len(run.stages) else WAITING,
                             text=it.text, origin_text=it.origin_text, enhanced=it.enhanced, read_name=it.read_name,
                             adds=[dict(a) for a in it.adds], reference=it.reference, last_frame=it.last_frame,
                             memos=json.loads(json.dumps(it.memos)), plan=dict(it.plan), branch=it.branch * count + c,
                             out_dir=it.out_dir)
                clones.append(clone)
            it.branch *= count
            at = run.items.index(it) + 1
            run.items[at:at] = clones

    def _substitute(self, it: Item, stage: dict) -> None:
        """置き換え: いまのプロンプト (元の文・盛った文・足した文) の語を置き換える・消す。"""
        rules = [(str(r[0]), str(r[1])) for r in stage.get("rules") or [] if isinstance(r, (list, tuple)) and len(r) == 2]
        with self._cond:
            it.text = recipes.apply_rules(it.text, rules)
            it.enhanced = recipes.apply_rules(it.enhanced, rules)
            for a in it.adds:
                a["text"] = recipes.apply_rules(str(a.get("text") or ""), rules)

    def _to_english(self, it: Item, stage: dict, cancel: threading.Event) -> None:
        """英語に: いまのプロンプト (元の文・盛った文・足した文) のうち、日本語などが入っているものだけを LLM で英語に訳す。
        同じ文は 1 回だけ訳す (追加テキストなど、どの件にも同じ文があるため)。"""
        with self._cond:
            parts = [it.text, it.enhanced] + [str(a.get("text") or "") for a in it.adds]
        todo = [t for t in dict.fromkeys(parts) if needs_english(t) and t not in self._english]
        snap = {"url": stage.get("url") or settings.llm_url(), "model": stage.get("model") or settings.enhance_model()}
        for n, text in enumerate(todo):
            if time.monotonic() < self._llm_down_until:
                it.warning = ENGLISH_SKIPPED
                break
            self._set_status(it, "英語にしています..." + (f" ({n + 1}/{len(todo)})" if len(todo) > 1 else ""))
            self.llm_status = f"英語に: {Path(it.current).name}"
            # 訳す指示は物語の「英語に」と同じ ([セリフ] の見出し・<lora:...> などは残す)
            try:
                self._english[text] = _cancellable(lambda: translate.to_english(snap, text), cancel)
            except llm.LLMOffline:
                # LLM が落ちている: 訳さずにそのまま送る (止めない。知らせは件・まとめ・頼んだアプリに出す)
                self._llm_down_until = time.monotonic() + LLM_DOWN_SKIP
                it.warning = ENGLISH_SKIPPED
                break

        def en(t):
            return self._english.get(t, t) if needs_english(t) else t

        with self._cond:
            it.text = en(it.text)
            it.enhanced = en(it.enhanced)
            for a in it.adds:
                a["text"] = en(str(a.get("text") or ""))

    def _gate_pass(self, it: Item, stage: dict) -> bool:
        """★を待つ: いまのファイルに★が stars 以上あれば通す。試作は止めない。"""
        if self.run.trial:
            return True
        return rating.get_stars(Path(it.current)) >= int(stage.get("stars") or 1)

    def gated(self) -> int:
        """★待ちの件数。"""
        with self._cond:
            return 0 if self.run is None else sum(1 for it in self.run.items if it.state == GATED)

    def recheck_gates(self) -> int:
        """★待ちの件の★を読み直し、付いたものを先へ進める (★を読むのに時間がかかるので、画面のスレッドから呼ばない)。
        戻り値は進めた件数。"""
        with self._cond:
            run = self.run
            if run is None:
                return 0
            waiting = [(it, it.current, int(run.stages[it.stage].get("stars") or 1)) for it in run.items
                       if it.state == GATED and it.stage < len(run.stages)]
        passed = [it for it, path, need in waiting if rating.get_stars(Path(path)) >= need]
        moved = 0
        for it in passed:
            with self._cond:
                if it.state != GATED or self.run is not run:
                    continue
                it.state = RUNNING
            self._advance(it)
            moved += 1
        return moved

    def end_gates(self) -> int:
        """★待ちを終える: まだ★のない件を「★なし」で終える (ここまでに出来たものはそのまま残す)。戻り値は件数。"""
        with self._cond:
            if self.run is None:
                return 0
            ended = [it for it in self.run.items if it.state == GATED]
            for it in ended:
                it.state = SKIPPED
            self._cond.notify_all()
        for it in ended:
            self._inherit(it)
        self._save()
        self._check_finished()
        self.changed.emit()
        return len(ended)

    def _run_gen(self, it: Item, m: Machine, cancel: threading.Event) -> None:
        run = self.run
        stage = run.stages[it.stage]
        req = comfy.GenRequest(
            connection_name=m.name, url=m.url,
            workflow_name=str(stage.get("workflow_name", "")), workflow_path=str(stage.get("workflow", "")),
            # 読み取った文も足した文もなければ、ワークフローに書かれたプロンプトのまま (None)
            prompt=(it.text or "") if (it.text is not None or it.adds) else None, seconds=stage.get("seconds"),
            base_prompt=str(stage.get("base") or "") if (it.text is not None or it.adds) else "",
            enhanced_prompt=it.enhanced,
            inserts=[dict(a) for a in it.adds] or None,
            reference=it.reference or None,
            last_frame=it.last_frame or None,
            reader={"name": it.read_name} if it.read_name else None,
            output_dir=it.out_dir or run.output_dir,
        )
        if self.run_file is not None:
            # 量産・試作: 入口のファイルを記録に残す (PromptWordCrafter の生成結果の帯がテキストからたどる)
            req.origin, req.origin_text = it.source, it.origin_text
            req.recipe, req.step = run.name, it.stage + 1
        if it.enhanced is not None:
            names = [s.get("enhancer", {}).get("name", "") for s in run.stages[:it.stage]
                     if s["type"] == recipes.ENHANCE and not s.get("bypass")]
            req.enhancer = {"name": names[-1] if names else ""}
        resume_id = it.prompt_id if it.machine.lower() == m.url.lower() else ""

        def submitted(pid: str):
            with self._cond:
                it.prompt_id, it.machine = pid, m.url
            self._save()

        try:
            outputs = comfy.generate(it.current, req, lambda t: self._set_status(it, t), cancel,
                                     prompt_id=resume_id, on_submitted=submitted,
                                     held=lambda: bool(self.run and self.run.paused))
        except comfy.GenerationCancelled:
            self._finish(it, CANCELED)
            return
        except comfy.GenerationHeld as held:
            with self._cond:
                it.state, it.status, it.at = WAITING, "", ""
                if not held.sent:
                    it.prompt_id, it.machine = "", ""
                self._cond.notify_all()
            self._save()
            self.changed.emit()
            return
        except comfy.ComfyError as exc:
            reachable = comfy.test_connection(m.url) is None
            if not reachable:
                # 台が落ちた: この件は待機に戻して、ほかの台に回す
                with self._cond:
                    m.state, m.reason, m.last_check = DOWN, f"{time.strftime('%H:%M')} から繋がりません", time.time()
                    it.state, it.status, it.at = WAITING, "", ""
                    for other in self.run.items:  # この台に頼み済みのものも、ほかの台で頼み直す
                        if other.machine.lower() == m.url.lower() and other.state == WAITING:
                            other.prompt_id, other.machine = "", ""
                    it.prompt_id, it.machine = "", ""
                    self._cond.notify_all()
                self._save()
                self.changed.emit()
                return
            with self._cond:
                m.fail_streak += 1
                others_ok = any(o is not m and o.state == UP and o.fail_streak == 0 for o in self.machines)
                if m.fail_streak >= FAIL_LIMIT and others_ok:
                    m.state = OFF
                    m.reason = f"{FAIL_LIMIT} 件続けて失敗したので外しました: {str(exc).splitlines()[0]}"
            self._finish(it, FAILED, str(exc))
            return
        except Exception as exc:  # noqa: BLE001
            self._finish(it, FAILED, str(exc))
            return

        for out in outputs:
            comfy.add_history(out, it.current, req)
        # 使ったリファレンス (ワークフローにリファレンスの入口があるときだけ。結果の欄に出す)
        used_ref = it.reference if it.reference and kinds.has_reference(str(stage.get("workflow", ""))) else ""
        with self._cond:
            now = time.time()
            for out in outputs:
                # 使ったプロンプトも残す (このあと空に戻すので、結果の欄で見られるように)
                it.outputs.append({"stage": it.stage, "path": out, "time": now, "from": it.current,
                                   "prompt": it.text or "", "enhanced": it.enhanced or "",
                                   "adds": [dict(a) for a in it.adds], "sent": req.full_prompt or "",
                                   **({"reference": used_ref} if used_ref else {})})
            it.current = outputs[0]
            it.text, it.enhanced, it.read_name = None, None, ""  # 新しいファイルには新しい説明が要る
            it.adds = []
            it.prompt_id, it.machine = "", ""
            m.done += 1
            m.fail_streak = 0
        self._advance(it)

    def _inherit(self, it: Item) -> None:
        """元の★を、この件で出来たファイルに引き継ぐ。見るのは終わった時点の★ (生成中に付けたものも)。"""
        run = self.run
        if run is None or not run.recipe.get("inherit", True) or not it.outputs:
            return
        stars = rating.get_stars(Path(it.source))
        if stars:
            for o in it.outputs:
                rating.set_stars(Path(o["path"]), stars)

    def _advance(self, it: Item) -> None:
        with self._cond:
            it.stage = next_stage(self.run.stages, it.stage)  # バイパスは飛ばす
            done = it.stage >= len(self.run.stages)
        if done:
            self._inherit(it)  # 完了にする前に (完了を見て J-Rate で開いたときに★が付いているように)
        with self._cond:
            it.status, it.at = "", ""
            it.state = DONE if done else WAITING
            self._cancels.pop(id(it), None)
            self._cond.notify_all()
        self._save()
        self._check_finished()
        self.changed.emit()

    def _finish(self, it: Item, state: str, error: str = "") -> None:
        with self._cond:
            it.state, it.status, it.at = state, "", ""
            if state == FAILED:
                it.error, it.error_stage = error, it.stage
            it.prompt_id, it.machine = "", ""
            self._cancels.pop(id(it), None)
            self._cond.notify_all()
        self._save()
        self._check_finished()
        self.changed.emit()

    def _check_finished(self) -> None:
        with self._cond:
            if self.run is None or self._stop or any(not i.finished for i in self.run.items):
                return
            self._stop = True
            self._cond.notify_all()
        self.pool.forget(self._key)  # 台の空きの置き場から外す (また始めれば、知らせたときに戻る)
        self.finished.emit()

    def _demand(self) -> tuple[int, dict]:
        """生成を待っている件数 (ロックを持って呼ぶ): (どの台にも頼んでいない件, 台 → 頼み済みの件)。一時停止・止めるときは 0。"""
        run = self.run
        free, pinned = 0, {}
        if run is None or run.paused or self._stop:
            return free, pinned
        for it in run.items:
            if it.state == WAITING and it.stage < len(run.stages) and run.stages[it.stage]["type"] == recipes.GEN:
                if it.machine:
                    pinned[it.machine.lower()] = pinned.get(it.machine.lower(), 0) + 1
                else:
                    free += 1
        return free, pinned

    def _publish(self) -> None:
        """生成を待っている件数を台の空きの置き場に知らせる (ロックを持って呼ぶ)。"""
        free, pinned = self._demand()
        self.pool.set_demand(self._key, self.first, free, pinned, self._wake)

    def _wake(self) -> None:
        """ほかのエンジンが台を空けた: 空きを見に行く。"""
        with self._cond:
            self._cond.notify_all()

    def _save(self) -> None:
        with self._cond:
            self._publish()  # 状態が変わるたびに呼ばれるので、ここで知らせる
            if self.run is None or not self.run_file:
                return
            text = json.dumps(self.run.to_dict(), ensure_ascii=False)
        try:
            store.write_text(store.app_file(self.run_file), text)
            self._save_failed = False
        except OSError:
            self._save_failed = True
