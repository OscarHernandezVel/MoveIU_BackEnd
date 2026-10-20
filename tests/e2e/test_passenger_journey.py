"""A passenger's journey through the API: guest access, live buses, comparison and alerts."""

from __future__ import annotations

from core_service.models import OccupancyState
from system_harness import API, System


class TestGuestAccess:
    def test_the_app_works_without_an_account(self, system: System) -> None:
        headers = system.guest()
        routes = system.client.get(f"{API}/routes", headers=headers).json()["routes"]
        assert [r["route_id"] for r in routes] == ["C5"] and len(routes[0]["stops"]) == 6
        detail = system.client.get(f"{API}/routes/C5", headers=headers).json()
        assert len(detail["path"]) >= 2 and detail["stops"][0]["name"] == "Terminal Sur"

    def test_live_occupancy_is_localised_and_never_colour_only(self, system: System) -> None:
        headers = system.guest()
        system.ingest("1045", 0.4, seated=41, standing=8, doors_total=49)
        body = system.client.get(f"{API}/buses/active?route_id=C5", headers=headers).json()
        occupancy = body["routes"][0]["buses"][0]["occupancy"]
        assert (occupancy["percentage"], occupancy["state"], occupancy["label"], occupancy["icon"]) == (
            100,
            "full",
            "Lleno",
            "full",
        )
        english = system.client.get(
            f"{API}/buses/active?route_id=C5", headers={**headers, "Accept-Language": "en-US,en;q=0.9"}
        ).json()
        assert english["routes"][0]["buses"][0]["occupancy"]["label"] == "Full"

    def test_silent_buses_are_shown_as_data_not_available(self, system: System) -> None:
        headers = system.guest()
        system.ingest("1045", 0.4)
        system.clock.advance(120)
        system.context.jobs.run_pending()
        bus = system.client.get(f"{API}/buses/active?route_id=C5", headers=headers).json()["routes"][0]["buses"][0]
        assert (
            bus["occupancy"]["state"] == OccupancyState.NO_DATA.value
            and bus["occupancy"]["label"] == "Datos no disponibles"
        )

    def test_route_query_limits(self, system: System) -> None:
        headers = system.guest()
        assert system.client.get(f"{API}/buses/active", headers=headers).status_code == 422
        assert system.client.get(f"{API}/buses/active?route_id=Z9", headers=headers).status_code == 404
        many = "&".join(f"route_id=R{i}" for i in range(6))
        assert system.client.get(f"{API}/buses/active?{many}", headers=headers).status_code == 422


class TestThisBusOrTheNext:
    def test_recommends_the_next_bus_when_this_one_is_crowded(self, system: System) -> None:
        headers = system.guest()
        system.ingest("1045", 0.60, seated=41, standing=8, doors_total=49)
        system.clock.advance(5)
        system.ingest("1045", 0.62, seated=41, standing=8, doors_total=49)
        system.ingest("1102", 0.30, seated=10, doors_total=10, ts_offset=0)
        system.clock.advance(5)
        system.ingest("1102", 0.34, seated=10, doors_total=10)
        system.clock.advance(5)
        system.ingest("1045", 0.63, seated=41, standing=8, doors_total=49)
        response = system.client.get(f"{API}/stops/P-022/next-buses?route_id=C5&direction=outbound", headers=headers)
        assert response.status_code == 200
        body = response.json()
        assert body["stop"]["name"] == "Av. Panamericana"
        assert [b["bus"]["bus_id"] for b in body["buses"]] == ["1045", "1102"]
        assert [b["recommended"] for b in body["buses"]] == [False, True]

    def test_unknown_stop_and_bad_parameters(self, system: System) -> None:
        headers = system.guest()
        assert system.client.get(f"{API}/stops/P-999/next-buses?route_id=C5", headers=headers).status_code == 404
        assert (
            system.client.get(
                f"{API}/stops/P-022/next-buses?route_id=C5&direction=sideways", headers=headers
            ).status_code
            == 422
        )
        assert (
            system.client.get(f"{API}/stops/P-022/next-buses?route_id=C5&limit=9", headers=headers).status_code == 422
        )


