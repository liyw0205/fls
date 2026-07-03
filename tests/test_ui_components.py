import unittest

from fls_manager.ui.components import message_card, pagination_card


class UiComponentTests(unittest.TestCase):
    def test_message_card_returns_empty_for_empty_message(self):
        self.assertEqual(message_card(""), "")
        self.assertEqual(message_card(None), "")
        self.assertEqual(message_card("   \n\t  "), "")

    def test_message_card_renders_kind_color_and_strong_style(self):
        success_html = message_card("保存成功", "success", strong=True)
        error_html = message_card("保存失败", "error")
        info_html = message_card("普通提示")

        self.assertIn('<div class="card">', success_html)
        self.assertIn('style="color:#18a058;font-weight:800;"', success_html)
        self.assertIn("保存成功", success_html)
        self.assertIn('style="color:#dc2626;"', error_html)
        self.assertIn("保存失败", error_html)
        self.assertIn('style="color:#6b7280;"', info_html)
        self.assertIn("普通提示", info_html)
        self.assertNotIn("font-weight:800;", error_html)
        self.assertNotIn("font-weight:800;", info_html)

    def test_message_card_falls_back_to_info_for_unknown_kind(self):
        html = message_card("未知类型", "warning")

        self.assertIn('style="color:#6b7280;"', html)
        self.assertIn("未知类型", html)

    def test_message_card_renders_optional_title_and_escapes_it(self):
        html = message_card("提示", title="<结果>")

        self.assertIn('<div class="card-title">&lt;结果&gt;</div>', html)
        self.assertIn("提示", html)
        self.assertNotIn("<结果>", html)

    def test_message_card_escapes_message_html(self):
        html = message_card('<script data-x="1">a & b</script>', "success")

        self.assertIn("&lt;script data-x=&quot;1&quot;&gt;a &amp; b&lt;/script&gt;", html)
        self.assertNotIn("<script>", html)

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
