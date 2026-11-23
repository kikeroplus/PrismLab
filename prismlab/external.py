"""ほかのアプリから頼まれた生成 (spec 6 章。2026-11-22 利用者の決定)。

PromptWordCrafter・MuseFactory・J-Rate は、PrismLab が出したレシピ (published.json) を選び、ファイルと台を付けて
パイプに {"cmd": "run", ...} を送ってくる。PrismLab は画面を出さずに (動いていなければ --background で起動して)、
頼まれた 1 件ごとに自分の生成の窓口 (Engine) を作って裏で動かす。台の空きは量産ライン・物語と分け合う (engine.POOL)。

- 出来たファイルはそのアプリの決まりの場所に置く (PWC・MuseFactory: テキストのフォルダの「生成結果」、J-Rate: 元画像と同じフォルダ)。
- 台は頼んだアプリが選んだもの (アプリごとに違う台を使える)。動いているあいだにそのアプリで台の設定を変えると
  {"cmd": "machines", "from", "machines"} が来て、そのアプリの動いている頼みの台も入れ替える。
- 進み具合は %APPDATA%\\PrismLab\\external_status.json に書く (頼んだアプリが読んで出す)。
- 終わっていないものは %APPDATA%\\PrismLab\\external\\(番号).json に書き、次に PrismLab が起動したときに聞かずに続ける。
"""

import json
import time
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, QTimer, Signal

from gencommon import comfy, store

from . import engine, recipes

EXT_DIR = "external"
STATUS_FILE = "external_status.json"
KEEP_FINISHED = 30 * 60  # 終わったものも、これだけのあいだは進み具合に残す (頼んだアプリが「完了」を見られるように)
RESULTS_DIR = "生成結果"
MAX_PATHS = 1000  # 進み具合に書く出来たファイルの数の上限  # PromptWordCrafter・MuseFactory の出力の置き場所 (テキストのフォルダの中)


def out_dir_for(app: str, source: str) -> str:
    """そのアプリの決まりの出力フォルダ。"""
    path = Path(source)
    if app in recipes.TEXT_APPS:
        return str(path.parent / RESULTS_DIR)
    return str(path.parent)  # J-Rate: 元画像と同じフォルダ


class Request:
    def __init__(self, rid: str, app: str, recipe: str):
        self.id = rid
        self.app = app
        self.recipe = recipe
        self.engine: Optional[engine.Engine] = None
        self.error = ""  # 始められなかった理由
        self.created = time.time()
        self.finished_at: Optional[float] = None
        self.canceled = False

    def counts(self) -> dict:
        run = self.engine.run if self.engine is not None else None
        items = run.items if run is not None else []
        c = {"total": len(items), "done": 0, "failed": 0, "canceled": 0, "running": 0, "waiting": 0, "gated": 0}
        for it in items:
            key = {engine.DONE: "done", engine.SKIPPED: "done", engine.FAILED: "failed", engine.CANCELED: "canceled",
                   engine.RUNNING: "running", engine.GATED: "gated"}.get(it.state, "waiting")
            c[key] += 1
        c["outputs"] = sum(len(it.outputs) for it in items)
        c["untranslated"] = sum(1 for it in items if it.warning)  # 「英語に」で LLM に繋がらず、訳さずに送った件
        return c

    def to_status(self) -> dict:
        state = "error" if self.error else "canceled" if self.canceled else "done" if self.finished_at else "running"
        data = {"id": self.id, "app": self.app, "recipe": self.recipe, "state": state, "error": self.error,
                "created": self.created, "finished_at": self.finished_at, **self.counts()}
        if self.engine is not None:
            data["machines"] = [{"url": m.url, "name": m.label, "state": m.state, "reason": m.reason, "done": m.done}
                                for m in self.engine.machines]
            run = self.engine.run
            data["errors"] = [it.error.splitlines()[0] for it in (run.items if run else []) if it.error][:5]
            if data["untranslated"]:
                data["warning"] = f"{engine.ENGLISH_SKIPPED} ({data['untranslated']} 件)"
            # 出来たファイル (J-Rate はこれを読んで、開いているフォルダの一覧に足す)
            data["paths"] = [o["path"] for it in (run.items if run else []) for o in it.outputs][:MAX_PATHS]
        return data


