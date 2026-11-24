"""ステップの並びを流れるもの (2026-11-21 利用者の希望: 初めて見た人にも、画像・動画・文字がどう流れ、どこで消えるか分かるように)。

ステップのあいだの矢印 1 つ (= すきま) ごとに、そこを通るもの (線) を数える。線の種類は 4 つで、上から決まった段に描く:
  画像・動画 (生成の種類の色。動画は太い線) / 文字 (プロンプト) / メモ (覚えた文。二重線) / リファレンス。
点線・破線はバイパス (使っていない) に見えるので使わない。
- new: その線は直前のステップで作られた・変えられた (始まりに丸を描く)
- cut: その線は次のステップに入らずに捨てられる (右の端に ✕。T2I に流れてきた画像、最後まで使われなかった文字)
- gone: 直前のステップで使い切った・このあと使わないもの (左の端に ✕ を描く。生成に送った文字など)

すきまは len(views) + 1 個: 0 番 = 入口のすぐあと、i 番 = i 番目のステップの前、最後 = 最後のステップのあと (出力へ)。
views は量産ラインの画面のステップの写し (type・bypass・kind・memo・count など)。
"""

from dataclasses import dataclass, field

from . import kinds, recipes

MEDIA, TEXT, MEMO, REF = "media", "text", "memo", "ref"
LANES = (MEDIA, TEXT, MEMO, REF)  # 描く段の順 (上から)
LANE_NAMES = {MEDIA: "画像・動画", TEXT: "文字", MEMO: "メモ", REF: "リファレンス"}


@dataclass
class Line:
    label: str  # 「画像」「動画」「文字」「メモ: メモ 1」「リファレンス」
    color: str
    style: str = "solid"  # solid / thick (動画) / double (メモ)
    new: bool = False  # 直前のステップで作られた・変えられた
    cut: bool = False  # 次のステップには入らず、ここで捨てられる
    why: str = ""  # 作られた・捨てられるわけ (矢印の説明に出す)


@dataclass
class Gap:
    lines: dict = field(default_factory=dict)  # 段 → Line
    gone: dict = field(default_factory=dict)  # 段 → 直前のステップで消えたわけ
    copies: int = 1  # 「増やす」で何本に分かれているか


def _media(kind: str) -> Line:
    color = kinds.color_of(kind)
    if kinds.is_video(kind):
        return Line("動画", color, "thick")
    return Line("画像", color)


def _text() -> Line:
    return Line("文字", kinds.TEXT_COLOR)


