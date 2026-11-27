"""量産ラインの待ち行列 (2026-11-27 利用者の希望)。

「開始」は 1 本の量産を待ち行列に入れる。入れた時点でレシピを写す (engine.Run.create) ので、
入れたあとでレシピを直しても、入れたものは変わらない。差し込みの割り当ても入れた時点で決めて先へ進めるので、
同じレシピを続けて入れても、同じファイルを使い回さない。

- 量産は 1 本ずつ。前の量産に送れる件 (待機中・実行中) がなくなったら (★待ちだけが残ったときも)、次を始める。
  一時停止している量産は、送れる件が残っているので、次は始めない (止めたいときは止めたまま)。
- 試作 (1 枚で試す) は並ばずにすぐ動く (量産中でも)。台の空きは engine.POOL で分け合う。
- ほかのアプリの頼み (external) はこれとは別で、受けたらすぐ動く (2026-11-23 の決定: 後から頼んだ側を待たせない)。
- 1 本ごとに %APPDATA%\\PrismLab\\line\\<id>.json に書く (エンジンが書く。待っているあいだはここで書く)。
  起動したときに読み、続きから始めるかを聞くまでは何も始めない (held)。
- 終わったものは KEEP_FINISHED 本まで一覧に残す (結果を見るため)。古いものから外す (ファイルは消さない。記録だけ)。
"""

import json
import time
import uuid
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, QTimer, Signal

from gencommon import store

from . import engine, recipes

LINE_DIR = "line"
KEEP_FINISHED = 12
SENDABLE = (engine.WAITING, engine.RUNNING)  # まだ送る (送っている) 件


class Job:
    """待ち行列の 1 本 (始めたときのレシピの写しを持つ量産 1 回)。"""

    def __init__(self, jid: str, run: engine.Run, eng: engine.Engine):
        self.id = jid
        self.run = run
        self.engine = eng
        self.recipe_ref: Optional[recipes.Recipe] = None  # 入れたときのレシピ (保存しない。なければ名前で探す)
        self.announced = run.unfinished == 0  # 終わったことを知らせた

    # 待ち行列の情報は Run.recipe["line"] に入れて、Run といっしょに書く
    @property
    def meta(self) -> dict:
        m = self.run.recipe.get("line")
        if not isinstance(m, dict):
            m = self.run.recipe["line"] = {}
        return m

    @property
    def seq(self) -> int:
        try:
            return int(self.meta.get("seq") or 0)
        except (TypeError, ValueError):
            return 0

    @property
    def waiting(self) -> bool:
        """まだ始めていない (待ち行列で順番を待っている)。"""
        return bool(self.meta.get("waiting"))

    @property
    def trial(self) -> bool:
        return self.run.trial

    @property
    def name(self) -> str:
        return self.run.name

    @property
    def finished(self) -> bool:
        return not self.waiting and self.run.unfinished == 0

    @property
    def unfinished(self) -> bool:
        """始めたが終わっていない。"""
        return not self.waiting and self.run.unfinished > 0

    def sendable(self) -> bool:
        """まだ送る件がある (待機中・実行中)。★待ちだけなら False。"""
        return not self.waiting and any(i.state in SENDABLE for i in self.run.items)

    @property
    def file(self) -> str:
        return f"{LINE_DIR}/{self.id}.json"


