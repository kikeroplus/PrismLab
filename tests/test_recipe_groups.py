"""レシピのまとまり (上のタブ): 保存・寄せ方・タブと札の出し方。

    python -m unittest tests.test_recipe_groups
APPDATA を一時フォルダに差し替えるので、実際の設定には触れない。
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="prismlab_groups_"))
os.environ["APPDATA"] = str(TMP / "appdata")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(ROOT.parent / "GenCommon"))
sys.path.insert(0, str(ROOT))

from PySide6.QtWidgets import QApplication  # noqa: E402

from prismlab import engine, profile, recipes  # noqa: E402

app = QApplication.instance() or QApplication(sys.argv)
profile.setup()


def make(name, group=""):
    return recipes.Recipe(name, stages=[recipes.Stage(recipes.GEN)], group=group)


class GroupModelTest(unittest.TestCase):
    def test_round_trip(self):
        r = recipes.Recipe.from_dict(make("a", "PWC").to_dict())
        self.assertEqual(r.group, "PWC")
        self.assertEqual(recipes.Recipe.from_dict({"name": "old"}).group, "")  # 前の保存にはない

    def test_order_and_sort(self):
        rs = [make("x"), make("p1", "PWC"), make("i1", "IE"), make("p2", "PWC"), make("y")]
        self.assertEqual(recipes.group_order(rs), ["PWC", "IE", ""])
        self.assertEqual([r.name for r in recipes.sort_by_group(rs)], ["p1", "p2", "i1", "x", "y"])
        self.assertEqual([r.name for r in recipes.sort_by_group(rs, ["IE", "PWC", ""])], ["i1", "p1", "p2", "x", "y"])

    def test_published_has_group(self):
        r = make("p", "PWC")
        r.apps = [recipes.APPS[0]]
        self.assertEqual(recipes.published_entries([r])[0]["group"], "PWC")


class GroupViewTest(unittest.TestCase):
    def setUp(self):
        from prismlab.line_view import LineView
        recipes.save([make("x"), make("p1", "PWC"), make("i1", "IE"), make("p2", "PWC")])
        recipes.set_last_index(0)
        self.view = LineView(engine.Engine())

    def chips(self):
        return [b.text() for b in self.view.recipe_group.buttons()]

    def test_tabs_and_chips(self):
        v = self.view
        self.assertEqual([r.name for r in v.recipes], ["p1", "p2", "i1", "x"])  # 読んだときに寄せる
        self.assertEqual(list(v._group_tabs), ["PWC", "IE", ""])
        self.assertEqual(v.recipe.name, "p1")
        self.assertEqual(self.chips(), ["p1", "p2"])
        v._select_recipe(1)  # p2 を見てから
        v._select_group("IE")
        self.assertEqual(v.recipe.name, "i1")
        self.assertEqual(self.chips(), ["i1"])
        v._select_group("PWC")  # そのタブで最後に見ていたものに戻る
        self.assertEqual(v.recipe.name, "p2")
        self.assertTrue(v._group_tabs["PWC"].isChecked())

    def test_regroup_keeps_selection(self):
        v = self.view
        v._select_group("")
        x = v.recipe
        v.recipes[0].group = "IE"  # p1 を IE へ (タブの並び PWC・IE は変えない)
        v._regroup(order=["PWC", "IE", ""])
        self.assertIs(v.recipe, x)
        self.assertEqual([r.name for r in v.recipes], ["p2", "p1", "i1", "x"])

    def test_no_groups_hides_tabs(self):
        from prismlab.line_view import LineView
        recipes.save([make("a"), make("b")])
        v = LineView(engine.Engine())
        self.assertTrue(v.group_host.isHidden())
        self.assertEqual([b.text() for b in v.recipe_group.buttons()], ["a", "b"])


if __name__ == "__main__":
    unittest.main()