class TestProximityAlertFlow:
    def create_alert(self, system: System, headers: dict, **extra: object):  # noqa: ANN202
        body = {"route_id": "C5", "direction": "outbound", "stop_id": "P-022", "stops_before": 2, **extra}
        return system.client.post(f"{API}/alerts", json=body, headers=headers)

    def test_the_passenger_is_warned_once_on_the_screen_and_by_push(self, system: System) -> None:
        headers = system.guest()
        created = self.create_alert(system, headers)
        assert created.status_code == 201 and created.json()["status"] == "active"
        assert len(system.client.get(f"{API}/alerts", headers=headers).json()["alerts"]) == 1

        system.drive("1045", 0.20, 0.45, steps=6)  # approaching P-022

        inbox = system.client.get(f"{API}/notifications", headers=headers).json()
        assert inbox["unread"] == 1
        alert = inbox["items"][0]
        assert alert["kind"] == "proximity" and "Av. Panamericana" in alert["body"] + alert["title"]
        assert (
            alert["accessibility_label"]
            and alert["haptic_pattern"]
            and alert["deep_link"].startswith("capacidadbus://")
        )
        assert len(system.push.sent) == 1
        assert (
            system.client.get(f"{API}/alerts", headers=headers).json()["alerts"] == []
        )  # consumed: it fires only once

        system.drive("1045", 0.46, 0.55, steps=3)
        assert len(system.client.get(f"{API}/notifications", headers=headers).json()["items"]) == 1
        assert len(system.push.sent) == 1

    def test_reading_and_clearing_the_inbox(self, system: System) -> None:
        headers = system.guest()
        self.create_alert(system, headers)
        system.drive("1045", 0.20, 0.45, steps=6)
        item = system.client.get(f"{API}/notifications", headers=headers).json()["items"][0]
        assert (
            system.client.post(f"{API}/notifications/{item['notification_id']}/read", headers=headers).status_code
            == 204
        )
        assert system.client.get(f"{API}/notifications", headers=headers).json()["unread"] == 0
        assert system.client.post(f"{API}/notifications/nope/read", headers=headers).status_code == 404
        assert system.client.post(f"{API}/notifications/read-all", headers=headers).json() == {"marked": 0}

    def test_a_crowded_bus_triggers_a_warning_with_the_alternative(self, system: System) -> None:
        headers = system.guest()
        self.create_alert(system, headers, stop_id="P-031", stops_before=1)  # last stop: nobody is close yet
        system.drive("1102", 0.05, 0.15, steps=3, seated=5, doors_total=5)  # a free bus follows behind
        system.drive("1045", 0.22, 0.30, steps=3, seated=41, standing=8, doors_total=49)
        items = system.client.get(f"{API}/notifications", headers=headers).json()["items"]
        warnings = [i for i in items if i["kind"] == "occupancy_warning"]
        assert len(warnings) == 1  # not repeated on every reading while the bus stays full
        assert "1102" in warnings[0]["body"]  # says which bus comes next
        assert (
            len(system.client.get(f"{API}/alerts", headers=headers).json()["alerts"]) == 1
        )  # the alert is still waiting

    def test_alert_lifecycle_and_limits(self, system: System) -> None:
        headers = system.guest()
        created = self.create_alert(system, headers).json()
        assert system.client.delete(f"{API}/alerts/{created['alert_id']}", headers=headers).status_code == 204
        assert system.client.delete(f"{API}/alerts/{created['alert_id']}", headers=headers).status_code == 404
        for _ in range(5):
            assert self.create_alert(system, headers).status_code == 201
        limited = self.create_alert(system, headers)
        assert limited.status_code == 422 and limited.json()["error"]["code"] == "alert_limit_reached"

    def test_alerts_are_private_to_each_phone(self, system: System) -> None:
        mine, other = system.guest("device-aaaa-0001"), system.guest("device-bbbb-0002", "push-token-2")
        alert_id = self.create_alert(system, mine).json()["alert_id"]
        assert system.client.get(f"{API}/alerts", headers=other).json()["alerts"] == []
        assert system.client.delete(f"{API}/alerts/{alert_id}", headers=other).status_code == 404
        assert len(system.client.get(f"{API}/alerts", headers=mine).json()["alerts"]) == 1

    def test_validation_of_alert_requests(self, system: System) -> None:
        headers = system.guest()
        assert self.create_alert(system, headers, stops_before=9).status_code == 422
        assert self.create_alert(system, headers, stop_id="P-999").status_code == 404
        assert self.create_alert(system, headers, bus_id="9999").status_code == 404
        assert self.create_alert(system, headers, direction="up").status_code == 422
        assert self.create_alert(system, headers, unexpected="x").status_code == 422

    def test_bus_that_stops_reporting_notifies_the_passenger_who_follows_it(self, system: System) -> None:
        headers = system.guest()
        self.create_alert(system, headers, bus_id="1045", stops_before=1)
        system.drive("1045", 0.10, 0.20, steps=3)
        system.clock.advance(120)
        system.context.jobs.run_pending()
        kinds = [i["kind"] for i in system.client.get(f"{API}/notifications", headers=headers).json()["items"]]
        assert "bus_lost" in kinds
        assert any(f.kind.value == "silent_bus" for f in system.context.findings_repository.list_recent(10))
        assert len(system.notifier.messages) == 1  # high severity: the team was e-mailed

    def test_alerts_expire_and_tell_the_passenger(self, system: System) -> None:
        headers = system.guest()
        self.create_alert(system, headers)
        system.clock.advance(3_601)
        system.context.jobs.run_pending()
        headers = system.guest()  # the old access token expired long ago
        assert system.client.get(f"{API}/alerts", headers=headers).json()["alerts"] == []
        kinds = [i["kind"] for i in system.client.get(f"{API}/notifications", headers=headers).json()["items"]]
        assert kinds == ["alert_expired"]

    def test_delivery_survives_a_flaky_push_provider(self, system: System) -> None:
        system.push.fail_times = 2  # two transient errors, then success (retry with back-off)
        headers = system.guest()
        self.create_alert(system, headers)
        system.drive("1045", 0.20, 0.45, steps=6)
        assert len(system.push.sent) == 1 and system.push.attempts == 3

    def test_when_push_is_down_the_on_screen_inbox_still_delivers(self, system: System) -> None:
        system.push.fail_times = 99
        headers = system.guest()
        self.create_alert(system, headers)
        system.drive("1045", 0.20, 0.45, steps=6)
        assert system.client.get(f"{API}/notifications", headers=headers).json()["unread"] == 1
        assert system.push.sent == []
