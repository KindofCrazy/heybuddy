from html.parser import HTMLParser

from tests.test_dashboard import observatory

__all__ = ["observatory"]


class PageStructure(HTMLParser):
    def __init__(self):
        super().__init__()
        self.cards = []
        self.current = []
        self.links = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "section" and "card" in attributes.get("class", "").split():
            self.cards.append(attributes["id"])
        if tag == "a":
            self.links.append(attributes.get("href"))
            if attributes.get("aria-current") == "page":
                self.current.append(attributes["href"])


async def test_navigation_serves_distinct_scoped_pages_without_side_effects(observatory):
    client, store, _, provider, _ = observatory
    before = store._connection.total_changes
    expected = {
        "/": ["status", "activity", "character", "threads", "memory", "jobs", "runs"],
        "/threads": ["threads", "episodes"],
        "/memory": ["memory", "jobs"],
        "/runs": ["runs"],
    }
    for path, cards in expected.items():
        response = await client.get(path)
        assert response.status_code == 200
        page = PageStructure()
        page.feed(response.text)
        assert page.cards == cards
        assert page.current == [path]
        assert all(destination in page.links for destination in expected)
        assert response.text.index('id="connection"') < response.text.index('id="language-toggle"')
    assert not provider.requests and store._connection.total_changes == before