class ExternalRuns(QObject):
    """頼まれた生成を持つ。changed は進み具合が変わったとき (画面のスレッドで)。"""

    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.requests: dict[str, Request] = {}
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(400)
        self._timer.timeout.connect(self._write_status)
        self._load_status()

    # ------------------------------------------------------------------
    def active(self) -> int:
        """動いている頼みの数。"""
        return sum(1 for r in self.requests.values() if r.engine is not None and r.finished_at is None
                   and not r.canceled and not r.error)

    def summary(self) -> str:
        parts = []
        for r in self.requests.values():
            if r.engine is not None and r.finished_at is None and not r.canceled and not r.error:
                c = r.counts()
                parts.append(f"{recipes.APP_SHORT.get(r.app, r.app)}「{r.recipe}」{c['done'] + c['failed']}/{c['total']}")
        return "、".join(parts)

    def receive(self, message: dict) -> str:
        """{"cmd": "run", "id", "from", "recipe", "index", "files", "machines"} を始める。だめなら理由 (進み具合にも書く)。"""
        rid = str(message.get("id") or f"r{int(time.time() * 1000)}")
        app = str(message.get("from") or "")
        name = str(message.get("recipe") or "")
        req = Request(rid, app, name)
        self.requests[rid] = req
        try:
            self._start(req, message)
        except ValueError as exc:
            req.error = str(exc)
        self._schedule()
        return req.error

    def _start(self, req: Request, message: dict) -> None:
        if req.app not in recipes.APPS:
            raise ValueError(f"{req.app or '(不明)'} からは頼めません。")
        items = recipes.load()
        try:
            index = int(message.get("index", -1))
        except (TypeError, ValueError):
            index = -1
        recipe = items[index] if 0 <= index < len(items) and items[index].name == req.recipe else \
            next((r for r in items if r.name == req.recipe), None)
        if recipe is None:
            raise ValueError(f"レシピ「{req.recipe}」が PrismLab にありません。")
        if req.app not in recipe.apps:
            raise ValueError(f"レシピ「{req.recipe}」は {req.app} に出していません (PrismLab の「ほかのアプリに出す」)。")
        files = [Path(str(f)) for f in message.get("files") or [] if isinstance(f, str) and f]
        files = [f for f in files if f.is_file()]
        if not files:
            raise ValueError("生成するファイルがありません (見つかりません)。")
        text = req.app in recipes.TEXT_APPS
        if text:
            files = [f for f in files if f.suffix.lower() == recipes.TEXT_EXT]
        else:
            files = [f for f in files if f.suffix.lower() in recipes.IMAGE_EXTS]
        if not files:
            raise ValueError("テキスト (.txt) がありません。" if text else "画像がありません。")
        if recipes.line_problems(recipe.stages, text_entry=text):
            raise ValueError(f"レシピ「{recipe.name}」は" + ("テキストからは始められません。" if text else "画像からは始められません。"))
        urls = [str(u).rstrip("/") for u in message.get("machines") or [] if isinstance(u, str) and u.strip()]
        if not urls:
            raise ValueError("台が選ばれていません。")
        r = recipe.copy()
        r.source, r.picked, r.picked_from, r.output_dir = recipes.PICKED, [str(f) for f in files], req.app, ""
        r.inherit = r.inherit and not text  # テキストには★がない
        sources = recipes.repeat_sources(files, r.repeat)
        try:
            run = engine.Run.create(r, sources)
        except (ValueError, comfy.ComfyError) as exc:
            raise ValueError(str(exc)) from exc
        for it in run.items:
            it.out_dir = out_dir_for(req.app, it.source)
        run.recipe["external"] = {"id": req.id, "app": req.app, "machines": urls[:recipes.MAX_MACHINES],
                                  "created": req.created}
        self._launch(req, run, urls)

    def _launch(self, req: Request, run: engine.Run, urls: list[str]) -> None:
        eng = engine.Engine(self, run_file=f"{EXT_DIR}/{req.id}.json")
        eng.set_machines(urls[:recipes.MAX_MACHINES])
        eng.changed.connect(self._schedule)
        eng.finished.connect(lambda r=req: self._finished(r))
        req.engine = eng
        eng.start(run)

    def set_machines(self, message: dict) -> int:
        """{"cmd": "machines", "from", "machines"}: そのアプリで台の設定を変えた。動いている頼みの台も入れ替える
        (2026-11-23 利用者の希望: PWC で台を変えたら、動いている生成もその台で続ける)。
        外した台で動いている件は最後まで動かし、その台の待ち行列に入れただけの件はほかの台で頼み直す。戻り値は入れ替えた頼みの数。"""
        app = str(message.get("from") or "")
        urls = [str(u).rstrip("/") for u in message.get("machines") or [] if isinstance(u, str) and u.strip()]
        urls = urls[:recipes.MAX_MACHINES]
        count = 0
        for req in self.requests.values():
            if req.app != app or req.engine is None or req.finished_at is not None or req.canceled or req.error:
                continue
            eng = req.engine
            if eng.run is not None:
                ext = eng.run.recipe.get("external")
                if isinstance(ext, dict):
                    ext["machines"] = list(urls)  # 次に PrismLab が起動して続けるときも、この台で
            eng.set_machines(urls)
            eng._save()  # noqa: SLF001 - 続きのファイルにも新しい台を書く
            count += 1
        self._schedule()
        return count

    def resume(self) -> int:
        """前に終わらなかった頼みを続ける (聞かない)。戻り値は続けた数。"""
        folder = store.app_file(EXT_DIR)
        count = 0
        for path in sorted(folder.glob("*.json")) if folder.is_dir() else []:
            run = engine.Run.from_dict(_read_json(path))
            ext = run.recipe.get("external") if run is not None else None
            if run is None or not isinstance(ext, dict) or not run.unfinished:
                _unlink(path)
                continue
            req = Request(path.stem, str(ext.get("app") or ""), run.name)
            req.created = float(ext.get("created") or time.time())
            self.requests[req.id] = req
            self._launch(req, run, [str(u) for u in ext.get("machines") or []])
            count += 1
        self._schedule()
        return count

    def cancel(self, rid: str) -> None:
        req = self.requests.get(rid)
        if req is None or req.engine is None or req.finished_at is not None:
            return
        req.canceled = True
        req.engine.run_file = None  # 止まりきるまでの書き込みで、続きのファイルを作り直さないように
        req.engine.discard()
        req.finished_at = time.time()
        _unlink(store.app_file(f"{EXT_DIR}/{req.id}.json"))
        self._schedule()

    def stop_threads(self) -> None:
        """PrismLab を閉じるとき: 書いたものは残し、次に起動したときに続ける。"""
        for req in self.requests.values():
            if req.engine is not None and req.finished_at is None:
                req.engine.stop_threads()
        self._write_status()

    def _finished(self, req: Request) -> None:
        if req.finished_at is None:
            req.finished_at = time.time()
        _unlink(store.app_file(f"{EXT_DIR}/{req.id}.json"))
        self._schedule()

    # ------------------------------------------------------------------
    def _schedule(self) -> None:
        if not self._timer.isActive():
            self._timer.start()
        self.changed.emit()

    def _load_status(self) -> None:
        """前の進み具合のうち、終わってまもないものは残す (頼んだアプリが見られるように)。"""
        data = _read_json(store.app_file(STATUS_FILE))
        self._kept = [d for d in (data.get("requests") if isinstance(data, dict) else None) or []
                      if isinstance(d, dict) and d.get("state") != "running"
                      and time.time() - float(d.get("finished_at") or d.get("created") or 0) < KEEP_FINISHED]

    def _write_status(self) -> None:
        now = time.time()
        for rid in [k for k, r in self.requests.items()
                    if (r.finished_at or (r.created if r.error else None) or now) < now - KEEP_FINISHED]:
            self.requests.pop(rid, None)
        live = [r.to_status() for r in self.requests.values()]
        ids = {d["id"] for d in live}
        kept = [d for d in self._kept if d.get("id") not in ids
                and now - float(d.get("finished_at") or d.get("created") or 0) < KEEP_FINISHED]
        data = {"updated": now, "pid": _pid(), "requests": kept + live}
        try:
            store.write_text(store.app_file(STATUS_FILE), json.dumps(data, ensure_ascii=False))
        except OSError:
            pass


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _pid() -> int:
    import os
    return os.getpid()
