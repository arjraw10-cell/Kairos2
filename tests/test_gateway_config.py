from __future__ import annotations

import pytest

from kairos.config import Config


def test_gateway_port_is_validated(monkeypatch):
    monkeypatch.setenv("KAIROS_GATEWAY_PORT", "65535")
    Config.reload()
    assert Config.KAIROS_GATEWAY_PORT() == 65535

    monkeypatch.setenv("KAIROS_GATEWAY_PORT", "0")
    Config.reload()
    with pytest.raises(ValueError, match="between 1 and 65535"):
        Config.KAIROS_GATEWAY_PORT()

    monkeypatch.setenv("KAIROS_GATEWAY_PORT", "not-a-port")
    Config.reload()
    with pytest.raises(ValueError, match="must be an integer"):
        Config.KAIROS_GATEWAY_PORT()

    monkeypatch.delenv("KAIROS_GATEWAY_PORT", raising=False)
    Config.reload()
