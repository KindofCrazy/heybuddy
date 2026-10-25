import socket

import pytest

from personal_agent.config import ENV_FIELDS


@pytest.fixture(autouse=True)
def offline_isolated_environment(monkeypatch, tmp_path):
    """Tests cannot discover real credentials or reach a remote network endpoint."""
    monkeypatch.chdir(tmp_path)
    for name in (*ENV_FIELDS, "OPENAI_BASE_URL", "OPENAI_ORG_ID", "OPENAI_PROJECT_ID"):
        monkeypatch.delenv(name, raising=False)

    original_connect = socket.socket.connect
    original_resolve = socket.getaddrinfo

    def connect(sock, address):
        # Windows asyncio creates a local socketpair for its own event loop.
        if sock.family in {socket.AF_INET, socket.AF_INET6} and address[0] not in {
            "127.0.0.1",
            "::1",
        }:
            raise AssertionError("Tests must not access the network")
        return original_connect(sock, address)

    def resolve(host, *args, **kwargs):
        if host not in {None, "localhost", "127.0.0.1", "::1"}:
            raise AssertionError("Tests must not resolve external hosts")
        return original_resolve(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket, "getaddrinfo", resolve)
