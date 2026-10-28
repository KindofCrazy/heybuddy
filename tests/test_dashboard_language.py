from html.parser import HTMLParser

from tests.test_dashboard import observatory

__all__ = ["observatory"]


class VisibleText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


async def test_initial_html_is_chinese_without_waiting_for_javascript(observatory):
    client, store, service, provider, _ = observatory
    before = store._connection.total_changes
    response = await client.get("/")
    assert response.status_code == 200
    visible = VisibleText()
    visible.feed(response.text)
    text = " ".join(visible.parts)
    for label in ("走进智能体", "智能体状态", "活动时间线", "主题线索", "后台任务", "详情查看器"):
        assert label in text
    assert "Agent status" not in text and "English" in text
    assert 'id="language-toggle"' in response.text
    assert 'id="drawer-language-toggle"' in response.text
    assert 'lang="zh-CN"' in response.text
    assert (await client.get("/static/i18n.js")).status_code == 200
    catalog = (await client.get("/static/zh-CN.json")).json()
    assert catalog["Reveal private messages & tool records ↓"] == "展开私人消息与工具记录 ↓"
    assert store._connection.total_changes == before and not provider.requests
