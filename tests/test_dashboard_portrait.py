from personal_agent.character.context import CharacterContextCompiler
from personal_agent.character.loader import CharacterLoader
from tests.test_dashboard import observatory

__all__ = ["observatory"]


async def test_luna_portrait_is_local_and_only_associated_with_luna(observatory):
    client, store, service, provider, _ = observatory
    before = store._connection.total_changes
    assert (await client.get("/api/character")).json()["enabled"] is False
    package = CharacterLoader().load("luna")
    service.sessions.character = CharacterContextCompiler(package)
    profile = (await client.get("/api/character")).json()
    assert profile["portrait_url"] == "/static/characters/luna.png"
    assert profile["fingerprint"] == package.fingerprint
    assert profile["summary"] == package.persona["简介"]
    image = await client.get(profile["portrait_url"])
    assert image.status_code == 200
    assert image.headers["content-type"] == "image/png"
    assert image.content.startswith(b"\x89PNG\r\n\x1a\n")
    assert "img-src 'self'" in image.headers["content-security-policy"]
    assert "Cache-Control" in image.headers
    service.sessions.character = CharacterContextCompiler(CharacterLoader().load("miru"))
    other = (await client.get("/api/character")).json()
    assert other["portrait_url"] is None
    assert other["summary"] is None
    assert store._connection.total_changes == before
    assert not provider.requests
