"""量産ラインの画面。

上から: レシピの札 / ステップの並び (入口 → ステップ → …) / 出力フォルダ / 台の札 / 開始・一時停止・中止 / 結果。
量産が動いているあいだは、始めたときのステップの写しを見せ、レシピは直せない。
"""

import html
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import QCoreApplication, QEvent, QEventLoop, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (QApplication, QButtonGroup, QCheckBox, QComboBox, QFileDialog, QHBoxLayout, QLabel, QListView,
                               QListWidget, QListWidgetItem, QMenu, QMessageBox, QPushButton, QSpinBox,
                               QTextBrowser, QVBoxLayout, QWidget)

from gencommon import comfy, inserts, settings

from . import engine, flow, kinds, rating, recipes, recycle, theme
from .dialogs import MachinesDialog, StageDialog, ask_text
from .hover_preview import HoverPreview
from .text_popup import TextPopup
from .thumbs import MOVIE_EXTS, ThumbCache
from .widgets import CARD_WIDTH, AddButton, Card, HScrollArea, InsertArrow, MachineCard, flow_legend, io_html

CONFIRM_COUNT = 30  # これを超える件数で始めるときは確かめる (generation-standard 6 章)
RESULT_ICON = QSize(128, 128)
STAR_ITEMS = ["★の絞り込みなし"] + [f"★{n} 以上" for n in range(1, 6)]


def _fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    h, m = seconds // 3600, seconds % 3600 // 60
    return f"{h}時間{m}分" if h else f"{max(m, 1)}分"


def _memo_detail(stage_type: str, memo: str, mode) -> str:
    name = memo or recipes.DEFAULT_MEMO
    if stage_type == recipes.SAVE:
        return f"メモ「{name}」に覚える"
    return f"メモ「{name}」から" + {recipes.APPEND: "後ろに足す", recipes.PREPEND: "前に足す"}.get(mode, "戻す (置き換える)")


def _named_title(stage) -> str:
    """名前を付けた追加テキスト・差し込みの見出し (名前だけ。札の幅に「種類: 名前」は入らないので、種類は重ねたときに出す)。
    名前がなければ空 (種類の名前のまま)。"""
    label = stage.get("label") if isinstance(stage, dict) else stage.label
    return str(label or "")


def _front_mark(front) -> str:
    """追加テキスト・差し込みを前に足すときの印 (札の説明の頭に付ける)。"""
    return "[前に足す] " if front else ""


# ステップの種類の短い説明 (メニューの項目に重ねると横に出す。2026-11-18 利用者の希望)
STAGE_HELP = {
    recipes.READ: "画像を LLM に見せて、説明のプロンプトを書かせます (.txt があればそれを使う)。",
    recipes.ENHANCE: "ここまでの文を LLM で盛ります。1 件ごとに違う盛り方になります。",
    recipes.TRANSLATE: "ここまでの文 (日本語など) を LLM で英語にします。英語だけの文はそのまま。",
    recipes.TEXT: "決まった文をプロンプトに足します。{a|b|c} と書くと 1 件ごとにどれかを選びます。",
    recipes.INSERT: "フォルダの .txt (セリフ・アクションなど) を、1 件ごとに順に足します。",
    recipes.SUB: "プロンプトの語を置き換える・消します (1 行に「元 => 先」)。",
    recipes.SAVE: "この時点のプロンプトを名前のメモに覚えます (生成を通っても残る)。",
    recipes.LOAD: "「覚える」で覚えたプロンプトを戻します (置き換える / 後ろに足す / 前に足す)。",
    recipes.SPLIT: "そこから先を N 本に分けます。前の読み取りは 1 回で、盛る・差し込みは 1 本ずつ変わります。",
    recipes.GATE: "J-Rate で★を付けたものだけ先へ進めます。ほかは★待ちで止めます。",
    recipes.FRAME: "前の生成で出来た動画の最後のコマを画像にして、次へ渡します。",
    recipes.REF_IMAGE: "決めた 1 枚の画像を、このあとの生成のリファレンス (顔など) に使います。",
    recipes.REF_FOLDER: "フォルダの画像を 1 件ごとに 1 枚ずつ、このあとの生成のリファレンス (顔など) に使います。",
    recipes.IMG_ONE: "流れている画像を、決めた 1 枚の画像に差し替えます (文はそのまま)。テキストからの I2I・I2V にも。",
    recipes.IMG_FOLDER: "流れている画像を、フォルダの画像 (1 件ごとに 1 枚ずつ) に差し替えます (文はそのまま)。",
    recipes.GEN: "ComfyUI のワークフローで画像・動画を作ります (T2I / I2I / I2V など)。",
}

FLOW_MIN_HEIGHT = 262  # ステップの並びの欄の高さ (札がこれより高ければ伸ばす)
FLOW_MAX_HEIGHT = 520
SPLIT_COLOR = "#4fc3f7"  # 増やすの札
GATE_COLOR = "#ffca28"  # ★を待つの札
TOOL_TYPES = (recipes.FRAME, recipes.SPLIT, recipes.GATE, recipes.SUB)


def _tool_detail(stage_type: str, count: int, stars: int, rules: list) -> str:
    """最後のコマ・増やす・★を待つ・置き換えの札の説明。"""
    if stage_type == recipes.FRAME:
        return "動画の最後のコマを画像にして次へ"
    if stage_type == recipes.SPLIT:
        return f"ここから先を {count} 通りに (盛る・差し込み・ワイルドカードで 1 本ずつ変わる)"
    if stage_type == recipes.GATE:
        return f"★{stars} 以上を付けたものだけ先へ (J-Rate で付ける)"
    if not rules:
        return "(置き換える語を入れてください)"
    first = f"{rules[0][0]} → {rules[0][1] or '(消す)'}"
    return first + (f" ほか {len(rules) - 1} 個" if len(rules) > 1 else "")


def _ref_detail(stage_type: str, image: str, folder: str, shuffle: bool) -> str:
    """リファレンス・画像の札の説明。"""
    if stage_type in recipes.PICK_ONE:
        return f"画像: {Path(image).name}" if image else "(画像を選んでください)"
    if not folder:
        return "(フォルダを選んでください)"
    return f"フォルダ: {Path(folder).name}" + ("  ・ ランダム" if shuffle else "  ・ 名前順")


def _ref_files(stage_type: str, image: str, folder: str) -> list[str]:
    """リファレンス・画像の札に並べる画像 (1 枚ならそれ、フォルダなら先頭の 3 枚)。"""
    if stage_type in recipes.PICK_ONE:
        return [image] if image and Path(image).is_file() else []
    return [str(p) for p in recipes.list_images(folder)[:3]] if folder else []


def find_jrate() -> Optional[Path]:
    """J-Rate の EXE (デスクトップの Application か、隣の J-Rate のフォルダ)。"""
    here = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve()
    candidates = [Path.home() / "Desktop" / "Application" / "J-Rate.exe"]
    for parent in here.parents:
        candidates.append(parent / "J-Rate" / "J-Rate.exe")
    return next((p for p in candidates if p.is_file()), None)


class _Scan:
    """入口のフォルダの画像と、それぞれの★を裏で読む ((パス, ★の数) の並び)。"""

    def __init__(self, done):
        self._done = done
        self._token = 0

    def start(self, folder: str):
        self._token += 1
        token = self._token

        def work():
            files = recipes.list_images(folder) if folder else []
            self._done(token, folder, [(p, rating.get_stars(p)) for p in files])

        threading.Thread(target=work, daemon=True).start()
        return token

    def current(self, token: int) -> bool:
        return token == self._token


class LineView(QWidget):
    status = Signal(str)  # ステータスバーに出す文
    to_story = Signal(list)  # 結果の画像を、開いている物語の最後にシーンとして足す
    to_batch = Signal(list)  # 結果の画像を、物語のバッチの最初の画像にする (設定の窓を出す)
    machines_changed = Signal()  # 台の設定を変えた (物語のタブの札と、物語のエンジンの台を直す)
    _scanned = Signal(int, str, list)

    def __init__(self, eng: engine.Engine, parent=None):
        super().__init__(parent)
        self.engine = eng
        self.recipes = recipes.load()
        if not self.recipes:
            self.recipes = recipes.default_recipes()
        self.recipes = recipes.sort_by_group(self.recipes)  # 同じまとまりを隣どうしに (保存はいつも寄せた順)
        self._group_pick: dict = {}  # まとまり → そのタブで最後に見ていたレシピ (タブを押したときに出す)
        self._group_tabs: dict = {}  # まとまり → タブのボタン
        self.index = min(max(recipes.last_index(), 0), len(self.recipes) - 1)
        self._run_recipe: Optional[recipes.Recipe] = None  # 今の (最後の) 量産のレシピ (それだけを量産中は直せなくする)
        self.thumbs = ThumbCache(160, self)
        self.thumbs.changed.connect(lambda _p: self._schedule())
        self._scanned_files: list = []  # 入口のフォルダの (パス, ★の数)
        self._scanned_folder = None  # _scanned_files を読んだフォルダ
        self._input_key = None  # 読みに行ったフォルダ (None = 読み直す)
        self._scan = _Scan(lambda *a: self._scanned.emit(*a))
        self._scanned.connect(self._on_scanned)
        self._results_key = None
        self.trial_source: Optional[str] = None  # 1 枚で試す画像 (None = 入口の対象の先頭)
        self._prompt_html = None
        self._texts_key = None  # テキストの入口: 読んだフォルダ
        self._text_files: list = []
        self._result_items: dict = {}

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 8, 10, 8)
        root.setSpacing(8)

        # レシピのまとまり (タブ。まとまりを 1 つも作っていなければ出さない)
        self.group_row = QHBoxLayout()
        self.group_row.setContentsMargins(0, 0, 0, 0)
        self.group_row.setSpacing(2)
        self.group_host = QWidget()
        self.group_host.setLayout(self.group_row)
        root.addWidget(self.group_host)

        # レシピの札 (選んでいるまとまりのものだけ)
        self.recipe_row = QHBoxLayout()
        self.recipe_row.setSpacing(6)
        self.recipe_group = QButtonGroup(self)
        self.recipe_group.setExclusive(True)
        self.recipe_group.idClicked.connect(self._select_recipe)
        recipe_host = QWidget()
        recipe_host.setLayout(self.recipe_row)
        top = QHBoxLayout()
        top.addWidget(recipe_host)
        top.addStretch(1)
        root.addLayout(top)

        # 流れの線の見方 (ステップのあいだの矢印の色・線の形)
        self.legend = QLabel(flow_legend())
        self.legend.setTextFormat(Qt.TextFormat.RichText)
        self.legend.setStyleSheet("font-size: 11px;")
        root.addWidget(self.legend)

        # ステップの並び
        self.flow_host = QWidget()
        self.flow = QHBoxLayout(self.flow_host)
        self.flow.setContentsMargins(2, 2, 2, 2)
        self.flow.setSpacing(0)
        self.text_popup = TextPopup(self)  # 文のステップの札に乗せると、全文と日本語訳を出す
        scroll = HScrollArea()
        scroll.setWidget(self.flow_host)
        scroll.setWidgetResizable(True)
        scroll.setFixedHeight(FLOW_MIN_HEIGHT)  # 札の高さに合わせて伸ばす (_fit_flow)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        root.addWidget(scroll)
        self.flow_scroll = scroll

        # 出力フォルダ
        out_row = QHBoxLayout()
        self.out_label = QLabel()
        self.out_label.setStyleSheet(f"color: {theme.TEXT_DIM};")
        self.out_change = QPushButton("変更...")
        self.out_change.clicked.connect(self._change_output)
        self.out_reset = QPushButton("既定に戻す")
        self.out_reset.clicked.connect(self._reset_output)
        self.inherit_box = QCheckBox("元の★を引き継ぐ")
        self.inherit_box.setToolTip("1 件が終わった時点の元の★を、その件で出来たファイルに付けます (更新日時は変えません)。")
        self.inherit_box.toggled.connect(self._inherit_changed)
        # ほかのアプリに出す (2026-11-22。PromptWordCrafter・MuseFactory・J-Rate のレシピの一覧に出す)
        self.apps_button = QPushButton()
        self.apps_button.setToolTip("このレシピを、ほかのアプリ (PromptWordCrafter・MuseFactory・J-Rate) のレシピの一覧に出します。"
                                    "出したアプリでは、ファイルを選んでレシピを押すと、PrismLab の画面を出さずに裏で生成します。")
        self.apps_menu = QMenu(self)
        self.apps_actions = {}
        for name in recipes.APPS:
            action = self.apps_menu.addAction(name)
            action.setCheckable(True)
            action.toggled.connect(lambda on, n=name: self._app_toggled(n, on))
            self.apps_actions[name] = action
        self.apps_button.setMenu(self.apps_menu)
        out_row.addWidget(self.out_label, 1)
        out_row.addWidget(self.apps_button)
        out_row.addWidget(self.inherit_box)
        out_row.addWidget(self.out_change)
        out_row.addWidget(self.out_reset)
        root.addLayout(out_row)

        # 台
        mrow = QHBoxLayout()
        mrow.setSpacing(8)
        self.machine_cards = [MachineCard() for _ in range(recipes.MAX_MACHINES)]
        for card in self.machine_cards:
            card.url = ""
            card.use_toggled.connect(lambda on, c=card: self._set_machine_on(c.url, on))
            mrow.addWidget(card, 1)
        self.machines_button = QPushButton("台の設定...")
        self.machines_button.clicked.connect(self._edit_machines)
        mrow.addWidget(self.machines_button)
        root.addLayout(mrow)

        # 開始・一時停止・中止
        crow = QHBoxLayout()
        self.start_button = QPushButton("▶ 開始")
        self.start_button.setStyleSheet(f"QPushButton {{ background: {theme.ACCENT}; color: black; font-weight: bold;"
                                        " padding: 6px 18px; } QPushButton:disabled { background: #3a3a3a; color: #777; }")
        self.start_button.clicked.connect(self._start)
        self.trial_button = QPushButton("▶ 1 枚で試す")
        self.trial_button.setToolTip("入口で選んだ 1 枚 (サムネールをクリック・画像をドラッグ。選ばなければ先頭) だけを、"
                                     "このレシピで通します。出力は 出力フォルダ\\試作 に入ります。")
        self.trial_button.clicked.connect(self._trial)
        self.pause_button = QPushButton("一時停止")
        self.pause_button.clicked.connect(self._toggle_pause)
        self.cancel_button = QPushButton("中止")
        self.cancel_button.clicked.connect(self._cancel)
        self.retry_button = QPushButton("失敗したものをやり直す")
        self.retry_button.clicked.connect(self._retry)
        # ★を待つ: ★を読み直す (戻ったとき・1 分ごとにも読む) / ★待ちを終える
        self.gate_recheck_button = QPushButton("★を読み直す")
        self.gate_recheck_button.setToolTip("★待ちのファイルの★を読み直し、付いたものを先へ進めます"
                                            " (PrismLab に戻ったときと、1 分ごとにも読みます)。")
        self.gate_recheck_button.clicked.connect(self._recheck_gates)
        self.gate_end_button = QPushButton("★待ちを終える...")
        self.gate_end_button.setToolTip("まだ★のないものを「★なし」で終えます (ここまでに出来たものは残します)。")
        self.gate_end_button.clicked.connect(self._end_gates)
        self._gate_timer = QTimer(self)
        self._gate_timer.setInterval(60_000)
        self._gate_timer.timeout.connect(self._recheck_gates)
        self._gate_checking = False
        self.summary = QLabel()
        self.summary.setStyleSheet("font-size: 13px;")
        for w in (self.start_button, self.trial_button, self.pause_button, self.cancel_button, self.retry_button,
                  self.gate_recheck_button, self.gate_end_button):
            crow.addWidget(w)
        self.gate_recheck_button.hide()
        self.gate_end_button.hide()
        crow.addSpacing(12)
        crow.addWidget(self.summary, 1)
        root.addLayout(crow)

        # 結果
        rhead = QHBoxLayout()
        self.results_title = QLabel("結果")
        self.results_title.setStyleSheet("font-weight: bold;")
        self.open_folder = QPushButton("出力フォルダを開く")
        self.open_folder.clicked.connect(self._open_output)
        self.open_jrate = QPushButton("J-Rate で開く")
        self.open_jrate.clicked.connect(self._open_in_jrate)
        rhead.addWidget(self.results_title)
        rhead.addStretch(1)
        rhead.addWidget(self.open_folder)
        rhead.addWidget(self.open_jrate)
        root.addLayout(rhead)
        self.results = QListWidget()
        self.results.setViewMode(QListView.ViewMode.IconMode)
        self.results.setIconSize(RESULT_ICON)
        self.results.setGridSize(QSize(RESULT_ICON.width() + 16, RESULT_ICON.height() + 34))
        self.results.setResizeMode(QListView.ResizeMode.Adjust)
        self.results.setMovement(QListView.Movement.Static)
        self.results.setWordWrap(True)
        self.results.setUniformItemSizes(True)
        self.results.setSelectionMode(QListView.SelectionMode.ExtendedSelection)  # Ctrl・Shift で複数
        self.results.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.results.customContextMenuRequested.connect(self._result_menu)
        self.results.itemDoubleClicked.connect(self._open_result)
        self.results.installEventFilter(self)  # Del で削除
        self._deleting = False
        # 乗せると大きく出す (失敗の項目は出さない)
        self.hover = HoverPreview(self.results, lambda index: "" if index.data(Qt.ItemDataRole.UserRole + 1)
                                  else index.data(Qt.ItemDataRole.UserRole), self.thumbs)
        self.results.currentItemChanged.connect(lambda *_: self._refresh_prompt())
        self.prompt_view = QTextBrowser()  # 選んだ結果 (試作なら今の件) の元・読み取った文・盛った文
        self.prompt_view.setFixedWidth(380)
        self.prompt_view.setOpenLinks(False)
        bottom = QHBoxLayout()
        bottom.addWidget(self.results, 1)
        bottom.addWidget(self.prompt_view)
        root.addLayout(bottom, 1)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(300)
        self._timer.timeout.connect(self.refresh)
        self.engine.changed.connect(self._schedule)
        self.engine.finished.connect(self._on_finished)

        self._build_recipe_chips()
        self.refresh(rebuild=True)

    # ------------------------------------------------------------------
    # 状態
    # ------------------------------------------------------------------
    @property
    def recipe(self) -> recipes.Recipe:
        return self.recipes[self.index]

    def running(self) -> bool:
        run = self.engine.run
        return self.engine.active and run is not None and run.unfinished > 0

    def run_recipe(self) -> Optional[recipes.Recipe]:
        """今の (最後の) 量産のレシピ。始めたときのものを覚えておき、なければ (続きから始めたときなど) 名前で探す。"""
        run = self.engine.run
        if run is None:
            return None
        if self._run_recipe is not None and any(r is self._run_recipe for r in self.recipes):
            return self._run_recipe
        self._run_recipe = next((r for r in self.recipes if r.name == run.name), None)
        return self._run_recipe

    def locked(self) -> bool:
        """見ているレシピが、動いている量産のものか (それだけは直せない。ほかのレシピは量産中も直せる)。"""
        return self.running() and self.run_recipe() is self.recipe

    def _schedule(self) -> None:
        if not self._timer.isActive():
            self._timer.start()

    def _save_recipes(self) -> None:
        try:
            recipes.save(self.recipes)
            recipes.set_last_index(self.index)
        except OSError as exc:
            self.status.emit(f"レシピを保存できません: {exc}")

    # ------------------------------------------------------------------
    # レシピの札
    # ------------------------------------------------------------------
    def _index_of(self, recipe) -> int:
        """レシピの番号 (同じ中身の複製と取り違えないよう、物そのもので探す)。"""
        return next((i for i, r in enumerate(self.recipes) if r is recipe), -1)

    def _regroup(self, keep=None, order=None) -> None:
        """同じまとまりを隣どうしに寄せ直し、選んでいるレシピ (keep) の番号を付け直す。
        order = まとまりの並び (渡さなければ今の並びで先に出た順)。"""
        keep = keep if keep is not None else self.recipe
        self.recipes = recipes.sort_by_group(self.recipes, order)
        self.index = max(self._index_of(keep), 0)

    def _group_label(self, group: str) -> str:
        return f"{group or recipes.OTHER_GROUP}  {sum(1 for r in self.recipes if r.group == group)}"

    def _build_recipe_chips(self) -> None:
        for row, buttons in ((self.group_row, None), (self.recipe_row, self.recipe_group)):
            while row.count():
                w = row.takeAt(0).widget()
                if w is None:
                    continue
                if buttons is not None and isinstance(w, QPushButton):
                    buttons.removeButton(w)
                w.hide()
                w.deleteLater()
        current = self.recipe.group
        self._group_pick[current] = self.recipe

        # まとまりのタブ
        self._group_tabs = {}
        has_groups = any(r.group for r in self.recipes)
        self.group_host.setVisible(has_groups)
        if has_groups:
            for g in recipes.group_order(self.recipes):
                tab = QPushButton(self._group_label(g))
                tab.setCheckable(True)
                tab.setChecked(g == current)
                tab.setStyleSheet(
                    "QPushButton { border: none; border-bottom: 2px solid transparent; border-radius: 0;"
                    f" padding: 3px 12px; background: transparent; color: {theme.TEXT_DIM}; }}"
                    f"QPushButton:hover {{ color: {theme.TEXT}; }}"
                    f"QPushButton:checked {{ border-bottom: 2px solid {theme.ACCENT}; color: {theme.TEXT}; font-weight: bold; }}")
                tab.setToolTip("レシピのまとまり。札の右クリックの「まとまり ▸」で入れる・外す。タブの右クリックで名前・並び")
                tab.clicked.connect(lambda _=False, name=g: self._select_group(name))
                tab.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
                tab.customContextMenuRequested.connect(
                    lambda pos, name=g, btn=tab: self._group_menu(name, btn.mapToGlobal(pos)))
                self._group_tabs[g] = tab
                self.group_row.addWidget(tab)
            self.group_row.addStretch(1)

        # 札 (選んでいるまとまりのものだけ)
        for i, r in enumerate(self.recipes):
            if r.group != current:
                continue
            b = QPushButton(r.name or "(名前なし)")
            b.setCheckable(True)
            b.setChecked(i == self.index)
            b.setStyleSheet("QPushButton { border-radius: 12px; padding: 4px 14px; }"
                            f"QPushButton:checked {{ background: {theme.ACCENT}; color: black; font-weight: bold; }}")
            b.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            b.customContextMenuRequested.connect(lambda pos, n=i, btn=b: self._recipe_menu(n, btn.mapToGlobal(pos)))
            self.recipe_group.addButton(b, i)
            self.recipe_row.addWidget(b)
        add = QPushButton("＋ レシピ")
        add.setStyleSheet("QPushButton { border-radius: 12px; padding: 4px 12px; border-style: dashed; }")
        if has_groups:
            add.setToolTip(f"このまとまり ({current or recipes.OTHER_GROUP}) にレシピを足します")
        add.clicked.connect(self._add_recipe)
        self.recipe_row.addWidget(add)

    def _select_recipe(self, index: int) -> None:
        regroup = self.recipes[index].group != self.recipe.group
        self.index = index
        self._group_pick[self.recipe.group] = self.recipe
        recipes.set_last_index(index)
        if regroup:
            self._build_recipe_chips()
        self.refresh(rebuild=True)

    def _select_group(self, group: str) -> None:
        """タブを押した: そのまとまりで最後に見ていたレシピ (なければ先頭) に切り替える。"""
        pick = self._group_pick.get(group)
        index = self._index_of(pick) if pick is not None and pick.group == group else -1
        if index < 0:
            index = next((i for i, r in enumerate(self.recipes) if r.group == group), -1)
        if index < 0 or index == self.index:
            self._build_recipe_chips()  # 押したタブの印を戻す
            return
        self._select_recipe(index)

    def _ask_group(self, title: str, current: str = "") -> Optional[str]:
        """まとまりの名前を聞く (「その他」と書いたら、まとまりなし)。取り消し = None。"""
        name = ask_text(self, title, "まとまりの名前 (PWC・IE・J-Rate など)", current)
        if name is None:
            return None
        return "" if name == recipes.OTHER_GROUP else name

    def _group_menu(self, group: str, pos) -> None:
        menu = QMenu(self)
        named = [g for g in recipes.group_order(self.recipes) if g]
        rename = menu.addAction("名前を変える...")
        rename.setEnabled(bool(group))
        menu.addSeparator()
        at = named.index(group) if group in named else -1
        left = menu.addAction("◀ 左へ")
        right = menu.addAction("右へ ▶")
        left.setEnabled(at > 0)
        right.setEnabled(0 <= at < len(named) - 1)  # 「その他」はいつも右端
        menu.addSeparator()
        ungroup = menu.addAction(f"まとまりを解く (レシピは「{recipes.OTHER_GROUP}」へ)")
        ungroup.setEnabled(bool(group))
        chosen = menu.exec(pos)
        keep = self.recipe
        if chosen is rename:
            name = self._ask_group("まとまりの名前を変える", group)
            if name is None or name == group:
                return
            for r in self.recipes:
                if r.group == group:
                    r.group = name  # ほかのまとまりと同じ名前なら 1 つにまとまる
        elif chosen in (left, right):
            j = at - 1 if chosen is left else at + 1
            named[at], named[j] = named[j], named[at]
            self.recipes = recipes.sort_by_group(self.recipes, named + [""])
        elif chosen is ungroup:
            for r in self.recipes:
                if r.group == group:
                    r.group = ""
        else:
            return
        self._regroup(keep)
        self._save_recipes()
        self._build_recipe_chips()
        self.refresh(rebuild=True)

    def _recipe_menu(self, index: int, pos) -> None:
        target = self.recipes[index]
        menu = QMenu(self)
        rename = menu.addAction("名前を変える...")
        dup = menu.addAction("複製する")
        group_menu = menu.addMenu("まとまり")
        group_actions = {}
        for g in recipes.group_order(self.recipes):
            if not g:
                continue
            a = group_menu.addAction(g)
            a.setCheckable(True)
            a.setChecked(g == target.group)
            group_actions[a] = g
        if group_actions:
            group_menu.addSeparator()
        new_group = group_menu.addAction("新しいまとまり...")
        no_group = group_menu.addAction(f"まとまりから外す (「{recipes.OTHER_GROUP}」へ)")
        no_group.setEnabled(bool(target.group))
        menu.addSeparator()
        # 左右は同じまとまりの中で (まとまりはいつも隣どうしに寄せてある)
        same = [k for k, r in enumerate(self.recipes) if r.group == target.group]
        at = same.index(index)
        left = menu.addAction("◀ 左へ")
        right = menu.addAction("右へ ▶")
        left.setEnabled(at > 0)
        right.setEnabled(at < len(same) - 1)
        menu.addSeparator()
        delete = menu.addAction("削除する...")
        busy = self.running() and self.run_recipe() is target
        delete.setEnabled(len(self.recipes) > 1 and not busy)  # 動いている量産のレシピは消さない
        if busy:
            delete.setText("削除する... (量産中は消せません)")
        chosen = menu.exec(pos)
        if chosen is rename:
            name = ask_text(self, "名前を変える", "レシピの名前", target.name)
            if name:
                target.name = name
        elif chosen is dup:
            copy = target.copy()  # まとまりも写る
            copy.name += " のコピー"
            self.recipes.insert(index + 1, copy)
            self.index = index + 1
        elif chosen in group_actions or chosen in (new_group, no_group):
            if chosen is new_group:
                name = self._ask_group("新しいまとまり")
                if name is None:
                    return
            else:
                name = group_actions.get(chosen, "")
            if name == target.group:
                return
            order = [g for g in recipes.group_order(self.recipes) if g]  # タブの並びは変えない (新しいものは「その他」の左)
            target.group = name
            self._regroup(order=order + ([name] if name and name not in order else []) + [""])  # 見ているレシピはそのまま (動かしたのが見ているものなら、タブもついていく)
            self.status.emit(f"「{target.name}」を「{name or recipes.OTHER_GROUP}」に入れました。")
        elif chosen in (left, right):
            j = same[at - 1] if chosen is left else same[at + 1]
            self.recipes[index], self.recipes[j] = self.recipes[j], self.recipes[index]
            self.index = j
        elif chosen is delete:
            if QMessageBox.question(self, "レシピの削除", f"レシピ「{target.name}」を削除しますか?",
                                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                    QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
                return
            keep = self.recipe
            if keep is target:  # 見ていたものを消した: 同じまとまりの隣 (なければ先頭)
                rest = [self.recipes[k] for k in same if k != index]
                keep = rest[min(at, len(rest) - 1)] if rest else next(r for r in self.recipes if r is not target)
            del self.recipes[index]
            self.index = self._index_of(keep)
        else:
            return
        self._save_recipes()
        self._build_recipe_chips()
        self.refresh(rebuild=True)

    def _add_recipe(self) -> None:
        name = ask_text(self, "レシピを足す", "レシピの名前")
        if not name:
            return
        new = recipes.Recipe(name, folder=self.recipe.folder, stages=[recipes.Stage(recipes.GEN)],
                             group=self.recipe.group)  # 見ているまとまりに足す
        self.recipes.append(new)
        self._regroup(new)
        self._save_recipes()
        self._build_recipe_chips()
        self.refresh(rebuild=True)

    # ------------------------------------------------------------------
    # ステップの並び
    # ------------------------------------------------------------------
    def _stage_views(self) -> list[dict]:
        """見せるステップ: 動いている量産のレシピなら始めたときの写し、そうでなければ選んでいるレシピ。"""
        if self.locked():
            views = []
            for s in self.engine.run.stages:
                v = {"type": s["type"], "bypass": bool(s.get("bypass"))}
                if s["type"] == recipes.READ:
                    v["detail"] = s["reader"].get("name", "")
                elif s["type"] == recipes.ENHANCE:
                    v["detail"] = s["enhancer"].get("name", "")
                elif s["type"] == recipes.TRANSLATE:
                    v["detail"] = "LLM: " + str(s.get("model") or "(エンハンサーと同じ)")
                elif s["type"] == recipes.TEXT:
                    v["detail"] = _front_mark(s.get("front")) + self._short(s.get("text", ""))
                    v["full"] = str(s.get("text") or "")
                    v["front"] = bool(s.get("front"))
                    v["title"] = _named_title(s)
                elif s["type"] == recipes.INSERT:
                    v["detail"] = _front_mark(s.get("front")) + "フォルダ: " + str(s.get("name", ""))
                    v["folder"] = str(s.get("folder") or "")
                    v["front"] = bool(s.get("front"))
                    v["title"] = _named_title(s)
                elif s["type"] in recipes.MEMO_TYPES:
                    v["detail"] = _memo_detail(s["type"], str(s.get("memo", "")), s.get("mode"))
                    v["memo"] = str(s.get("memo") or "")
                    v["append"] = s.get("mode") in (recipes.APPEND, recipes.PREPEND)
                    v["front"] = s.get("mode") == recipes.PREPEND
                elif s["type"] in recipes.PICK_TYPES:
                    image, folder = str(s.get("image") or ""), str(s.get("folder") or "")
                    v["detail"] = _ref_detail(s["type"], image, folder, bool(s.get("shuffle")))
                    v["refs"] = _ref_files(s["type"], image, folder)
                elif s["type"] in TOOL_TYPES:
                    rules = s.get("rules") or []
                    if s["type"] == recipes.SUB:
                        v["full"] = "\n".join(f"{r[0]} => {r[1]}" for r in rules
                                              if isinstance(r, (list, tuple)) and len(r) == 2) \
                            if isinstance(rules, list) else str(rules)
                    v["count"], v["stars"] = int(s.get("count") or 2), int(s.get("stars") or 1)
                    v["detail"] = _tool_detail(s["type"], v["count"], v["stars"],
                                               [tuple(r) for r in rules] if isinstance(rules, list) else
                                               recipes.parse_rules(str(rules)))
                else:
                    v["kind"] = s.get("kind", "")
                    v["detail"] = s.get("workflow_name", "")
                    v["seconds"] = s.get("seconds")
                views.append(v)
            return views
        views = []
        for s in self.recipe.stages:
            v = {"type": s.type, "bypass": s.bypass}
            if s.type == recipes.READ:
                v["detail"] = s.reader or "(読み取りプリセットを選んでください)"
            elif s.type == recipes.ENHANCE:
                v["detail"] = s.enhancer or "(エンハンサーを選んでください)"
            elif s.type == recipes.TRANSLATE:
                v["detail"] = "LLM: " + settings.enhance_model()
            elif s.type == recipes.TEXT:
                v["detail"] = _front_mark(s.front) + self._short(s.text) if s.text.strip() else "(足す文を入れてください)"
                v["full"] = s.text
                v["front"], v["title"] = s.front, _named_title(s)
            elif s.type == recipes.INSERT:
                if not s.folder:
                    v["detail"] = "(フォルダを選んでください)"
                else:
                    try:
                        nxt = inserts.peek(s.folder).describe()
                    except inserts.InsertError as exc:
                        nxt = str(exc)
                    v["detail"] = f"{_front_mark(s.front)}フォルダ: {Path(s.folder).name}\n{nxt}"
                v["folder"] = s.folder
                v["front"], v["title"] = s.front, _named_title(s)
            elif s.type in recipes.MEMO_TYPES:
                v["detail"] = _memo_detail(s.type, s.memo, s.mode)
                v["memo"] = s.memo
                v["append"] = s.mode in (recipes.APPEND, recipes.PREPEND)
                v["front"] = s.mode == recipes.PREPEND
            elif s.type in recipes.PICK_TYPES:
                v["detail"] = _ref_detail(s.type, s.image, s.folder, s.shuffle)
                v["refs"] = _ref_files(s.type, s.image, s.folder)
            elif s.type in TOOL_TYPES:
                v["count"], v["stars"] = s.count, s.stars
                v["detail"] = _tool_detail(s.type, s.count, s.stars, recipes.parse_rules(s.rules))
                if s.type == recipes.SUB:
                    v["full"] = s.rules
            else:
                v["kind"] = kinds.kind_of(s.workflow)
                names = [p.name for p in comfy.workflows() if s.workflow and p.value.lower() == s.workflow.lower()]
                v["detail"] = (names[0] if names and names[0] else Path(s.workflow).stem) if s.workflow \
                    else "(ワークフローを選んでください)"
                v["seconds"] = s.seconds
            views.append(v)
        return views

    @staticmethod
    def _short(text: str, limit: int = 60) -> str:
        one = " ".join(text.split())
        return one if len(one) <= limit else one[:limit] + "…"

    def _rebuild_flow(self) -> None:
        self.text_popup.hide()
        while self.flow.count():
            w = self.flow.takeAt(0).widget()
            if w is not None:
                w.hide()  # 消えるまでのあいだも描かない (新しい並びと重ならないように)
                w.deleteLater()
        run = self.engine.run
        showing = self.locked()
        source = str(run.recipe.get("source") or recipes.FOLDER) if showing else self.recipe.source
        picked = list(run.recipe.get("picked") or []) if showing else self.recipe.picked
        single, texts = source == recipes.SINGLE, recipes.entry_is_text(source, picked)
        is_picked = source == recipes.PICKED
        self.entry = Card()
        self.entry.title.setText("入口")
        if texts:
            self.entry.chip.set("文字", kinds.TEXT_COLOR)
            self.entry.set_color(kinds.TEXT_COLOR)
        else:
            self.entry.chip.set("画像", kinds.IMAGE_COLOR)
            self.entry.set_color(kinds.IMAGE_COLOR)
        self.entry.clicked.connect(self._entry_clicked)
        self.entry.bar.hide()
        self.entry.thumbs.clickable = True
        self.entry.thumbs.clicked.connect(self._pick_trial)
        self.entry.thumbs.setVisible(not texts)
        self.entry.accept_files((recipes.TEXT_EXT,) if texts else recipes.IMAGE_EXTS)
        self.entry.file_dropped.connect(self._entry_dropped)
        # 入口の種類: フォルダ / 1 枚 (LoadImage のように) / テキスト (T2I の量産)
        self.mode_folder = QPushButton("フォルダ")
        self.mode_single = QPushButton("1 枚")
        self.mode_texts = QPushButton("テキスト")
        self.mode_picked = QPushButton("選択")
        self.mode_picked.setToolTip("J-Rate・PromptWordCrafter の右クリック「PrismLab に送る」で送られたもの。"
                                    "[フォルダ] などを押すと前の入口に戻り、ここを押すとまた使えます。")
        self.mode_picked.setVisible(bool(picked))
        self.mode_single.setToolTip("フォルダのかわりに 1 枚の画像から始めます。回数ぶん同じ画像を通します"
                                    " (差し込み・盛るで 1 本ずつ変わります)。")
        self.mode_texts.setToolTip("フォルダの .txt を 1 つずつ元の文にして始めます (T2I の量産)。"
                                   "最初の生成は T2I か T2V にします。")
        mode_row = QHBoxLayout()
        mode_row.setSpacing(0)
        self.mode_group = QButtonGroup(self.entry)
        self.mode_buttons = (self.mode_folder, self.mode_single, self.mode_texts, self.mode_picked)
        for n, b in enumerate(self.mode_buttons):
            b.setCheckable(True)
            b.setStyleSheet("QPushButton { padding: 2px 8px; font-size: 11px; }")
            self.mode_group.addButton(b, n)
            mode_row.addWidget(b)
        mode_row.addStretch(1)
        self.mode_buttons[recipes.SOURCES.index(source)].setChecked(True)
        self.mode_group.idClicked.connect(self._source_changed)
        self.entry.box.insertLayout(1, mode_row)
        self.entry.setToolTip(
            "クリックかドラッグで画像を選ぶ。" if single else
            "J-Rate・PromptWordCrafter から送られたもの。サムネールのクリックかドラッグで、試す 1 つを選ぶ。" if is_picked else
            "クリックでテキストのフォルダを選ぶ。.txt のドラッグで、試す 1 つを選ぶ。" if texts else
            "クリックでフォルダを選ぶ。サムネールのクリックか画像のドラッグで、試す 1 枚を選ぶ。")
        self.repeat_box = QSpinBox()
        self.repeat_box.setRange(1, recipes.MAX_REPEAT)
        self.repeat_box.setPrefix("回数 ")
        self.repeat_box.setSuffix(" 回")
        self.repeat_box.setValue(self.recipe.repeat)
        self.repeat_box.setToolTip("同じ元を何回通すか。全部の元を 1 回ずつ → 2 回目 … の順に作ります。")
        self.repeat_box.valueChanged.connect(self._repeat_changed)
        self.entry.box.insertWidget(2, self.repeat_box)
        self.star_box = QComboBox()
        self.star_box.addItems(STAR_ITEMS)
        self.star_box.setCurrentIndex(self.recipe.min_stars)
        self.star_box.currentIndexChanged.connect(self._stars_changed)
        self.star_box.setVisible(source == recipes.FOLDER)
        self.entry.box.insertWidget(3, self.star_box)
        self.breakdown = QLabel()
        self.breakdown.setTextFormat(Qt.TextFormat.RichText)
        self.breakdown.setStyleSheet(f"background: transparent; color: {theme.TEXT_DIM}; font-size: 11px;")
        self.breakdown.setVisible(not single)  # テキストの入口では、先頭の文を出す
        self.breakdown.setWordWrap(True)
        self.entry.box.insertWidget(4, self.breakdown)
        self.rate_button = QPushButton("J-Rate で★を付ける")
        self.rate_button.setToolTip("入口のフォルダを J-Rate で開きます。★を付けて戻ると、ここに反映します。")
        self.rate_button.clicked.connect(self._rate_in_jrate)
        self.rate_button.setVisible(source == recipes.FOLDER)
        self.entry.box.insertWidget(self.entry.box.count() - 1, self.rate_button)
        self.unpick_button = QPushButton("× 外す")
        self.unpick_button.setToolTip("送られたものを入口から外し、前の入口 (フォルダなど) に戻します。")
        self.unpick_button.clicked.connect(self._unpick)
        self.unpick_button.setVisible(is_picked and not showing)
        self.entry.box.insertWidget(self.entry.box.count() - 1, self.unpick_button)
        self.flow.addWidget(self.entry)

        self.stage_cards: list[Card] = []
        self._ref_thumbs: dict = {}  # リファレンスの札の番号 → 並べる画像
        views = self._stage_views()
        running_now = showing
        problems = set() if running_now else recipes.line_problems(self.recipe.stages, text_entry=texts)
        prev_out = ("文字", kinds.TEXT_COLOR) if texts else ("画像", kinds.IMAGE_COLOR)
        has_text = texts  # テキストの入口は、初めから文がある
        lanes = flow.gaps(views, text_entry=texts)  # 矢印ごとに流れるもの (色の線で描く)
        names = ["入口"] + [v.get("title") or recipes.STAGE_TITLES[v["type"]] for v in views] + ["出力"]
        for i, v in enumerate(views):
            self.flow.addWidget(self._insert_arrow(i, lanes[i], f"{names[i]} → {names[i + 1]}"))
            card = Card()
            card.title.setText(v.get("title") or recipes.STAGE_TITLES[v["type"]])
            if v.get("title"):
                card.title.setToolTip(f"{recipes.STAGE_TITLES[v['type']]}: {v['title']}")
            card.detail.setText(v["detail"] + (f"  ・ {v['seconds']:g} 秒" if v.get("seconds") else ""))
            if v["bypass"]:
                # バイパス: 前のものをそのまま次に渡す (流れる中身は変えない)
                card.chip.set("バイパス", "#777777")
                card.io.setText(io_html(["そのまま通す"]))
                card.thumbs.hide()
            elif v["type"] == recipes.GEN:
                kind = v.get("kind", "")
                color = kinds.color_of(kind)
                card.chip.set(kind or "?", color)
                card.set_color(color)
                ins = [prev_out] if kind.startswith("I") or not kind else []
                if has_text:
                    ins.append(("文字", kinds.TEXT_COLOR))
                out = ("動画", color) if kinds.is_video(kind) else ("画像", color)
                parts = []
                for n, p in enumerate(ins):
                    if n:
                        parts.append("+")
                    parts.append(p)
                card.io.setText(io_html(parts + ["→", out]) if parts else io_html(["→", out]))
                prev_out, has_text = out, False
            elif v["type"] in recipes.MEMO_TYPES:
                # 覚える・思い出す: プロンプトをメモに写す・メモから戻す (中身は文字のまま)
                card.chip.set("メモ", kinds.TEXT_COLOR)
                card.set_color(kinds.TEXT_COLOR)
                src = ("文字", kinds.TEXT_COLOR)
                if v["type"] == recipes.SAVE:
                    card.io.setText(io_html([src, "→", src, "+", ("メモ", kinds.TEXT_COLOR)]))
                else:
                    memo = ("メモ", kinds.TEXT_COLOR)
                    joined = [memo, "+", src] if v.get("front") else [src, "+", memo]
                    card.io.setText(io_html((joined if has_text and v.get("append") else [memo]) + ["→", src]))
                    has_text = True
                card.thumbs.hide()
            elif v["type"] == recipes.FRAME:
                # 最後のコマ: 動画 → その最後のコマの画像
                card.chip.set("コマ", kinds.IMAGE_COLOR)
                card.set_color(kinds.IMAGE_COLOR)
                card.io.setText(io_html([prev_out, "→", ("画像", kinds.IMAGE_COLOR)]))
                prev_out = ("画像", kinds.IMAGE_COLOR)
                card.thumbs.hide()
            elif v["type"] in recipes.REF_TYPES:
                # リファレンス: 流れている画像・文はそのままで、あとの生成に渡すリファレンスを決める
                # 見出しは「リファレンス」だけ (「リファレンス (フォルダ)」は札の幅に入らない)。1 枚 / フォルダは印で見せる
                card.title.setText("リファレンス")
                card.title.setToolTip(recipes.STAGE_TITLES[v["type"]])
                card.chip.set("1 枚" if v["type"] == recipes.REF_IMAGE else "フォルダ", kinds.REF_COLOR)
                card.set_color(kinds.REF_COLOR)
                ref = ("リファレンス", kinds.REF_COLOR)
                card.io.setText(io_html([prev_out, "+", ref, "→", "あとの生成へ"]))
                if v["type"] == recipes.REF_IMAGE and not running_now:
                    # エクスプローラー・J-Rate から画像を落とすと、その画像にする
                    card.accept_files(recipes.IMAGE_EXTS)
                    card.file_dropped.connect(lambda path, n=i: self._set_ref_image(n, path))
                if not running_now and not v["bypass"] and not self._ref_used_later(i):
                    card.detail.setText(v["detail"] + "\n⚠ このリファレンスを使う生成がありません (あとの生成のワークフローに、"
                                        "タイトルが reference / face / 顔 などの LoadImage がない)")
                self._ref_thumbs[i] = v.get("refs") or []
            elif v["type"] in recipes.IMG_TYPES:
                # 画像: 流れている画像を差し替える (文はそのまま)
                card.title.setText("画像")
                card.title.setToolTip(recipes.STAGE_TITLES[v["type"]])
                card.chip.set("1 枚" if v["type"] == recipes.IMG_ONE else "フォルダ", kinds.IMAGE_COLOR)
                card.set_color(kinds.IMAGE_COLOR)
                image = ("画像", kinds.IMAGE_COLOR)
                card.io.setText(io_html([image, "→", image]))
                prev_out = image
                if v["type"] == recipes.IMG_ONE and not running_now:
                    card.accept_files(recipes.IMAGE_EXTS)  # 画像を落とすと、その画像にする
                    card.file_dropped.connect(lambda path, n=i: self._set_ref_image(n, path))
                self._ref_thumbs[i] = v.get("refs") or []
            elif v["type"] == recipes.SPLIT:
                # 増やす: そこから先を N 通りに (中身はそのまま)
                card.chip.set(f"×{v.get('count', 2)}", SPLIT_COLOR)
                card.set_color(SPLIT_COLOR)
                card.io.setText(io_html([prev_out] + (["+", ("文字", kinds.TEXT_COLOR)] if has_text else [])
                                        + ["→", f"{v.get('count', 2)} 本に分ける"]))
                card.thumbs.hide()
            elif v["type"] == recipes.GATE:
                # ★を待つ: J-Rate で★を付けたものだけ先へ
                card.chip.set("★" * int(v.get("stars", 1)), GATE_COLOR)
                card.set_color(GATE_COLOR)
                card.io.setText(io_html([prev_out, "→", (f"★{v.get('stars', 1)} 以上だけ", GATE_COLOR)]))
                card.thumbs.hide()
            elif v["type"] == recipes.SUB:
                # 置き換え: 文字 → 文字
                card.chip.set("文字", kinds.TEXT_COLOR)
                card.set_color(kinds.TEXT_COLOR)
                src = ("文字", kinds.TEXT_COLOR)
                card.io.setText(io_html([src, "→", ("置き換え", kinds.TEXT_COLOR), "→", src]))
                card.thumbs.hide()
            elif v["type"] in recipes.ADD_TYPES:
                # 追加テキスト・差し込み: 文を足す (盛るより前なら一緒に盛る)
                card.chip.set("文字", kinds.TEXT_COLOR)
                card.set_color(kinds.TEXT_COLOR)
                src = ("文字", kinds.TEXT_COLOR)
                add = ("足す", kinds.TEXT_COLOR)
                joined = ([add, "+", src] if v.get("front") else [src, "+", add]) if has_text else [add]
                card.io.setText(io_html(joined + ["→", src]))
                has_text = True
                card.thumbs.hide()
            else:
                card.chip.set("LLM", kinds.LLM_LINE)
                card.set_color(kinds.LLM_LINE)
                if v["type"] == recipes.READ:
                    card.io.setText(io_html([prev_out, "→", ("文字", kinds.TEXT_COLOR)]))
                elif v["type"] == recipes.TRANSLATE:
                    card.io.setText(io_html([("文字", kinds.TEXT_COLOR), "→", ("英語", kinds.TEXT_COLOR)]))
                else:
                    card.io.setText(io_html([("文字", kinds.TEXT_COLOR), "→", ("文字", kinds.TEXT_COLOR)]))
                has_text = True
                card.thumbs.hide()
            card.set_bypassed(v["bypass"])
            stages = self.recipe.stages
            if not running_now and not v["bypass"]:
                # その位置では動かないステップに、理由を赤で出す (始める前に気付けるように)
                reason = ""
                if ("needimage", i) in problems:
                    reason = "画像がまだありません (テキストの入口では、先に T2I / T2V の生成を置く)"
                elif ("enhance", i) in problems:
                    reason = "盛る元の文がありません (前に読み取り・追加テキスト・差し込みを置く)"
                elif ("memoempty", i) in problems:
                    reason = "覚える文がありません (前に読み取り・盛る・追加テキストなどを置く。生成のあとは空です)"
                elif ("norecall", i) in problems:
                    reason = "このメモを覚えていません (前に同じ名前の「覚える」を置く)"
                elif ("needvideo", i) in problems:
                    reason = "動画がまだありません (前に I2V などの動画の生成を置く)"
                elif ("notext", i) in problems:
                    reason = ("英語にする" if v["type"] == recipes.TRANSLATE else "置き換える") + \
                        "文がありません (前に読み取り・盛る・追加テキストなどを置く)"
                if reason:
                    card.io.setText(f'<span style="color:#ef5350; font-weight:bold">⚠ {html.escape(reason)}</span>')
                    card.io.setWordWrap(True)
                    card.set_color("#ef5350")
            # バイパスのスイッチはどのステップにも出す (2026-11-19 利用者の希望)。困るあとのステップは一緒に入る
            card.bypass.setVisible(not running_now and i < len(stages))
            if not running_now and i < len(stages):
                card.bypass.setToolTip(self._bypass_tip(i))
            card.bypass_toggled.connect(lambda on, n=i: self._set_bypass(n, on))
            if v["type"] in recipes.ADD_TYPES or v["type"] == recipes.SUB:
                # 札の上の端から処理バーの上までに乗せたときだけ (2026-11-21 利用者の希望)
                self.text_popup.attach(card, lambda v=v: self._popup_text(v),
                                       lambda c=card: c.bar.geometry().top())
            # 左へ・右へ (量産が動いているレシピでは並びを直せないので出さない)
            card.moves.setVisible(not running_now and i < len(stages))
            card.move_left.setEnabled(i > 0)
            card.move_right.setEnabled(i < len(stages) - 1)
            card.move_requested.connect(lambda step, n=i: QTimer.singleShot(0, lambda: self._move_stage(n, step)))
            card.clicked.connect(lambda n=i: self._edit_stage(n))
            card.menu_requested.connect(lambda pos, n=i: self._stage_menu(n, pos))
            self.flow.addWidget(card)
            self.stage_cards.append(card)

        self.flow.addWidget(self._insert_arrow(len(views), lanes[-1], f"{names[-2]} → 出力フォルダ"))
        self.add_stage = AddButton("＋ ステップ")
        self.add_stage.setMenu(self._type_menu(self.add_stage, lambda t: self._add_stage(t),
                                               paste_at=lambda: len(self.recipe.stages)))
        self.flow.addWidget(self.add_stage)
        self.flow.addStretch(1)

    def _type_menu(self, parent, chosen, skip: str = "", paste_at: Optional[Callable[[], int]] = None) -> QMenu:
        """ステップの種類 (読み取り・盛る・生成) を選ぶメニュー。paste_at があれば、コピーしたステップの貼り付けも
        (開くたびに作り直す。ほかのレシピでコピーしたものも出すため)。"""
        menu = QMenu(parent)
        menu.setToolTipsVisible(True)  # 項目に重ねると、種類の説明を横に出す

        def fill():
            menu.clear()
            if paste_at is not None and LineView.clipboard:
                # 並びを作り直すとこのメニューのボタンも消えるので、メニューを閉じてから行う
                paste = menu.addAction(self._paste_title(),
                                       lambda: QTimer.singleShot(0, lambda: self._paste_stages(paste_at())))
                paste.setToolTip("コピーしたステップを、設定ごとここに入れます"
                                 + (f" (「{LineView.clipboard_from}」から)。" if LineView.clipboard_from else "。"))
                menu.addSeparator()
            for t in recipes.STAGE_ORDER:
                if t != skip:
                    act = menu.addAction(recipes.STAGE_TITLES[t], lambda t=t: QTimer.singleShot(0, lambda: chosen(t)))
                    act.setToolTip(STAGE_HELP.get(t, ""))

        fill()
        menu.aboutToShow.connect(fill)
        return menu

    def _insert_arrow(self, index: int, gap=None, where: str = "") -> InsertArrow:
        """index の位置 (0 = 入口のすぐあと) にステップを挿入する矢印。gap (flow.Gap) があれば流れるものを線で描く。"""
        button = InsertArrow()
        if gap is not None:
            button.set_gap(gap, flow.describe(gap, where))
        button.set_menu(self._type_menu(button, lambda t: self._add_stage(t, index), paste_at=lambda: index))
        button.setEnabled(not self.locked())
        return button

    # ------------------------------------------------------------------
    # ステップのコピー・貼り付け (レシピをまたいで使える。アプリを閉じるまで覚える)
    # ------------------------------------------------------------------
    clipboard: list = []  # コピーしたステップ (Stage.to_dict() の並び)。量産ラインの画面で 1 つ
    clipboard_from = ""  # コピー元のレシピの名前 (知らせに出す)

    def _copy_stages(self, start: int, end: int) -> None:
        """start から end (含まない) のステップをコピーする。動いている量産のレシピからもコピーできる。"""
        stages = self.recipe.stages[start:end]
        if not stages:
            return
        LineView.clipboard = [s.to_dict() for s in stages]
        LineView.clipboard_from = self.recipe.name
        what = recipes.stage_title(stages[0]) if len(stages) == 1 else f"{len(stages)} 個のステップ"
        self.status.emit(f"「{what}」をコピーしました。ほかのレシピでも、ステップの右クリックか → の挿入で貼り付けられます。")

    @staticmethod
    def _paste_title() -> str:
        clip = LineView.clipboard
        if len(clip) == 1:
            return f"貼り付け: {recipes.stage_title(clip[0]) or '?'}"
        return f"貼り付け: {len(clip)} 個のステップ"

    def _paste_stages(self, index: int) -> None:
        """コピーしたステップを index の位置に入れる (中身はコピーしたときのまま。窓は出さない)。"""
        if self.locked() or not LineView.clipboard:
            return
        stages = self.recipe.stages
        index = max(0, min(index, len(stages)))
        pasted = [recipes.Stage.from_dict(dict(d)) for d in LineView.clipboard]
        recipes.renumber_bypass_groups(pasted, stages)
        stages[index:index] = pasted
        self._save_recipes()
        self.refresh(rebuild=True)
        self.status.emit(f"{len(pasted)} 個のステップを貼り付けました"
                         + (f" (「{LineView.clipboard_from}」から)" if LineView.clipboard_from != self.recipe.name else "") + "。")

    def _edit_stage(self, index: int) -> None:
        if self.locked():
            self._edit_running_stage(index)
            return
        stage = self.recipe.stages[index]
        edited = recipes.Stage.from_dict(stage.to_dict())
        if StageDialog(edited, self).exec():
            self.recipe.stages[index] = edited
            self._save_recipes()
            self.refresh(rebuild=True)

    def _edit_running_stage(self, index: int) -> None:
        """動いている量産のステップの中身を直す (2026-11-19 利用者の希望)。レシピと量産の写しの両方を直し、
        これからそのステップに来る件から効く。並び・種類・バイパス・ワークフローは量産が終わってから。"""
        run = self.engine.run
        stages = self.recipe.stages
        same = run is not None and len(run.stages) == len(stages) and all(
            snap["type"] == st.type and bool(snap.get("bypass")) == st.bypass
            and (st.type != recipes.GEN or str(snap.get("workflow", "")).lower() == st.workflow.lower())
            for snap, st in zip(run.stages, stages))
        if not same or index >= len(stages):
            self.status.emit("このレシピは量産を始めたときと並びが違うので、量産が終わるまで直せません。")
            return
        if stages[index].bypass:
            self.status.emit("バイパスしているステップは、量産が終わってから直してください。")
            return
        edited = recipes.Stage.from_dict(stages[index].to_dict())
        if not StageDialog(edited, self, running=True).exec():
            return
        new_stages = list(stages)
        new_stages[index] = edited
        problem = recipes.entry_problem(new_stages, self.recipe.texts)
        if problem:
            QMessageBox.warning(self, "ステップの設定", problem)
            return
        try:
            snap = engine.stage_snapshot(edited)
            self.engine.update_stage(index, snap)
        except Exception as exc:  # noqa: BLE001 - プリセットがない・差し込みのフォルダが読めないなど
            QMessageBox.warning(self, "ステップの設定", f"量産中のステップを直せませんでした。\n{exc}")
            return
        stages[index] = edited
        self._save_recipes()
        self.refresh(rebuild=True)
        self.status.emit(f"{index + 1} 番目のステップを直しました。これからそこに来る件から新しい中身で進みます。")

    def _stage_menu(self, index: int, pos) -> None:
        stages = self.recipe.stages
        if self.locked():
            # 動いている量産のレシピは中身 (設定) だけ直せる。並びは変えられないが、コピーはできる (ほかのレシピに貼るため)
            if index < len(stages):
                menu = QMenu(self)
                edit = menu.addAction("設定...")
                edit.setEnabled(not stages[index].bypass)
                menu.addSeparator()
                copy = menu.addAction("コピー")
                copy_right = menu.addAction("ここから右をまとめてコピー")
                copy_right.setEnabled(index < len(stages) - 1)
                chosen = menu.exec(pos)
                if chosen is edit:
                    self._edit_running_stage(index)
                elif chosen is copy:
                    self._copy_stages(index, index + 1)
                elif chosen is copy_right:
                    self._copy_stages(index, len(stages))
            return
        menu = QMenu(self)
        edit = menu.addAction("設定...")
        jobs = {}  # サブメニューの項目 → メニューを閉じてからやること

        def sub(title: str, job, skip: str = "") -> None:
            m = menu.addMenu(title)
            m.setToolTipsVisible(True)
            for t in recipes.STAGE_ORDER:
                if t != skip:
                    act = m.addAction(recipes.STAGE_TITLES[t])
                    act.setToolTip(STAGE_HELP.get(t, ""))
                    jobs[act] = lambda t=t: job(t)

        sub("種類を変える", lambda t: self._change_stage_type(index, t), skip=stages[index].type)
        menu.addSeparator()
        sub("左に挿入", lambda t: self._add_stage(t, index))
        sub("右に挿入", lambda t: self._add_stage(t, index + 1))
        duplicate = menu.addAction("複製")
        menu.addSeparator()
        copy = menu.addAction("コピー")
        copy_right = menu.addAction("ここから右をまとめてコピー")
        copy_right.setEnabled(index < len(stages) - 1)
        has_clip = bool(LineView.clipboard)
        clip_name = self._paste_title().replace("貼り付け: ", "") if has_clip else ""
        paste_left = menu.addAction(f"左に貼り付け ({clip_name})" if has_clip else "左に貼り付け")
        paste_right = menu.addAction(f"右に貼り付け ({clip_name})" if has_clip else "右に貼り付け")
        paste_left.setEnabled(has_clip)
        paste_right.setEnabled(has_clip)
        menu.addSeparator()
        bypass = menu.addAction("バイパス" + self._bypass_with_text(index))
        bypass.setCheckable(True)
        bypass.setChecked(stages[index].bypass)
        bypass.setEnabled(stages[index].bypass or recipes.can_bypass(stages, index, self.recipe.texts))
        if not bypass.isEnabled():
            bypass.setText("バイパス (入れられません)")
            bypass.setToolTip(recipes.bypass_reason(stages, index, self.recipe.texts))
            menu.setToolTipsVisible(True)
        menu.addSeparator()
        left = menu.addAction("◀ 左へ")
        right = menu.addAction("右へ ▶")
        left.setEnabled(index > 0)
        right.setEnabled(index < len(stages) - 1)
        menu.addSeparator()
        delete = menu.addAction("このステップを削除")
        chosen = menu.exec(pos)
        if chosen is edit:
            self._edit_stage(index)
            return
        if chosen in jobs:
            jobs[chosen]()
            return
        if chosen is bypass:
            self._set_bypass(index, bypass.isChecked())
            return
        if chosen is copy:
            self._copy_stages(index, index + 1)
            return
        if chosen is copy_right:
            self._copy_stages(index, len(stages))
            return
        if chosen in (paste_left, paste_right):
            self._paste_stages(index if chosen is paste_left else index + 1)
            return
        if chosen in (left, right):
            self._move_stage(index, -1 if chosen is left else 1)
            return
        if chosen is duplicate:
            copy_stage = recipes.Stage.from_dict(stages[index].to_dict())
            copy_stage.bypass_group = ""  # 複製はまとまりに入れない (1 つだけのバイパスにする)
            stages.insert(index + 1, copy_stage)
        elif chosen is delete:
            del stages[index]
        else:
            return
        self._save_recipes()
        self.refresh(rebuild=True)

    @staticmethod
    def _popup_text(v: dict) -> tuple:
        """文のステップの札に乗せたときに出す (見出し, 文)。差し込みは次に使う .txt の中身。"""
        if v["type"] == recipes.INSERT:
            folder = v.get("folder") or ""
            if not folder:
                return "", ""
            nxt = inserts.peek(folder).next_file
            return f"差し込み — 次に使う: {nxt.name}", nxt.read_text(encoding="utf-8-sig", errors="replace")
        if v["type"] == recipes.SUB:
            return "置き換えの決まり", v.get("full") or ""
        return recipes.STAGE_TITLES[v["type"]] + (f" — {v['title']}" if v.get("title") else ""), v.get("full") or ""

    def _move_stage(self, index: int, step: int) -> None:
        """index 番目のステップを左 (-1) か右 (+1) の隣と入れ替える (札の ◀ ▶ と右クリックの「左へ」「右へ」)。"""
        stages = self.recipe.stages
        j = index + step
        if self.locked() or not (0 <= index < len(stages)) or not (0 <= j < len(stages)):
            return
        stages[index], stages[j] = stages[j], stages[index]
        self._save_recipes()
        self.refresh(rebuild=True)

    def _others_text(self, indexes: list, index: int) -> str:
        """一緒に入る・戻るステップの名前 (「盛る」「置き換え」)。"""
        return "・".join(f"「{recipes.stage_title(self.recipe.stages[j])}」" for j in indexes if j != index)

    def _bypass_with_text(self, index: int) -> str:
        """右クリックの「バイパス」に添える、一緒に入る・戻るもの。"""
        stages = self.recipe.stages
        if stages[index].bypass:
            members = recipes.bypass_members(stages, index)
            return f" (外すと {self._others_text(members, index)} も戻す)" if len(members) > 1 else ""
        group = recipes.bypass_set(stages, index, self.recipe.texts) or [index]
        return f" ({self._others_text(group, index)} も一緒に)" if len(group) > 1 else ""

    def _bypass_tip(self, index: int) -> str:
        """↷ に乗せたときの説明。"""
        stages = self.recipe.stages
        tip = "バイパス: このステップを飛ばして、前のものをそのまま次に渡します"
        if stages[index].bypass:
            members = recipes.bypass_members(stages, index)
            if len(members) > 1:
                tip += f"\n{self._others_text(members, index)} と一緒に入れました。外すと一緒に戻ります。"
            return tip
        group = recipes.bypass_set(stages, index, self.recipe.texts)
        if group is None:
            return tip + "\n" + recipes.bypass_reason(stages, index, self.recipe.texts)
        if len(group) > 1:
            tip += f"\nこれがないと動かない {self._others_text(group, index)} も一緒にバイパスにします。"
        return tip

    def _set_bypass(self, index: int, on: bool) -> None:
        """バイパスを入れる・切る。入れるときは、外すと動かなくなるあとのステップも一緒に入れる (2026-11-19 利用者の希望)。
        一緒に入れたものは、どれかを切るとまとめて戻す。生成や画像がなくなるなら入れない。"""
        stages = self.recipe.stages
        if self.locked() or index >= len(stages) or stages[index].bypass == on:
            return
        if on:
            group = recipes.bypass_set(stages, index, self.recipe.texts)
            if group is None:
                self.status.emit(recipes.bypass_reason(stages, index, self.recipe.texts))
                QTimer.singleShot(0, lambda: self.refresh(rebuild=True))  # スイッチを戻す
                return
            mark = recipes.new_bypass_group(stages) if len(group) > 1 else ""
            for j in group:
                stages[j].bypass, stages[j].bypass_group = True, mark
            if len(group) > 1:
                self.status.emit(f"{self._others_text(group, index)} も一緒にバイパスにしました "
                                 f"(「{recipes.STAGE_TITLES[stages[index].type]}」がないと動かないため)。")
        else:
            members = recipes.bypass_members(stages, index)
            for j in members:
                stages[j].bypass, stages[j].bypass_group = False, ""
            if len(members) > 1:
                self.status.emit(f"一緒にバイパスにしていた {self._others_text(members, index)} も戻しました。")
        self._save_recipes()
        # 押したスイッチのカードも作り直すので、信号から抜けてから行う
        QTimer.singleShot(0, lambda: self.refresh(rebuild=True))

    def _ref_used_later(self, index: int) -> bool:
        """index 番目のリファレンスのあとに、リファレンスの入口のある生成があるか (次のリファレンスまで。バイパスは数えない)。"""
        for s in self.recipe.stages[index + 1:]:
            if s.bypass:
                continue
            if s.type in recipes.REF_TYPES:
                return False
            if s.type == recipes.GEN and (not s.workflow or kinds.has_reference(s.workflow)):
                return True  # ワークフローが未選択なら、まだ言わない
        return False

    def _set_ref_image(self, index: int, path: str) -> None:
        """リファレンス (1 枚)・画像 (1 枚) の札に落とした画像を、そのステップの画像にする。"""
        if self.locked() or index >= len(self.recipe.stages):
            return
        stage = self.recipe.stages[index]
        stage.image = str(Path(path))
        self._save_recipes()
        what = "リファレンスの画像" if stage.type in recipes.REF_TYPES else "画像のステップ"
        self.status.emit(f"{what}を {Path(path).name} にしました。")
        QTimer.singleShot(0, lambda: self.refresh(rebuild=True))  # 落とした札も作り直すので、信号から抜けてから

    def _add_stage(self, stage_type: str, index: Optional[int] = None) -> None:
        """ステップを足す (index なしは末尾)。設定の窓で OK したときだけ入れる。"""
        if self.locked():
            return
        stage = recipes.Stage(stage_type)
        if StageDialog(stage, self).exec():
            stages = self.recipe.stages
            stages.insert(len(stages) if index is None else min(index, len(stages)), stage)
            self._save_recipes()
            self.refresh(rebuild=True)

    def _change_stage_type(self, index: int, stage_type: str) -> None:
        """ステップの種類を変える。新しい種類の設定の窓で OK したときだけ置き換える (位置はそのまま)。"""
        if self.locked():
            return
        stage = recipes.Stage(stage_type)
        if StageDialog(stage, self).exec():
            self.recipe.stages[index] = stage
            self._save_recipes()
            self.refresh(rebuild=True)

    # ------------------------------------------------------------------
    # 入口・出力
    # ------------------------------------------------------------------
    def _choose_folder(self) -> None:
        if self.locked():
            return
        folder = QFileDialog.getExistingDirectory(self, "入口のフォルダを選ぶ", self.recipe.folder or "")
        if folder:
            self.recipe.folder = str(Path(folder))
            self.trial_source = None
            self._save_recipes()
            self.refresh(rebuild=True)

    def _entry_clicked(self) -> None:
        if self.recipe.is_picked:
            self.status.emit("入口は送られたものです。フォルダを選ぶときは [フォルダ] に切り替えてください。")
        elif self.recipe.single:
            self._choose_image()
        else:
            self._choose_folder()

    def _app_toggled(self, name: str, on: bool) -> None:
        apps = [a for a in recipes.APPS if (a == name and on) or (a != name and a in self.recipe.apps)]
        if apps != self.recipe.apps:
            self.recipe.apps = apps
            self._save_recipes()
            self._show_apps()
            shown = "・".join(recipes.APP_SHORT[a] for a in apps)
            self.status.emit(f"「{self.recipe.name}」を出すアプリ: {shown or 'なし'}")

    def _show_apps(self) -> None:
        apps = self.recipe.apps
        self.apps_button.setText("ほかのアプリに出す: " + (" · ".join(recipes.APP_SHORT[a] for a in apps) if apps else "なし"))
        for name, action in self.apps_actions.items():
            action.blockSignals(True)
            action.setChecked(name in apps)
            action.blockSignals(False)

    def _inherit_changed(self, on: bool) -> None:
        if self.recipe.inherit != on:
            self.recipe.inherit = on
            self._save_recipes()

    def _entry_dropped(self, path: str) -> None:
        if self.recipe.single:
            self._set_image(path)
        else:
            self._drop_trial(path)

    def _source_changed(self, mode: int) -> None:
        if self.locked():
            return
        source = recipes.SOURCES[mode] if 0 <= mode < len(recipes.SOURCES) else recipes.FOLDER
        if source == recipes.PICKED and not self.recipe.picked:
            return
        if source != self.recipe.source:
            if source == recipes.PICKED:
                self.recipe.picked_back = self.recipe.source
            self.recipe.source = source
            self.trial_source = None
            self._save_recipes()
            QTimer.singleShot(0, lambda: self.refresh(rebuild=True))  # 押したボタンも作り直すので、抜けてから

    def _unpick(self) -> None:
        """送られたものを外して、前の入口に戻す。"""
        if self.locked():
            return
        r = self.recipe
        r.picked, r.picked_from = [], ""
        r.source, r.picked_back = r.picked_back or recipes.FOLDER, ""
        self.trial_source = None
        self._save_recipes()
        self.status.emit("送られたものを入口から外しました。")
        QTimer.singleShot(0, lambda: self.refresh(rebuild=True))

    def receive_picked(self, files: list, sender: str, name: str = "", index: int = -1) -> str:
        """ほかのアプリから送られたファイルを、そのレシピの入口にする (spec 5 章)。入れられなければ理由を返す。"""
        target = -1
        if 0 <= index < len(self.recipes) and self.recipes[index].name == name:
            target = index
        else:
            target = next((i for i, r in enumerate(self.recipes) if r.name == name), -1)
        if target < 0:
            return f"レシピ「{name}」が見つかりません (名前を変えたか、消したかもしれません)。"
        if self.running() and self.run_recipe() is self.recipes[target]:
            return (f"レシピ「{name}」で量産が動いているので、入口に入れられません。"
                    "終わるか中止してから送るか、ほかのレシピに送ってください。")
        paths = [str(Path(f)) for f in files if f]
        found = [f for f in paths if Path(f).is_file()]
        if not found:
            return "送られたファイルが見つかりません。"
        texts = [f for f in found if f.lower().endswith(recipes.TEXT_EXT)]
        images = [f for f in found if f.lower().endswith(recipes.IMAGE_EXTS)]
        if texts and images or not (texts or images):
            return "送れるのは、画像だけか、テキスト (.txt) だけです。"
        r = self.recipes[target]
        r.picked, r.picked_from = texts or images, sender or "ほかのアプリ"
        if r.source != recipes.PICKED:
            r.picked_back = r.source
        r.source = recipes.PICKED
        self.index = target
        self.trial_source = None
        self._save_recipes()
        button = self.recipe_group.button(target)
        if button is not None:
            button.setChecked(True)
            self._group_pick[r.group] = r
        else:
            self._build_recipe_chips()  # ほかのまとまりのレシピ: そのタブに切り替える
        self.refresh(rebuild=True)
        unit = "件" if texts else "枚"
        missing = len(paths) - len(found)
        note = f" (見つからない {missing})" if missing else ""
        self.status.emit(f"{r.picked_from} から {len(r.picked)} {unit}を「{r.name}」の入口に入れました{note}。"
                         "確かめてから「開始」を押してください。")
        return ""

    def _choose_image(self) -> None:
        if self.locked():
            return
        start = self.recipe.image or self.recipe.folder or ""
        exts = " ".join("*" + e for e in recipes.IMAGE_EXTS)
        path, _ = QFileDialog.getOpenFileName(self, "入口の画像を選ぶ", start, f"画像 ({exts})")
        if path:
            self._set_image(path)

    def _set_image(self, path: str) -> None:
        if self.locked():
            return
        self.recipe.image = str(Path(path))
        self._save_recipes()
        self.status.emit(f"入口の画像: {Path(path).name}")
        self.refresh()

    def _repeat_changed(self, value: int) -> None:
        if self.locked():
            return
        self.recipe.repeat = value
        self._save_recipes()
        self.refresh()

    def _entry_sources(self) -> list:
        """入口の対象 (読んである★で絞ったもの。1 枚のときはその画像、テキストのときは .txt)。"""
        if self.recipe.single:
            return [Path(self.recipe.image)] if self.recipe.image and Path(self.recipe.image).is_file() else []
        if self.recipe.is_picked:
            return self.recipe.picked_files()
        if self.recipe.texts:
            if self._texts_key != self.recipe.folder:
                self._texts_key = self.recipe.folder
                self._text_files = recipes.list_texts(self.recipe.folder) if self.recipe.folder else []
            return list(self._text_files)
        return [p for p, s in self._scanned_files if s >= self.recipe.min_stars]

    def _trial_path(self) -> Optional[Path]:
        """1 枚で試す画像: 選んだもの、なければ入口の対象の先頭 (1 枚のときはその画像)。"""
        if self.recipe.single:
            sources = self._entry_sources()
            return sources[0] if sources else None
        if self.trial_source and Path(self.trial_source).is_file():
            return Path(self.trial_source)
        sources = self._entry_sources()
        return sources[0] if sources else None

    def _pick_trial(self, index: int) -> None:
        if self.locked() or self.recipe.single:
            return
        sources = self._entry_sources()
        if index < len(sources):
            self.trial_source = str(sources[index])
            self.status.emit(f"試す 1 枚: {sources[index].name}  (「▶ 1 枚で試す」で通します)")
            self.refresh()

    def _drop_trial(self, path: str) -> None:
        if self.locked():
            return
        self.trial_source = str(Path(path))
        what = "試す 1 つ" if self.recipe.texts else "試す 1 枚"
        self.status.emit(f"{what}: {Path(path).name}  (「▶ 1 枚で試す」で通します)")
        self.refresh()

    def _stars_changed(self, index: int) -> None:
        if self.locked():
            return
        self.recipe.min_stars = index
        self._save_recipes()
        self.refresh()

    def _change_output(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "出力フォルダを選ぶ", self.recipe.resolved_output_dir() or "")
        if folder:
            self.recipe.output_dir = str(Path(folder))
            self._save_recipes()
            self.refresh()

    def _reset_output(self) -> None:
        self.recipe.output_dir = ""
        self._save_recipes()
        self.refresh()

    def _on_scanned(self, token: int, folder: str, files: list) -> None:
        if not self._scan.current(token):
            return
        self._scanned_files, self._scanned_folder = files, folder
        for p, _s in files[:3]:
            self.thumbs.forget(str(p))  # J-Rate で差し替えたかもしれないので、先頭は描き直す
        self.refresh()

    def rescan(self) -> None:
        """入口の★を読み直す (J-Rate から戻ったときなど)。動いている量産の入口は変えない。
        ★待ちの件があれば、その★も読み直す。"""
        if self.engine.gated():
            self._recheck_gates()
        if self.locked():
            return
        self._input_key = None
        self._texts_key = None  # テキストの入口も読み直す (ほかのアプリで .txt を直したかもしれない)
        self._schedule()

    def _rate_in_jrate(self) -> None:
        folder = self.recipe.folder
        if not folder or not Path(folder).is_dir():
            self.status.emit("先に入口のフォルダを選んでください (入口のステップをクリック)。")
            return
        exe = find_jrate()
        if exe is None:
            self.status.emit("J-Rate が見つかりません (デスクトップの Application か、隣の J-Rate のフォルダに置いてください)。")
            return
        subprocess.Popen([str(exe), folder])  # noqa: S603 - 決まった EXE に入口のフォルダを渡す
        self.status.emit("J-Rate で★を付けたら、PrismLab に戻ってください。戻ったときに★を読み直します。")

    @staticmethod
    def _star_breakdown(files: list) -> str:
        """★の内訳 (ある★だけ): ★5 3 · ★4 12 · なし 40。"""
        counts = [0] * 6
        for _p, s in files:
            counts[s] += 1
        parts = [f'<span style="color:#e0a000">★{n}</span> {counts[n]}' for n in range(5, 0, -1) if counts[n]]
        if counts[0]:
            parts.append(f"なし {counts[0]}")
        return " · ".join(parts)

    def _output_dir(self) -> str:
        run = self.engine.run
        if run is not None and (self.locked() or not self.recipe.folder
                                or (run.trial and run.name == self.recipe.name)):  # 試作のあとは 試作 のフォルダ
            return run.output_dir
        return self.recipe.resolved_output_dir()

    # ------------------------------------------------------------------
    # 台
    # ------------------------------------------------------------------
    def _edit_machines(self) -> None:
        dialog = MachinesDialog(recipes.all_machines(), self, off=recipes.machines_off())
        if dialog.exec():
            urls = dialog.urls()
            recipes.set_machines(urls, dialog.off())
            self._apply_machines()

    def _set_machine_on(self, url: str, on: bool) -> None:
        """台の札の「使う」: 台を一時的に休ませる / 戻す。動いている件はその台で最後まで作る。"""
        if not url:
            return
        recipes.set_machine_on(url, on)
        self._apply_machines()
        name = next((p.name for p in comfy.connections() if p.value and p.value.rstrip("/").lower() == url.lower()
                     and p.name), url)
        self.status.emit(("台を使います: " if on else "台を休みにしました (新しい件は送りません): ") + name)

    def _apply_machines(self) -> None:
        """保存した台を、量産と物語 (候補づくり・バッチ) に知らせる。"""
        self.engine.set_machines(recipes.machines())
        self.machines_changed.emit()
        self.refresh()

    # ------------------------------------------------------------------
    # 開始・一時停止・中止
    # ------------------------------------------------------------------
    def _ready(self, title: str, need_folder: bool = True) -> bool:
        """始められるか (入口・ステップ・台)。だめなら理由を出して False。"""
        r = self.recipe
        if r.single:
            if not r.image or not Path(r.image).is_file():
                QMessageBox.warning(self, title, "入口の画像を選んでください (入口をクリックするか、画像をドラッグ)。")
                return False
        elif r.is_picked:
            if not r.picked_files():
                QMessageBox.warning(self, title, "送られたファイルが見つかりません (動かしたか、消したかもしれません)。")
                return False
        elif need_folder and (not r.folder or not Path(r.folder).is_dir()):
            QMessageBox.warning(self, title, "入口のフォルダを選んでください (入口のステップをクリック)。")
            return False
        if not r.stages:
            QMessageBox.warning(self, title, "ステップがありません。「＋ ステップ」で足してください。")
            return False
        problem = recipes.entry_problem(r.stages, r.texts)
        if problem:
            QMessageBox.warning(self, title, problem)
            return False
        if any(s.type == recipes.GEN and not s.bypass for s in r.stages) and not self.engine.machines:
            QMessageBox.warning(self, title, recipes.no_machines_reason())
            return False
        return True

    def _trial(self) -> None:
        """1 枚だけ通す (試作)。出力は (出力フォルダ)\\試作。量産の進み具合には数えない。"""
        if self.running():
            return
        r = self.recipe
        source = self._trial_path()
        if source is None:
            QMessageBox.warning(self, "1 枚で試す", "試す元がありません。入口のフォルダを選ぶか、画像 (テキストの入口なら .txt) を"
                                                  "入口にドラッグしてください。")
            return
        if not self._ready("1 枚で試す", need_folder=False):
            return
        if not r.resolved_output_dir():
            r = r.copy()
            r.output_dir = str(source.parent / r.name) if r.name else str(source.parent)  # 入口がないときは画像の隣
        try:
            run = engine.Run.create(r.copy(), [source], trial=True)
        except (ValueError, comfy.ComfyError) as exc:
            QMessageBox.warning(self, "1 枚で試す", str(exc))
            return
        self.engine.start(run)
        self._run_recipe = self.recipe
        self.results.setCurrentItem(None)
        self.status.emit(f"「{r.name}」を 1 枚で試しています: {source.name}")
        self.refresh(rebuild=True)

    def _start(self) -> None:
        r = self.recipe
        if not self._ready("開始"):
            return
        self.setCursor(Qt.CursorShape.WaitCursor)
        try:
            if r.single:
                base = [Path(r.image)]
            elif r.is_picked:
                base = r.picked_files()
            elif r.texts:
                base = recipes.list_texts(r.folder)
            else:
                base = recipes.list_images(r.folder)
                if r.min_stars:
                    base = [p for p in base if rating.get_stars(p) >= r.min_stars]
        finally:
            self.unsetCursor()
        if not base:
            QMessageBox.warning(self, "開始", "入口に中身のあるテキスト (.txt) がありません。" if r.texts else
                                "入口に対象の画像がありません (★の絞り込みも確かめてください)。")
            return
        files = recipes.repeat_sources(base, r.repeat)  # 回数ぶん (1 本ずつ別の件)
        try:
            run = engine.Run.create(r.copy(), files)
        except (ValueError, comfy.ComfyError) as exc:
            QMessageBox.warning(self, "開始", str(exc))
            return
        if len(files) > CONFIRM_COUNT:
            what = (f"{Path(r.image).name} を {len(files)} 回" if r.single else
                    f"{len(base)} 件 × {r.repeat} 回 = {len(files)} 件" if r.repeat > 1 else f"{len(files)} 件")
            if QMessageBox.question(self, "開始", f"{what}「{r.name}」で量産します。始めますか?",
                                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                    QMessageBox.StandardButton.Yes) != QMessageBox.StandardButton.Yes:
                return
        self.engine.start(run)
        self._run_recipe = self.recipe
        self.status.emit(f"「{r.name}」を始めました ({len(files)} 件)。")
        self.refresh(rebuild=True)

    def _toggle_pause(self) -> None:
        run = self.engine.run
        if run is None:
            return
        self.engine.set_paused(not run.paused)
        if run.paused:
            self.status.emit("一時停止しました。実行中のものが終わったら止まります。")

    def _cancel(self) -> None:
        run = self.engine.run
        if run is None or run.unfinished == 0:
            return
        running = sum(1 for i in run.items if i.state == engine.RUNNING)
        text = f"残りの {run.unfinished} 件を取り消しますか?"
        if running:
            text += f"\n実行中のもの ({running} 件) は ComfyUI に中断を送ります。"
        if QMessageBox.question(self, "中止", text, QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
            self.engine.cancel_all()

    def _recheck_gates(self) -> None:
        """★待ちの★を裏で読み直す (★を読むのに時間がかかるので)。"""
        if self._gate_checking or not self.engine.gated():
            return
        self._gate_checking = True
        eng = self.engine

        def work():
            try:
                moved = eng.recheck_gates()
            finally:
                self._gate_checking = False
            if moved:
                self.status.emit(f"★が付いた {moved} 件を先へ進めました。")

        threading.Thread(target=work, daemon=True, name="prismlab-gate").start()

    def _end_gates(self) -> None:
        n = self.engine.gated()
        if not n:
            return
        if QMessageBox.question(self, "★待ちを終える",
                                f"★待ちの {n} 件を、★なしで終えますか?\n(ここまでに出来たものは残します)",
                                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        ended = self.engine.end_gates()
        self.status.emit(f"★待ちの {ended} 件を終えました。")

    def _retry(self) -> None:
        n = self.engine.retry_failed()
        if n:
            self.status.emit(f"失敗した {n} 件をやり直します。")
            self.refresh(rebuild=True)

    def _on_finished(self) -> None:
        run = self.engine.run
        if run is None:
            return
        done = sum(1 for i in run.items if i.state == engine.DONE)
        failed = sum(1 for i in run.items if i.state == engine.FAILED)
        what = f"「{run.name}」の試作" if run.trial else f"「{run.name}」"
        self.status.emit(f"{what}が終わりました: 完了 {done} 件" + (f"・失敗 {failed} 件" if failed else ""))
        window = self.window()
        if not window.isActiveWindow():
            QApplication.alert(window)
        self.refresh(rebuild=True)

    # ------------------------------------------------------------------
    # 結果
    # ------------------------------------------------------------------
    def _open_output(self) -> None:
        folder = self._output_dir()
        if folder and Path(folder).is_dir():
            os.startfile(folder)  # noqa: S606 - 利用者のフォルダをエクスプローラーで開く
        else:
            self.status.emit("出力フォルダはまだありません (生成すると作ります)。")

    def _open_in_jrate(self) -> None:
        folder = self._output_dir()
        if not folder or not Path(folder).is_dir():
            self.status.emit("出力フォルダはまだありません (生成すると作ります)。")
            return
        exe = find_jrate()
        if exe is None:
            self.status.emit("J-Rate が見つかりません (デスクトップの Application か、隣の J-Rate のフォルダに置いてください)。")
            return
        subprocess.Popen([str(exe), folder])  # noqa: S603 - 決まった EXE に出力フォルダを渡す

    def _open_result(self, item: QListWidgetItem) -> None:
        path = item.data(Qt.ItemDataRole.UserRole)
        if path and Path(path).exists():
            os.startfile(path)  # noqa: S606

    def _result_menu(self, pos) -> None:
        clicked = self.results.itemAt(pos)
        if clicked is not None and not clicked.isSelected():
            self.results.setCurrentItem(clicked)  # 選んでいないものの上なら、それだけを選び直す
        picked = [i for i in self.results.selectedItems() if not i.data(Qt.ItemDataRole.UserRole + 1)]  # 失敗は除く
        # 一覧は新しい順なので、下から (作った順に) 足す
        picked.sort(key=self.results.row, reverse=True)
        images = [str(i.data(Qt.ItemDataRole.UserRole)) for i in picked
                  if str(i.data(Qt.ItemDataRole.UserRole)).lower().endswith(recipes.IMAGE_EXTS)]
        menu = QMenu(self)
        act = menu.addAction(f"物語の最後にシーンとして足す ({len(images)} 枚)" if images
                             else "物語の最後にシーンとして足す (画像を選んでください)")
        act.setEnabled(bool(images))
        act.triggered.connect(lambda: self.to_story.emit(images))
        batch = menu.addAction(f"物語のバッチに足す... ({len(images)} 枚)" if images
                               else "物語のバッチに足す... (画像を選んでください)")
        batch.setEnabled(bool(images))
        batch.setToolTip("1 枚ごとに 1 件のバッチとして、最初の画像にします (設定の窓を出します)。")
        batch.triggered.connect(lambda: self.to_batch.emit(images))
        menu.addSeparator()
        current = clicked if clicked is not None and not clicked.data(Qt.ItemDataRole.UserRole + 1) else None
        if current is not None:
            path = str(current.data(Qt.ItemDataRole.UserRole))
            menu.addAction("開く", lambda: self._open_result(current))
            menu.addAction("エクスプローラーで表示",
                           lambda: subprocess.Popen(["explorer", "/select,", os.path.normpath(path)]))  # noqa: S603,S607
        menu.addSeparator()
        count = len(picked)
        delete = menu.addAction(f"削除 (ごみ箱へ){f' ― {count} 個' if count > 1 else ''}\tDel")
        delete.setEnabled(count > 0)
        delete.triggered.connect(self._delete_results)
        menu.exec(self.results.mapToGlobal(pos))

    def eventFilter(self, obj, event) -> bool:
        if obj is self.results and event.type() == QEvent.Type.KeyPress and event.key() == Qt.Key.Key_Delete:
            if not event.isAutoRepeat():  # 押しっぱなしで続けて消えないように
                self._delete_results()
            return True
        return super().eventFilter(obj, event)

    def _delete_results(self) -> None:
        """選んでいる結果のファイルをごみ箱へ。1 つなら確かめず、複数かごみ箱に入らないときは確かめる (ui-standard 7 章)。
        失敗の項目は除く。終わっていない件が次のステップに渡すもの (★を待つ・続きの生成) は消さない。"""
        if self._deleting:
            return
        items = [i for i in self.results.selectedItems() if not i.data(Qt.ItemDataRole.UserRole + 1)]
        if not items:
            return
        busy = self.engine.in_use(str(i.data(Qt.ItemDataRole.UserRole)) for i in items)
        if busy:
            items = [i for i in items if str(i.data(Qt.ItemDataRole.UserRole)).lower() not in busy]
            if not items:
                self.status.emit("このあとのステップで使っているので削除できません (その件が終わってから消してください)。")
                return
        paths = [Path(str(i.data(Qt.ItemDataRole.UserRole))) for i in items]
        recyclable = all(recycle.can_recycle(p) for p in paths)
        if len(paths) > 1 or not recyclable or busy:
            what = paths[0].name if len(paths) == 1 else f"選んでいる {len(paths)} 個の結果"
            text = f"{what}を削除 (ごみ箱へ) しますか？"
            if not recyclable:
                text = f"{what}を削除しますか？\n\nこの場所のファイルはごみ箱に入らず、完全に消えます。"
            if busy:
                text += f"\n\nこのあとのステップで使っている {len(busy)} 個は残します。"
            if QMessageBox.question(self, "結果を削除", text) != QMessageBox.StandardButton.Yes:
                return
        first_row = min(self.results.row(i) for i in items)
        self.hover.hide()  # 再生している動画を手放してから消す
        self._deleting = True
        try:
            # サムネールを読んでいる途中なら、読み終わるのを待つ (読んでいるあいだは消せない)
            end = time.monotonic() + 3
            while (self.thumbs.busy(paths) or self.hover.big.busy(paths)) and time.monotonic() < end:
                QCoreApplication.processEvents(QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents, 50)
                time.sleep(0.02)
            error = recycle.send_to_recycle_bin(paths + recycle.captions(paths))
        finally:
            self._deleting = False
        gone = [p for p in paths if not p.exists()]
        for p in gone:
            self.thumbs.forget(str(p))
            self.hover.big.forget(str(p))
        self.engine.forget_outputs(gone)
        self.refresh(rebuild=True)
        if self.results.count():  # 消したあとは、同じ位置 (= 次) のものを選ぶ
            self.results.setCurrentRow(min(first_row, self.results.count() - 1))
        if error and len(gone) < len(paths):
            self.status.emit(f"{error}  (残った {len(paths) - len(gone)} 個 / ごみ箱へ移動 {len(gone)} 個)")
        elif len(gone) == 1:
            self.status.emit(f"ごみ箱へ移動しました: {gone[0].name}")
        elif gone:
            self.status.emit(f"ごみ箱へ移動しました: {len(gone)} 個")

    def _refresh_results(self, items: list) -> None:
        entries = []  # (時刻, パス, 失敗の理由, 見出し, (件の番号, 出力の番号 / -1 = 失敗))
        for n, it in enumerate(items):
            for k, o in enumerate(it.outputs):
                entries.append((float(o.get("time") or 0), str(o["path"]), "", Path(o["path"]).name, (n, k)))
            if it.state == engine.FAILED:
                entries.append((float("inf"), it.current, it.error, "失敗: " + Path(it.source).name, (n, -1)))
        entries.sort(key=lambda e: e[0], reverse=True)
        key = tuple((e[1], e[2]) for e in entries)
        if key != self._results_key:
            self._results_key = key
            self.hover.hide()  # 並びが変わるので、出している拡大は消す
            self.results.clear()
            self._result_items = {}
            for _t, path, error, title, where in entries:
                item = QListWidgetItem(title)
                item.setData(Qt.ItemDataRole.UserRole, path)
                item.setData(Qt.ItemDataRole.UserRole + 1, error)
                item.setData(Qt.ItemDataRole.UserRole + 2, where)
                item.setToolTip(error)  # 成功したものは、乗せると拡大に名前を出す (ツールチップと重ねない)
                # 初めから枠の大きさを持たせる。アイコンなし (サムネールの読み込み中) で測られると、
                # 文字だけの高さで並べたまま、あとから付いたアイコンの上の端しか見えなくなる
                item.setSizeHint(self.results.gridSize())
                if error:
                    item.setForeground(QColor("#ef5350"))
                self.results.addItem(item)
                self._result_items.setdefault(path.lower(), []).append(item)
        for path_key, list_items in self._result_items.items():
            for item in list_items:
                if not item.icon().isNull():
                    continue
                pix = self.thumbs.get(item.data(Qt.ItemDataRole.UserRole))
                if pix is not None:
                    item.setIcon(QIcon(self._framed(pix, bool(item.data(Qt.ItemDataRole.UserRole + 1)))))
        done = sum(len(i.outputs) for i in items)
        run = self.engine.run
        self.results_title.setText(f"結果 ({done})" + ("  ・ 試作" if run is not None and run.trial else ""))
        self._refresh_prompt()

    @staticmethod
    def _add_rows(adds, before: bool) -> list:
        """足した文 (追加テキスト・差し込み) の行。before = 盛る前に足したもの。"""
        rows = []
        for a in adds or []:
            if isinstance(a, dict) and bool(a.get("before")) == before:
                title = "差し込み " + str(a.get("file") or "") if a.get("file") else str(a.get("name") or "追加テキスト")
                rows.append(("＋ " + title + (" (盛る前)" if before else " (盛ったあと)"), str(a.get("text") or "")))
        return rows

    def _refresh_prompt(self) -> None:
        """プロンプトの欄: 選んだ結果の元・読み取った文・盛った文。試作で何も選んでいなければ、その件のいまの中身。"""
        run, items, _m = self.engine.snapshot()
        current = self.results.currentItem()
        where = current.data(Qt.ItemDataRole.UserRole + 2) if current is not None else None
        rows = []  # (見出し, 中身)
        head = ""
        if where is not None and where[0] < len(items):
            it = items[where[0]]
            if where[1] >= 0 and where[1] < len(it.outputs):
                o = it.outputs[where[1]]
                head = Path(o["path"]).name
                rows = [("元", Path(o.get("from") or it.source).name)]
                if o.get("reference"):
                    rows.append(("リファレンス", str(o["reference"])))
                rows.append(("読み取った文", o.get("prompt") or "(なし)"))
                rows += self._add_rows(o.get("adds"), before=True)
                rows.append(("盛った文", o.get("enhanced") or "(盛っていません)"))
                rows += self._add_rows(o.get("adds"), before=False)
                rows.append(("送った文 (ベースプロンプト込み)", o.get("sent") or "(ワークフローのプロンプトのまま)"))
            else:
                head = "失敗: " + Path(it.source).name
                rows = [("理由", it.error), ("読み取った文", it.text or "(まだ)"), ("盛った文", it.enhanced or "(まだ)")]
        elif run is not None and run.trial and items:
            it = items[0]
            head = "試作: " + Path(it.source).name
            rows = [("いま", engine.STATE_TEXT.get(it.state, it.state) + (f" ・ {it.status}" if it.status else ""))]
            if it.text is not None or it.enhanced is not None or it.adds:  # 途中 (生成の前)
                rows += [("読み取った文", it.text or "(なし)")] + self._add_rows(it.adds, before=True)
                rows += [("盛った文", it.enhanced or "(盛っていません)")] + self._add_rows(it.adds, before=False)
            elif it.outputs:  # 生成のあとは、生成に使ったもの
                o = it.outputs[-1]
                rows += [("読み取った文", o.get("prompt") or "(なし)")] + self._add_rows(o.get("adds"), before=True)
                rows += [("盛った文", o.get("enhanced") or "(盛っていません)")]
                rows += self._add_rows(o.get("adds"), before=False)
                rows.append(("送った文 (ベースプロンプト込み)", o.get("sent") or "(ワークフローのプロンプトのまま)"))
            if it.outputs:
                rows.append(("出来たもの", Path(it.outputs[-1]["path"]).name))
        if not head:
            text = (f'<p style="color:{theme.TEXT_DIM}">結果を選ぶと、その元の画像と、読み取った文・盛った文を出します。'
                    "<br><br>「▶ 1 枚で試す」で試作すると、途中の文もここに出ます。</p>")
        else:
            parts = [f"<p><b>{html.escape(head)}</b></p>"]
            for title, body in rows:
                parts.append(f'<p style="margin-top:8px"><span style="color:{theme.ACCENT}">{html.escape(title)}</span><br>'
                             f"{html.escape(str(body)).replace(chr(10), '<br>')}</p>")
            text = "".join(parts)
        if text != self._prompt_html:
            self._prompt_html = text
            self.prompt_view.setHtml(text)

    @staticmethod
    def _framed(pix: QPixmap, failed: bool) -> QPixmap:
        canvas = QPixmap(RESULT_ICON)
        canvas.fill(Qt.GlobalColor.transparent)
        p = QPainter(canvas)
        if not pix.isNull():
            scaled = pix.scaled(RESULT_ICON - QSize(8, 8), Qt.AspectRatioMode.KeepAspectRatio,
                                Qt.TransformationMode.SmoothTransformation)
            p.drawPixmap((RESULT_ICON.width() - scaled.width()) // 2, (RESULT_ICON.height() - scaled.height()) // 2,
                         scaled)
        if failed:
            p.setPen(QPen(QColor("#ef5350"), 4))
            p.drawRect(2, 2, RESULT_ICON.width() - 4, RESULT_ICON.height() - 4)
        p.end()
        return canvas

    # ------------------------------------------------------------------
    # 描き直し
    # ------------------------------------------------------------------
    def refresh(self, rebuild: bool = False) -> None:
        run, items, machines = self.engine.snapshot()
        running = self.running()
        showing = self.locked()  # 見ているレシピが動いている量産のもの (入口・ステップは量産の写しを見せる)
        mode = (showing, id(run), self.index)
        if rebuild or mode != getattr(self, "_flow_mode", None):
            self._flow_mode = mode
            self._rebuild_flow()
        # ステップに数を出すのは、動いている量産か、見ているレシピで最後に回した量産のとき
        show_stats = showing or (not running and run is not None and self.run_recipe() is self.recipe
                                 and len(run.stages) == len(self.stage_cards))
        busy_recipe = self.run_recipe() if running else None
        for b in self.recipe_group.buttons():
            r = self.recipes[self.recipe_group.id(b)] if self.recipe_group.id(b) < len(self.recipes) else None
            b.setText(("▶ " if r is not None and r is busy_recipe else "") + ((r.name if r else "") or "(名前なし)"))
            b.setToolTip("量産中 (このレシピはステップの中身だけ直せます。ほかのレシピは全部直せます)" if r is not None and r is busy_recipe else "")
        for g, tab in self._group_tabs.items():  # 動いている量産のレシピが入っているまとまりのタブにも ▶
            tab.setText(("▶ " if busy_recipe is not None and busy_recipe.group == g else "") + self._group_label(g))

        # 入口
        source = str(run.recipe.get("source") or recipes.FOLDER) if showing else self.recipe.source
        picked = list(run.recipe.get("picked") or []) if showing else self.recipe.picked
        picked_from = str(run.recipe.get("picked_from") or "") if showing else self.recipe.picked_from
        single, texts = source == recipes.SINGLE, recipes.entry_is_text(source, picked)
        is_picked = source == recipes.PICKED
        image = str(run.recipe.get("image", "")) if showing else self.recipe.image
        repeat = int(run.recipe.get("repeat") or 1) if showing else self.recipe.repeat
        if showing:
            sources = list(dict.fromkeys(Path(i.source) for i in items))  # 回数ぶん重なったものは 1 つに
            folder = str(run.recipe.get("folder", ""))
            stars = int(run.recipe.get("min_stars") or 0)
        elif single or texts or is_picked:
            folder, stars = self.recipe.folder, 0
            sources = self._entry_sources()
        else:
            folder, stars = self.recipe.folder, self.recipe.min_stars
            if folder != self._input_key:
                self._input_key = folder
                if folder != self._scanned_folder:
                    self._scanned_files, self._scanned_folder = [], None  # 別のフォルダ: 前のものは見せない
                self._scan.start(folder)
            sources = [p for p, s in self._scanned_files if s >= stars]
        if texts:
            first = sources[0] if sources else None
            self.breakdown.setText(("先頭: " + html.escape(self._short(inserts.read_file(first), 80))) if first else "")
        elif is_picked:
            missing = sum(1 for f in picked if not Path(f).is_file())
            self.breakdown.setText(f"見つからない {missing}" if missing and not showing else "")
        else:
            self.breakdown.setText("" if showing else self._star_breakdown(self._scanned_files))
        self.rate_button.setVisible(not showing and source == recipes.FOLDER)
        self._show_apps()
        self.inherit_box.blockSignals(True)
        self.inherit_box.setChecked(bool(run.recipe.get("inherit", True)) if showing else self.recipe.inherit)
        self.inherit_box.setEnabled(not showing and not texts)  # テキストには★がない
        self.inherit_box.blockSignals(False)
        self.star_box.blockSignals(True)
        self.star_box.setCurrentIndex(stars)
        self.star_box.setEnabled(not showing)
        self.star_box.blockSignals(False)
        for b in self.mode_buttons:
            b.setEnabled(not showing)
        self.repeat_box.setEnabled(not showing)
        times = f" × {repeat} 回" if repeat > 1 else ""
        if single:
            self.entry.detail.setText(("画像: " + Path(image).name) if image else "クリックして画像を選ぶ (ドラッグも)")
            self.entry.detail.setToolTip(image)
            trial = None
            count = f"{repeat} 回通す" if image else ""
            sources = sources[:1]
        elif is_picked:
            unit = "件" if texts else "枚"
            self.entry.detail.setText(f"{picked_from or 'ほかのアプリ'} から {len(picked)} {unit}")
            self.entry.detail.setToolTip("\n".join(Path(f).name for f in picked[:30])
                                         + ("\n…" if len(picked) > 30 else ""))
            trial = None if showing else self._trial_path()
            count = f"対象 {len(sources)} {unit}{times}"
            if trial is not None:
                count += f"  ・ {'試す 1 つ' if texts else '試す 1 枚'}: {trial.name}"
        else:
            what = "テキストのフォルダ" if texts else "フォルダ"
            self.entry.detail.setText((f"{what}: " + (Path(folder).name or folder)) if folder
                                      else f"クリックして{what}を選ぶ")
            self.entry.detail.setToolTip(folder)
            trial = None if showing else self._trial_path()
            count = (f"対象 {len(sources)} {'件' if texts else '枚'}{times}") if folder else ""
            if trial is not None:
                count += ("  ・ " if count else "") + f"{'試す 1 つ' if texts else '試す 1 枚'}: {trial.name}"
        self.entry.count.setText(count)
        self.entry.count.setToolTip(str(trial) if trial else "")
        shown = [Path(p) for p in sources[:3]]
        selected = shown.index(trial) if trial is not None and self.trial_source and trial in shown else -1
        self.entry.thumbs.clickable = not showing and not single and not texts
        self.entry.thumbs.set([self.thumbs.get(str(p)) for p in shown], selected=selected)

        # ステップ
        total = len(items)
        for i, card in enumerate(self.stage_cards):
            if i in self._ref_thumbs:
                card.thumbs.set([self.thumbs.get(p) for p in self._ref_thumbs[i]])
            if card._bypassed:
                card.bar.setValue(0)
                card.count.setText("バイパス中")
                continue
            if not show_stats:
                card.bar.setValue(0)
                card.count.setText("")
                if i not in self._ref_thumbs:
                    card.thumbs.set([])
                continue
            passed = sum(1 for it in items if it.stage > i)
            here = [it for it in items if it.stage == i and it.state == engine.RUNNING]
            failed = sum(1 for it in items if it.state == engine.FAILED and it.error_stage == i)
            gated = sum(1 for it in items if it.stage == i and it.state == engine.GATED)
            skipped = sum(1 for it in items if it.stage == i and it.state == engine.SKIPPED)
            card.bar.setMaximum(max(total, 1))
            card.bar.setValue(passed)
            text = f"済み {passed} / {total}"
            if here:
                text += f" ・ 実行中 {len(here)}"
            if gated:
                text += f" ・ ★待ち {gated}"
            if skipped:
                text += f" ・ ★なし {skipped}"
            if failed:
                text += f" ・ 失敗 {failed}"
            card.count.setText(text)
            stage_type = run.stages[i]["type"]
            card.set_color(card._color, bool(here))
            if stage_type == recipes.GEN:
                outs = sorted((o for it in items for o in it.outputs if o.get("stage") == i),
                              key=lambda o: float(o.get("time") or 0), reverse=True)[:3]
                # 動画は、いつものサムネール (始めのほう) の右下に最後のコマを小さく重ねる
                card.thumbs.set([self.thumbs.get(str(o["path"])) for o in outs],
                                insets=[self.thumbs.get(str(o["path"]), last=True)
                                        if Path(str(o["path"])).suffix.lower() in MOVIE_EXTS else None for o in outs])
            elif here:
                card.detail.setToolTip(here[0].status)

        # 出力
        out = self._output_dir()
        self.out_label.setText("出力フォルダ: " + (out or "(入口のフォルダを選ぶと決まります)")
                               + ("" if self.recipe.output_dir or showing else "   (既定: 入口のフォルダ\\レシピ名)"))
        self.out_change.setEnabled(not showing)
        self.out_reset.setEnabled(not showing and bool(self.recipe.output_dir))

        # 台 (台の設定で選んである台を全部。休みの台は「使う」のチェックを外して出す)
        configured = recipes.all_machines()
        off = recipes.machines_off()
        by_url = {m.url.lower(): m for m in machines}
        names = {p.value.rstrip("/").lower(): p.name for p in comfy.connections() if p.value} if configured else {}
        for n, card in enumerate(self.machine_cards):
            if n >= len(configured):
                card.setVisible(n == 0)
                if n == 0:
                    card.name.setText("台が決まっていません")
                    card.done.setText("")
                    card.status.setText("「台の設定...」で ComfyUI を選んでください")
                    card.set_down(False, "#777777")
                    card.set_on(True, shown=False)
                continue
            url = configured[n]
            card.url = url
            card.setVisible(True)
            m = by_url.get(url.lower())
            mine = [it for it in items if it.state == engine.RUNNING and it.at.lower() == url.lower()]
            if url.lower() in off or m is None:
                card.name.setText(names.get(url.lower()) or url)
                card.done.setText("")
                card.status.setText("休み (いまの件が終わったら止まります)" if mine else "休み (新しい件は送りません)")
                card.set_on(False)
                card.set_down(False, "#777777")
                continue
            card.set_on(True)
            card.name.setText(m.label)
            card.done.setText(f"{m.done} 件")
            if m.state == engine.DOWN:
                text = m.reason + "。残りはほかの台に回しています"
            elif m.state == engine.OFF:
                text = m.reason
            elif mine:
                text = " / ".join(f"{Path(it.current).name}: {it.status or '準備中'}" for it in mine)
            else:
                text = "待機中" if running else ""
                others = self.engine.pool.used(url)  # ほかのアプリの頼み・物語がこの台で動かしている数
                if others:
                    text = (text + " · " if text else "") + f"ほかの生成が {others} 件使っています"
            card.status.setText(text)
            card.set_down(m.state != engine.UP)

        # 開始・一時停止・中止
        self.start_button.setEnabled(not running)
        self.trial_button.setEnabled(not running)
        why = f"量産「{run.name}」が動いています。始めるのは終わってからです (このレシピは今も直せます)。" \
            if running and not showing and run is not None else ""
        self.start_button.setToolTip(why)
        self.trial_button.setToolTip(why or "今のレシピで 1 枚だけ通します。")
        self.pause_button.setEnabled(running)
        self.pause_button.setText("再開" if run is not None and run.paused else "一時停止")
        self.cancel_button.setEnabled(running)
        failed_total = sum(1 for it in items if it.state == engine.FAILED)
        self.retry_button.setEnabled(failed_total > 0 and not (run is not None and run.paused))
        self.retry_button.setVisible(failed_total > 0)
        gated_total = sum(1 for it in items if it.state == engine.GATED)
        self.gate_recheck_button.setVisible(gated_total > 0)
        self.gate_end_button.setVisible(gated_total > 0)
        if gated_total and not self._gate_timer.isActive():
            self._gate_timer.start()
        elif not gated_total:
            self._gate_timer.stop()
        self.summary.setText(self._summary(run, items, running))
        self._refresh_results(items)
        self._fit_flow()

    def _fit_flow(self) -> None:
        """ステップの並びの欄を、いちばん高い札 (入口の★の内訳などで伸びる) が全部見える高さにする。
        縦のスクロールバーは出さず、そのぶん結果の欄を下へ送る (2026-11-18 利用者の希望)。窓の高さの半分までにする。"""
        cards = [self.entry] + list(self.stage_cards) if getattr(self, "entry", None) is not None else []
        tallest = 0
        for card in cards:
            hint = card.heightForWidth(card.width() or CARD_WIDTH) if card.hasHeightForWidth() else -1
            tallest = max(tallest, hint if hint > 0 else card.sizeHint().height(), card.minimumSizeHint().height())
        bar = self.flow_scroll.horizontalScrollBar().sizeHint().height()
        margins = self.flow.contentsMargins()
        want = tallest + margins.top() + margins.bottom() + bar + 4
        limit = max(FLOW_MIN_HEIGHT, self.height() // 2) if self.height() > 0 else FLOW_MAX_HEIGHT
        height = max(FLOW_MIN_HEIGHT, min(want, limit, FLOW_MAX_HEIGHT))
        self.flow_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff if want <= height
                                                    else Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        if self.flow_scroll.height() != height:
            self.flow_scroll.setFixedHeight(height)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._fit_flow()

    def _summary(self, run, items, running: bool) -> str:
        if run is None:
            return ""
        total = len(items)
        done = sum(1 for i in items if i.state == engine.DONE)
        failed = sum(1 for i in items if i.state == engine.FAILED)
        canceled = sum(1 for i in items if i.state == engine.CANCELED)
        gated = sum(1 for i in items if i.state == engine.GATED)
        skipped = sum(1 for i in items if i.state == engine.SKIPPED)
        text = ("試作 " if run.trial else "") + f"「{run.name}」 完了 {done} / {total}"
        if gated:
            text += (f' ・ <span style="color:{GATE_COLOR}; font-weight:bold">★待ち {gated}</span>'
                     " (J-Rate で★を付けると先へ)")
        if skipped:
            text += f" ・ ★なし {skipped}"
        if failed:
            text += f" ・ 失敗 {failed}"
        if canceled:
            text += f" ・ 取り消し {canceled}"
        untranslated = sum(1 for i in items if i.warning)
        if untranslated:  # 「英語に」で LLM に繋がらず、訳さずに送った件 (止めずに進める。2026-11-23)
            text += (f' ・ <span style="color:#ff9800; font-weight:bold">英語にせず {untranslated}</span>'
                     " (LLM に繋がりません。訳さずに送りました)")
        if running:
            if run.paused:
                text = '<span style="color:#ff9800; font-weight:bold">一時停止中</span>  ' + text
            elif done and total - done - failed - canceled - gated - skipped > 0:
                left = total - done - failed - canceled - gated - skipped
                spent = time.time() - run.started
                text += f" ・ 残り 約 {_fmt_duration(spent / done * left)}"
            if self.engine.llm_status:
                text += f" ・ {self.engine.llm_status}"
        return text
