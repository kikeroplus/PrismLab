"""量産ラインを動かす部分 (画面とは別)。

- 入口の 1 枚につき 1 件 (Item)。件はステップを左から順に通る。
- 読み取り・盛る (LLM) は 1 つのスレッドで、生成は台ごとに 2 つのスレッドで進める。
  どのスレッドも「次のステップが自分の種類で、待機中の件」を前から順に取る (終わった台から次の 1 件を取る)。
- 台に繋がらなくなったら、その台の件は待機に戻し、ほかの台に回す。繋がらない台は 30 秒ごとに確かめる。
- 状態が変わるたびに %APPDATA%\\PrismLab\\line_run.json に書く (続きから始めるため)。
"""

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import QObject, Signal

from gencommon import comfy, enhancer, llm, reader, store

from . import kinds, recipes

RUN_FILE = "line_run.json"
TRIAL_DIR = "試作"  # 1 枚で試すときの出力 (出力フォルダの下)
SLOTS_PER_MACHINE = 2  # 1 台に同時に送る件数 (generation-standard 6 章)
FAIL_LIMIT = 3  # ある台でだけ、これだけ続けて失敗したら、その台を外す
DOWN_RECHECK = 30.0  # 繋がらない台を確かめ直す間隔 (秒)

WAITING, RUNNING, DONE, FAILED, CANCELED = "waiting", "running", "done", "failed", "canceled"
STATE_TEXT = {WAITING: "待機中", RUNNING: "実行中", DONE: "完了", FAILED: "失敗", CANCELED: "取り消し"}
UP, DOWN, OFF = "up", "down", "off"