def gaps(views: list, text_entry: bool = False) -> list:
    """すきまごとの Gap の並び (len(views) + 1 個)。"""
    on = views
    # メモとリファレンスは、あとで使うあいだだけ描く (使い終わったら ✕)
    last_load = {}  # メモの名前 → 最後に思い出す番号
    last_gen = -1
    for i, v in enumerate(on):
        if v.get("bypass"):
            continue
        if v["type"] == recipes.LOAD:
            last_load[v.get("memo") or recipes.DEFAULT_MEMO] = i
        elif v["type"] == recipes.GEN:
            last_gen = i

    first = Gap()
    if text_entry:
        first.lines[TEXT] = Line("文字", kinds.TEXT_COLOR, new=True, why="入口のテキスト")
    else:
        first.lines[MEDIA] = Line("画像", kinds.IMAGE_COLOR, new=True, why="入口の画像")
    out = [first]
    media = first.lines.get(MEDIA)
    text = TEXT in first.lines
    memos: dict = {}  # 覚えたメモの名前 → まだ使うか
    ref = False
    copies = 1
    for i, v in enumerate(on):
        before = out[-1]
        t = v["type"]
        made: dict = {}  # 段 → 作られた・変えられたわけ
        gone: dict = {}  # 段 → このステップで消えたわけ
        had = set(before.lines)
        if not v.get("bypass"):
            if t == recipes.GEN:
                kind = v.get("kind", "")
                takes_image = kind.startswith("I") or not kind
                if media is not None and not takes_image:
                    # T2I / T2V は流れてきた画像・動画を使わない: このステップの前で捨てる
                    before.lines[MEDIA].cut = True
                    before.lines[MEDIA].why = f"{kind} は流れてきた{before.lines[MEDIA].label}を使わないので、ここで捨てます"
                media = _media(kind)
                made[MEDIA] = f"{kind or '生成'} で作った{media.label}" + \
                    (f" (流れてきた{before.lines[MEDIA].label}が元)" if MEDIA in before.lines and takes_image else "")
                if text:
                    gone[TEXT] = "文字は生成に送って使い切りました (このあとは空。続けて使うなら「覚える」→「思い出す」)"
                text = False
            elif t == recipes.FRAME:
                media = Line("画像", kinds.IMAGE_COLOR)
                made[MEDIA] = "動画の最後のコマの画像"
            elif t in recipes.IMG_TYPES:
                if MEDIA in before.lines:
                    # 流れてきた画像・動画は使わない: このステップの前で捨てる
                    before.lines[MEDIA].cut = True
                    before.lines[MEDIA].why = f"画像を差し替えるので、流れてきた{before.lines[MEDIA].label}はここで捨てます"
                media = Line("画像", kinds.IMAGE_COLOR)
                made[MEDIA] = ("決めた 1 枚の画像" if t == recipes.IMG_ONE else "フォルダから 1 件ごとに選んだ画像") + \
                    (" (文はそのまま)" if text else "")
            elif t == recipes.READ:
                made[TEXT] = "画像を読み取った文" + (" (前の文と入れ替え)" if text else "")
                text = True
            elif t == recipes.ENHANCE:
                if text:
                    made[TEXT] = "AI で盛った文"
            elif t == recipes.TRANSLATE:
                if text:
                    made[TEXT] = "英語に訳した文"
            elif t == recipes.SUB:
                if text:
                    made[TEXT] = "語を置き換えた文"
            elif t in recipes.ADD_TYPES:
                made[TEXT] = ("文を前に足した" if v.get("front") else "文を後ろに足した") if text else "足した文"
                text = True
            elif t == recipes.SAVE:
                name = v.get("memo") or recipes.DEFAULT_MEMO
                if text:
                    memos[name] = True
                    made[MEMO] = f"文を「{name}」に覚えた (生成を通っても残ります)"
            elif t == recipes.LOAD:
                name = v.get("memo") or recipes.DEFAULT_MEMO
                if name in memos:
                    made[TEXT] = f"「{name}」から戻した文" + ("" if not text else
                                                          (" (今の文に足した)" if v.get("append") else " (今の文と入れ替え)"))
                    text = True
            elif t in recipes.REF_TYPES:
                ref = True
                made[REF] = "このあとの生成に渡すリファレンス"
            elif t == recipes.SPLIT:
                copies *= max(2, int(v.get("count") or 2))
        # 使い終わったメモ・リファレンスは、ここで終わり
        for name in list(memos):
            if last_load.get(name, -1) <= i:
                memos.pop(name)
        if ref and last_gen <= i:
            ref = False
            if REF in had:
                gone[REF] = "このあとに生成がないので、リファレンスはここで終わり"
        if MEMO in had and not memos:
            gone[MEMO] = "このあとに思い出すがないので、メモはここで終わり"

        gap = Gap(copies=copies)
        if media is not None:
            line = Line(media.label, media.color, media.style)
            if MEDIA in made:
                line.new, line.why = True, made[MEDIA]
            gap.lines[MEDIA] = line
        if text:
            line = _text()
            if TEXT in made:
                line.new, line.why = True, made[TEXT]
            gap.lines[TEXT] = line
        if memos:
            label = "メモ: " + "・".join(memos)
            gap.lines[MEMO] = Line(label, kinds.TEXT_COLOR, "double", MEMO in made, why=made.get(MEMO, ""))
        if ref:
            gap.lines[REF] = Line("リファレンス", kinds.REF_COLOR, "solid", REF in made, why=made.get(REF, ""))
        gap.gone = gone
        out.append(gap)

    # 最後まで使われなかった文字は、出力には行かずに消える
    last = out[-1]
    if TEXT in last.lines:
        last.lines[TEXT].cut = True
        last.lines[TEXT].why = "この文はどの生成にも送られずに消えます"
    return out


def describe(gap: Gap, where: str = "") -> str:
    """矢印の説明 (ツールチップ)。"""
    rows = []
    if where:
        rows.append(where)
    if gap.lines:
        rows.append("ここを流れるもの" + (f" ({gap.copies} 本に分かれています)" if gap.copies > 1 else "") + ":")
        for lane in LANES:
            line = gap.lines.get(lane)
            if line is None:
                continue
            mark = "✕ " if line.cut else ("● " if line.new else "　")
            rows.append(f"{mark}{line.label}" + (f" — {line.why}" if line.why else ""))
    else:
        rows.append("ここを流れるものはありません")
    for lane in LANES:
        if lane in gap.gone:
            rows.append(f"✕ {LANE_NAMES[lane]}: {gap.gone[lane]}")
    return "\n".join(rows)