class LineQueue(QObject):
    """量産ラインの待ち行列。changed は一覧・状態が変わったとき、job_finished は 1 本が終わったとき (画面のスレッド)。
    job_started は待ち行列の次を始めたとき。"""

    changed = Signal()
    job_finished = Signal(object)
    job_started = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.jobs: list[Job] = []
        self.urls: list[str] = []
        self.held = False  # 起動して、続きから始めるかをまだ聞いていない (何も始めない)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(100)
        self._timer.timeout.connect(self._tick)

    # ------------------------------------------------------------------
    # 読み込み
    # ------------------------------------------------------------------
    def load(self) -> None:
        """前に書いたものを読む (始めない)。終わっていないものがあれば held にする。"""
        self._migrate()
        folder = store.app_file(LINE_DIR)
        found = []
        for path in folder.glob("*.json") if folder.is_dir() else []:
            run = engine.Run.from_dict(_read_json(path))
            if run is None:
                continue
            found.append((path.stem, run))
        for jid, run in found:
            self.jobs.append(Job(jid, run, self._engine_for(jid, run)))
        self.jobs.sort(key=lambda j: j.seq)
        self.held = any(j.unfinished or j.waiting for j in self.jobs)
        self._trim()

    def _migrate(self) -> None:
        """前の作り (量産 1 本だけを line_run.json に書いていた) のものを、待ち行列の 1 本にする。"""
        old = store.app_file(engine.RUN_FILE)
        run = engine.load_run()
        if run is None:
            return
        jid = _new_id()
        run.recipe["line"] = {"seq": 1, "waiting": False}
        try:
            store.write_text(store.app_file(f"{LINE_DIR}/{jid}.json"), json.dumps(run.to_dict(), ensure_ascii=False))
            old.unlink()
        except OSError:
            pass

    def _engine_for(self, jid: str, run: engine.Run) -> engine.Engine:
        eng = engine.Engine(self, run_file=f"{LINE_DIR}/{jid}.json")
        eng.set_machines(self.urls)
        eng.changed.connect(self._schedule)
        eng.finished.connect(self._schedule)
        eng.show(run)
        return eng

    # ------------------------------------------------------------------
    # 見る
    # ------------------------------------------------------------------
    def waiting_jobs(self) -> list[Job]:
        return sorted((j for j in self.jobs if j.waiting), key=lambda j: j.seq)

    def running_jobs(self) -> list[Job]:
        """始めて終わっていないもの (試作も)。"""
        return [j for j in self.jobs if j.unfinished]

    def finished_jobs(self) -> list[Job]:
        return [j for j in self.jobs if j.finished]

    def line_busy(self) -> bool:
        """量産 (試作でないもの) が動いているか待っている (開始を押すと待ち行列に並ぶ)。"""
        return any(not j.trial and (j.waiting or j.sendable()) for j in self.jobs)

    def active(self) -> bool:
        """動いているものがある (止めたまま置いてあるだけのものは数えない)。"""
        return any(j.unfinished and j.engine.active for j in self.jobs)

    def place(self, job: Job) -> int:
        """待っているものの何番目か (1 から。待っていなければ 0)。"""
        waiting = self.waiting_jobs()
        return waiting.index(job) + 1 if job in waiting else 0

    def engines(self) -> list[engine.Engine]:
        return [j.engine for j in self.jobs]

    # ------------------------------------------------------------------
    # 入れる・始める
    # ------------------------------------------------------------------
    def add(self, run: engine.Run, recipe: Optional[recipes.Recipe] = None) -> Job:
        """待ち行列に入れる。試作はすぐ始める。量産は、前の量産に送る件がなければすぐ始める。"""
        for picker in run.pickers:  # 差し込みの位置を、入れた時点で進める (次に入れたものは続きのファイルから)
            picker.commit()
        run.pickers = []
        jid = _new_id()
        run.recipe["line"] = {"seq": max((j.seq for j in self.jobs), default=0) + 1, "waiting": not run.trial}
        job = Job(jid, run, self._engine_for(jid, run))
        job.recipe_ref = recipe
        job.announced = False
        self.jobs.append(job)
        if run.trial:
            self._start(job)
        else:
            self._write(job)
            self._tick()
        self.changed.emit()
        return job

    def _start(self, job: Job) -> None:
        job.meta["waiting"] = False
        job.run.started = time.time()  # 残り時間は始めたときから数える
        job.announced = False
        job.engine.set_machines(self.urls)
        job.engine.start(job.run)

    def _write(self, job: Job) -> None:
        """待っているものを書く (始めたものはエンジンが書く)。"""
        try:
            store.write_text(store.app_file(job.file), json.dumps(job.run.to_dict(), ensure_ascii=False))
        except OSError:
            pass

    def _schedule(self) -> None:
        if not self._timer.isActive():
            self._timer.start()

    def _tick(self) -> None:
        """終わったものを知らせ、量産に送る件がなくなっていたら次を始める。"""
        for job in self.jobs:
            if job.waiting:
                continue
            if job.run.unfinished == 0:
                if not job.announced:
                    job.announced = True
                    self.job_finished.emit(job)
            else:
                job.announced = False  # 失敗したものをやり直した
        if not self.held and not any(not j.trial and j.sendable() for j in self.jobs):
            waiting = self.waiting_jobs()
            if waiting:
                self._start(waiting[0])
                self.job_started.emit(waiting[0])
        self._trim()
        self.changed.emit()

    def _trim(self) -> None:
        """終わったものを KEEP_FINISHED 本まで残す (古いものから外す。出来たファイルは消さない)。"""
        done = sorted(self.finished_jobs(), key=lambda j: j.run.started)
        for job in done[:max(0, len(done) - KEEP_FINISHED)]:
            self._drop(job)

    def _drop(self, job: Job) -> None:
        if job in self.jobs:
            self.jobs.remove(job)
        job.engine.run_file = None  # 止まりきるまでの書き込みで、ファイルを作り直さないように
        _unlink(store.app_file(job.file))
        # すぐには消さない (終わりかけのスレッドがまだ知らせを出すかもしれない)。誰も持たなくなったら Python が消す
        job.engine.setParent(None)

    # ------------------------------------------------------------------
    # 操作
    # ------------------------------------------------------------------
    def remove(self, job: Job) -> bool:
        """待っているもの・終わったものを一覧から外す (動いているものは外さない)。"""
        if job.unfinished:
            return False
        self._drop(job)
        self._tick()
        return True

    def move(self, job: Job, delta: int) -> bool:
        """待っているものの順番を変える (delta = -1 で先に、+1 で後に)。"""
        waiting = self.waiting_jobs()
        if job not in waiting:
            return False
        at = waiting.index(job)
        to = at + delta
        if not 0 <= to < len(waiting):
            return False
        other = waiting[to]
        a, b = job.seq, other.seq
        job.meta["seq"], other.meta["seq"] = b, a
        self.jobs.sort(key=lambda j: j.seq)
        self._write(job)
        self._write(other)
        self.changed.emit()
        return True

    def set_machines(self, urls: list[str]) -> None:
        self.urls = list(urls)
        for job in self.jobs:
            job.engine.set_machines(self.urls)

    def resume(self) -> None:
        """続きから始める: 始めて終わっていないものを動かし、待っているものは順番に。"""
        self.held = False
        for job in self.jobs:
            if job.unfinished and not job.engine.active:
                job.engine.set_machines(self.urls)
                job.engine.start(job.run)
        self._tick()

    def discard_unfinished(self) -> None:
        """中断する: 動いているものは残りを取り消し (頼み済みのものは ComfyUI の待ち行列から外す)、待っているものは外す。"""
        self.held = False
        for job in list(self.jobs):
            if job.waiting:
                self._drop(job)
            elif job.unfinished:
                job.engine.run = job.run
                job.engine.discard()
                job.engine.show(job.run)
        self._tick()

    def stop_threads(self) -> None:
        """閉じるとき: スレッドに終わるよう伝える (書いたものは残り、次に起動したときに続けられる)。"""
        for job in self.jobs:
            job.engine.stop_threads()


def _new_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass
