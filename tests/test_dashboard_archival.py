from personal_agent.memory import index
from tests.test_dashboard import observatory

__all__ = ["observatory"]


async def test_archival_search_reads_indexed_topics_without_models_or_writes(observatory):
    client, store, service, provider, _ = observatory
    index.create(
        service.sessions.memory,
        "music/piano.md",
        "---\nname: Piano\ndescription: Piano practice\n---\n# Piano\nArchived piano notes.\n",
    )
    before = store._connection.total_changes
    data = (await client.get("/api/memory/search", params={"query": "piano"})).json()
    assert data["results"][0]["path"] == "music/piano.md"
    assert "Archived piano notes" in data["results"][0]["content"]
    assert (await client.get("/api/memory/file?path=music/piano.md")).status_code == 200
    assert (await client.get("/api/memory/search?query=%20%20")).status_code == 422
    assert (await client.get("/api/memory/search", params={"query": "x" * 257})).status_code == 422
    assert store._connection.total_changes == before
    assert not provider.requests
