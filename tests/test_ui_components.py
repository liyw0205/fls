import unittest

from fls_manager.ui.components import pagination_card


class UiComponentTests(unittest.TestCase):
    def test_pagination_card_returns_empty_for_single_page(self):
        self.assertEqual(
            pagination_card(1, 1, href_for=lambda page: f"/items?page={page}"),
            "",
        )

    def test_pagination_card_renders_links_and_escapes_href(self):
        html = pagination_card(
            3,
            6,
            href_for=lambda page: f"/items?page={page}&q=<tag>",
        )

        self.assertIn('<div class="card">', html)
        self.assertIn('<div class="help">', html)
        self.assertIn('<div class="action-row">', html)
        self.assertIn("第 <b>3</b> / <b>6</b> 页", html)
        self.assertIn('href="/items?page=2&amp;q=&lt;tag&gt;"', html)
        self.assertIn('href="/items?page=3&amp;q=&lt;tag&gt;"', html)
        self.assertIn('class="btn btn-primary"', html)
        self.assertIn(">上一页</a>", html)
        self.assertIn(">下一页</a>", html)

    def test_pagination_card_renders_disabled_edges_and_ellipsis(self):
        html = pagination_card(
            1,
            8,
            href_for=lambda page: f"/items?page={page}",
        )

        self.assertIn("cursor:not-allowed", html)
        self.assertIn(">上一页</span>", html)
        self.assertIn("...</span>", html)
        self.assertIn('href="/items?page=8"', html)

    def test_pagination_card_renders_onclick_buttons(self):
        html = pagination_card(
            2,
            3,
            onclick_for=lambda page: f"goPage({page})",
            page_label="任务第",
        )

        self.assertIn("任务第 <b>2</b> / <b>3</b> 页", html)
        self.assertIn('<button class="btn btn-gray" type="button" onclick="goPage(1)">上一页</button>', html)
        self.assertIn('<button class="btn btn-primary" type="button" onclick="goPage(2)">2</button>', html)
        self.assertIn('<button class="btn btn-gray" type="button" onclick="goPage(3)">下一页</button>', html)


if __name__ == "__main__":
    unittest.main()