@dataclass
class Item:
    source: str
    current: str = ""  # 次のステップに渡すファイル (生成のステップを通るたびに、出来たファイルに変わる)
    stage: int = 0  # 次にやるステップの番号
    state: str = WAITING
    text: Optional[str] = None  # 元のプロンプト (読み取った説明)。None = まだない
    enhanced: Optional[str] = None  # 盛ったプロンプト
    read_name: str = ""  # 今の text を読み取った読み取りプリセットの名前
    # [{"stage": ステップ, "path": パス, "time": 時刻, "from": 元のファイル, "prompt": 読み取った文, "enhanced": 盛った文}]
    outputs: list = field(default_factory=list)
    error: str = ""
    error_stage: int = -1
    machine: str = ""  # 頼み済みの台の URL (prompt_id とセット)
    prompt_id: str = ""
    status: str = ""  # 進み具合の文 (保存しない)
    at: str = ""  # いま動いている台の URL (保存しない)

    def to_dict(self) -> dict:
        data = dict(self.__dict__)
        data.pop("status", None)
        data.pop("at", None)
        return data

    @staticmethod
    def from_dict(data: dict) -> Optional["Item"]:
        if not isinstance(data, dict) or not data.get("source"):
            return None
        it = Item(str(data["source"]))
        for key in ("current", "error", "machine", "prompt_id", "read_name"):
            setattr(it, key, str(data.get(key) or ""))
        it.current = it.current or it.source
        for key in ("text", "enhanced"):
            value = data.get(key)
            setattr(it, key, value if isinstance(value, str) else None)
        try:
            it.stage = int(data.get("stage") or 0)
            it.error_stage = int(data.get("error_stage", -1))
        except (TypeError, ValueError):
            return None
        state = data.get("state")
        it.state = state if state in STATE_TEXT else WAITING
        if it.state == RUNNING:
            it.state = WAITING  # 閉じたとき実行中だったもの (頼み済みなら prompt_id で続きを待つ)
        outs = data.get("outputs")
        it.outputs = [o for o in outs if isinstance(o, dict) and o.get("path")] if isinstance(outs, list) else []
        return it

    @property
    def finished(self) -> bool:
        return self.state in (DONE, FAILED, CANCELED)


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
        if trial:
            out = str(Path(out) / TRIAL_DIR)
        return Run(recipe.to_dict(), stages, out, items, trial=trial)

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

    def __init__(self, parent=None):
        super().__init__(parent)
        self.run: Optional[Run] = None
        self.machines: list[Machine] = []
        self.llm_status = ""
        self._cond = threading.Condition()
        self._threads: dict = {}  # ("llm",) / (台の URL の小文字, n) → スレッド (終わるときに自分で外す)
        self._stop = False
        self._cancels: dict[int, threading.Event] = {}  # id(Item) → キャンセル
        self._save_failed = False

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
                if it.state == WAITING:
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
        """頼み済みの台が今の台にないときは、ほかの台で頼み直す。"""
        if self.run is None:
            return
        urls = {m.url.lower() for m in self.machines}
        for it in self.run.items:
            if it.machine and it.machine.lower() not in urls:
                it.machine, it.prompt_id = "", ""

    def _machine(self, url: str) -> Optional[Machine]:
        for m in self.machines:
            if m.url.lower() == url.lower():
                return m
        return None

    def _pick(self, gen: bool, url: str = "") -> Optional[Item]:
        run = self.run
        if run is None or run.paused:
            return None
        for it in run.items:
            if it.state != WAITING or it.stage >= len(run.stages):
                continue
            is_gen = run.stages[it.stage]["type"] == recipes.GEN
            if gen != is_gen:
                continue
            if gen and it.machine and it.machine.lower() != url.lower():
                continue  # ほかの台に頼み済み (その台で続きを待つ)
            return it
        return None

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
                    if m.state == UP:
                        it = self._pick(gen=True, url=url)
                        if it is not None:
                            it.state = RUNNING
                            it.at = url
                            cancel = self._cancels.setdefault(id(it), threading.Event())
                            break
                    self._cond.wait(1.0)
            if check:
                self._recheck(m)
                continue
            self._run_gen(it, m, cancel)

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
            else:
                if it.text is None or not it.text.strip():
                    raise llm.LLMError("盛る元のプロンプトがありません。前に読み取りのステップを置いてください。")
                self._set_status(it, "AI で盛っています...")
                self.llm_status = f"盛る: {Path(it.current).name}"
                source = it.text
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

    def _run_gen(self, it: Item, m: Machine, cancel: threading.Event) -> None:
        run = self.run
        stage = run.stages[it.stage]
        req = comfy.GenRequest(
            connection_name=m.name, url=m.url,
            workflow_name=str(stage.get("workflow_name", "")), workflow_path=str(stage.get("workflow", "")),
            prompt=it.text, seconds=stage.get("seconds"),
            base_prompt=str(stage.get("base") or "") if it.text is not None else "",
            enhanced_prompt=it.enhanced,
            reader={"name": it.read_name} if it.read_name else None,
            output_dir=run.output_dir,
        )
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
        with self._cond:
            now = time.time()
            for out in outputs:
                # 使ったプロンプトも残す (このあと空に戻すので、結果の欄で見られるように)
                it.outputs.append({"stage": it.stage, "path": out, "time": now, "from": it.current,
                                   "prompt": it.text or "", "enhanced": it.enhanced or ""})
            it.current = outputs[0]
            it.text, it.enhanced, it.read_name = None, None, ""  # 新しいファイルには新しい説明が要る
            it.prompt_id, it.machine = "", ""
            m.done += 1
            m.fail_streak = 0
        self._advance(it)

    def _advance(self, it: Item) -> None:
        with self._cond:
            it.stage = next_stage(self.run.stages, it.stage)  # バイパスは飛ばす
            it.status, it.at = "", ""
            it.state = DONE if it.stage >= len(self.run.stages) else WAITING
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
        self.finished.emit()

    def _save(self) -> None:
        with self._cond:
            if self.run is None:
                return
            text = json.dumps(self.run.to_dict(), ensure_ascii=False)
        try:
            store.write_text(store.app_file(RUN_FILE), text)
            self._save_failed = False
        except OSError:
            self._save_failed = True
