"""Client behaviour against a mocked Resy API."""

from __future__ import annotations

from datetime import date

import httpx
import pytest
import respx

from jamesiv.models import AuthError, RateLimited, Slot, SlotTaken
from jamesiv.resy import BASE_URL, ResyClient

FIND_PAYLOAD = {
    "results": {
        "venues": [
            {
                "slots": [
                    {
                        "config": {"token": "cfg-1", "type": "Dining Room"},
                        "date": {"start": "2026-09-23 19:00:00", "end": "2026-09-23 21:00:00"},
                        "size": {"min": 2, "max": 4},
                    },
                    {
                        "config": {"token": "cfg-2", "type": "Bar"},
                        "date": {"start": "2026-09-23 17:30:00"},
                        "size": {"min": 1, "max": 2},
                    },
                    # No config token -> a waitlist stub, not bookable.
                    {"config": {"type": "Dining Room"}, "date": {"start": "2026-09-23 20:00:00"}},
                ]
            }
        ]
    }
}


@pytest.fixture
async def client():
    c = ResyClient(rate=1000, burst=1000)
    yield c
    await c.aclose()


class TestAuth:
    @respx.mock
    async def test_login_captures_token_and_default_card(self, client):
        respx.post(f"{BASE_URL}/3/auth/password").mock(
            return_value=httpx.Response(
                200,
                json={
                    "token": "tok-abc",
                    "id": 555,
                    "payment_methods": [
                        {"id": 111, "is_default": False},
                        {"id": 222, "is_default": True},
                    ],
                },
            )
        )
        await client.authenticate("a@b.com", "pw")
        assert client.auth_token == "tok-abc"
        assert client.user_id == 555
        assert client.payment_method_id == 222

    @respx.mock
    async def test_login_falls_back_to_first_card_when_none_is_default(self, client):
        respx.post(f"{BASE_URL}/3/auth/password").mock(
            return_value=httpx.Response(
                200, json={"token": "t", "payment_methods": [{"id": 999}]}
            )
        )
        await client.authenticate("a@b.com", "pw")
        assert client.payment_method_id == 999

    @respx.mock
    async def test_bad_password_raises_autherror(self, client):
        respx.post(f"{BASE_URL}/3/auth/password").mock(return_value=httpx.Response(419))
        with pytest.raises(AuthError):
            await client.authenticate("a@b.com", "wrong")

    @respx.mock
    async def test_expired_token_on_a_normal_call_raises_autherror(self, client):
        client.set_token("stale")
        respx.get(f"{BASE_URL}/3/venue").mock(return_value=httpx.Response(401))
        with pytest.raises(AuthError):
            await client.venue_by_slug("tatiana")


class TestFind:
    @respx.mock
    async def test_parses_bookable_slots_and_skips_stubs(self, client):
        respx.get(f"{BASE_URL}/4/find").mock(return_value=httpx.Response(200, json=FIND_PAYLOAD))
        slots = await client.find(venue_id=42, day="2026-09-23", party_size=2)

        assert [s.config_id for s in slots] == ["cfg-2", "cfg-1"]  # sorted by start time
        assert slots[1].seating_type == "Dining Room"
        assert slots[1].day == date(2026, 9, 23)
        assert slots[1].max_size == 4
        assert slots[1].party_size == 2

    @respx.mock
    async def test_empty_results_are_not_an_error(self, client):
        respx.get(f"{BASE_URL}/4/find").mock(
            return_value=httpx.Response(200, json={"results": {"venues": []}})
        )
        assert await client.find(venue_id=42, day="2026-09-23", party_size=2) == []

    @respx.mock
    async def test_non_200_returns_empty_rather_than_raising(self, client):
        respx.get(f"{BASE_URL}/4/find").mock(return_value=httpx.Response(404))
        assert await client.find(venue_id=42, day="2026-09-23", party_size=2) == []

    @respx.mock
    async def test_429_raises_ratelimited_with_retry_after(self, client):
        respx.get(f"{BASE_URL}/4/find").mock(
            return_value=httpx.Response(429, headers={"Retry-After": "17"})
        )
        with pytest.raises(RateLimited) as exc:
            await client.find(venue_id=42, day="2026-09-23", party_size=2)
        assert exc.value.retry_after == 17.0


class TestBooking:
    @pytest.fixture
    def slot(self):
        return Slot.from_find_payload(
            FIND_PAYLOAD["results"]["venues"][0]["slots"][0], venue_id=42, party_size=2
        )

    @respx.mock
    async def test_details_extracts_nested_book_token(self, client, slot):
        respx.post(f"{BASE_URL}/3/details").mock(
            return_value=httpx.Response(200, json={"book_token": {"value": "bt-xyz"}})
        )
        assert await client.book_token_for(slot) == "bt-xyz"

    @respx.mock
    async def test_details_on_a_vanished_slot_raises_slottaken(self, client, slot):
        respx.post(f"{BASE_URL}/3/details").mock(return_value=httpx.Response(412))
        with pytest.raises(SlotTaken):
            await client.book_token_for(slot)

    @respx.mock
    async def test_book_sends_payment_method_and_returns_tokens(self, client, slot):
        client.payment_method_id = 222
        route = respx.post(f"{BASE_URL}/3/book").mock(
            return_value=httpx.Response(
                201, json={"resy_token": "rt-1", "reservation_id": 98765}
            )
        )
        resy_token, reservation_id = await client.book(slot, "bt-xyz")

        assert (resy_token, reservation_id) == ("rt-1", "98765")
        body = route.calls.last.request.content.decode()
        assert "book_token=bt-xyz" in body
        assert "struct_payment_method" in body and "222" in body

    @respx.mock
    async def test_losing_the_race_raises_slottaken_not_a_generic_error(self, client, slot):
        respx.post(f"{BASE_URL}/3/book").mock(return_value=httpx.Response(409))
        with pytest.raises(SlotTaken):
            await client.book(slot, "bt-xyz")


class TestVenueLookup:
    @respx.mock
    async def test_resolves_slug_to_id(self, client):
        respx.get(f"{BASE_URL}/3/venue").mock(
            return_value=httpx.Response(200, json={"id": {"resy": 12345}, "name": "Tatiana"})
        )
        venue = await client.venue_by_slug("tatiana")
        assert (venue.id, venue.name) == (12345, "Tatiana")
