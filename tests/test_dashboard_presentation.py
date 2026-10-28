import pytest

from personal_agent.dashboard import app as dashboard
from personal_agent.dashboard.layout import CARDS, PAGES
from tests.test_dashboard import observatory
from tests.test_dashboard_navigation import PageStructure

__all__ = ["observatory"]


async def test_old_runtime_card_context_does_not_crowd_overview(observatory, monkeypatch):
    client, _, _, _, _ = observatory
    pages = dict(PAGES)
    pages["overview"] = (*PAGES["overview"][:3], CARDS)
    monkeypatch.setattr(dashboard, "PAGES", pages)
    response = await client.get("/")
    page = PageStructure()
    page.feed(response.text)
    assert page.cards == ["status", "activity", "character", "jobs"]
    assert 'class="title-dot"' not in response.text
    assert '<symbol id="icon-book-open"' in response.text
    assert 'href="#icon-book-open"' in response.text
    assert "ISC License" in (await client.get("/static/ICONS-LICENSE.txt")).text


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
async def test_markdown_frontmatter_is_not_a_heading_and_raw_content_is_preserved(
    observatory, newline
):
    client, _, service, _, _ = observatory
    memory = service.sessions.memory
    folder = memory.root / "topics"
    folder.mkdir()
    content = "---\nname: Example\ndescription: A topic\n---\n# Actual title\n\nReal notes.\n"
    content = content.replace("\n", newline)
    (folder / "example.md").write_bytes(content.encode("utf-8"))
    response = (await client.get("/api/memory/file?path=topics/example.md")).json()
    assert response["content"] == content
    assert "name: Example" not in response["html"]
    assert "Actual title" in response["html"] and "Real notes." in response["html"]
    assert (await client.get("/static/memory-workspace.js")).status_code == 200
