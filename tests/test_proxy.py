"""Proxy + aggressive-polling wiring: the fix for datacenter-IP throttling."""

from __future__ import annotations

import pytest

from jamesiv.config import Config, DropConfig, Secrets, Settings, Target
from jamesiv.hunter import Hunter
from jamesiv.notify import Notifier
from jamesiv.resy import ResyClient
from jamesiv.state import Store


class _Notifier(Notifier):
    def __init__(self):
        super().__init__(Secrets())
    async def send(self, *a, **k):
        pass


def _hunter(store, *, proxy: str = "", aggressive: bool = False):
    target = Target(name="T", slug="t", venue_id=1,
                    drop={"at": "10:00", "days_ahead": 30})
    config = Config(
        settings=Settings(aggressive_polling=aggressive,
                          aggressive_max_requests=40, aggressive_interval_ms=120),
        targets=[target],
    )
    secrets = Secrets(proxy_url=proxy)
    return Hunter(config=config, secrets=secrets, client=ResyClient(rate=99, burst=99),
                  store=store, notifier=_Notifier()), target


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "p.db")
    yield s
    s.close()


class TestEffectiveDrop:
    def test_proxy_plus_aggressive_lifts_the_cap(self, store):
        hunter, target = _hunter(store, proxy="http://u:p@gate:7000", aggressive=True)
        eff = hunter._effective_drop(target.drop)
        assert eff.max_requests == 40
        assert eff.burst_interval_ms == 120

    def test_aggressive_without_a_proxy_changes_nothing(self, store):
        # The critical safety rule: no proxy => the polite 5-shot cap stands,
        # even with aggressive_polling mistakenly on.
        hunter, target = _hunter(store, proxy="", aggressive=True)
        eff = hunter._effective_drop(target.drop)
        assert eff.max_requests == target.drop.max_requests == 5

    def test_proxy_without_aggressive_stays_polite(self, store):
        hunter, target = _hunter(store, proxy="http://u:p@gate:7000", aggressive=False)
        assert hunter._effective_drop(target.drop).max_requests == 5

    def test_recon_always_beats_aggressive(self, store):
        hunter, _ = _hunter(store, proxy="http://u:p@gate:7000", aggressive=True)
        recon_drop = DropConfig(at="10:00", days_ahead=30, recon=True)
        # Recon is measurement, not racing: its own sampling plan must win.
        assert hunter._effective_drop(recon_drop).recon is True
        assert hunter._effective_drop(recon_drop).max_requests == 5


class TestProxyWiring:
    async def test_clients_accept_a_proxy_without_error(self):
        # httpx must build a client with the proxy set; construction is the
        # surface that would break on an unsupported kwarg.
        c = ResyClient(rate=1, burst=1, proxy="http://u:p@gate:7000")
        assert c.http is not None
        await c.aclose()

    def test_has_proxy_reflects_the_secret(self):
        assert Secrets(proxy_url="http://x").has_proxy is True
        assert Secrets(proxy_url="").has_proxy is False
