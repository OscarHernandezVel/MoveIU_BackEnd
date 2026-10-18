"""Path geometry, route track (linked list + binary search) and direction detection."""

from __future__ import annotations

import pytest

from core_service.errors import NotFoundError, ValidationError
from core_service.models import Direction, GeoPoint, Stop, haversine_m
from core_service.routing import DirectionDetector, PathGeometry, RouteCatalog, RouteTrack
from factories import demo_buses, demo_stops, demo_track


@pytest.fixture(scope="module")
def track() -> RouteTrack:
    return demo_track()


class TestPathGeometry:
    def test_length_is_about_three_and_a_half_kilometres(self, track: RouteTrack) -> None:
        assert 3_000 < track.path.length_m < 4_500

    def test_length_matches_haversine_within_half_a_percent(self, track: RouteTrack) -> None:
        points = track.path.points
        expected = sum(haversine_m(a, b) for a, b in zip(points, points[1:]))
        assert track.path.length_m == pytest.approx(expected, rel=0.005)

    def test_projection_of_the_ends(self, track: RouteTrack) -> None:
        points = track.path.points
        assert track.path.project(points[0]).progress == pytest.approx(0.0, abs=1e-9)
        assert track.path.project(points[-1]).progress == pytest.approx(1.0, abs=1e-9)

    def test_point_at_and_project_are_inverse(self, track: RouteTrack) -> None:
        for progress in (0.0, 0.13, 0.5, 0.77, 1.0):
            projected = track.path.project(track.path.point_at(progress))
            assert projected.progress == pytest.approx(progress, abs=1e-6)
            assert projected.distance_m < 0.5

    def test_far_point_reports_its_distance(self, track: RouteTrack) -> None:
        far = GeoPoint(1.2000, -77.2600)
        assert track.path.project(far).distance_m > 500

    def test_invalid_paths(self) -> None:
        with pytest.raises(ValidationError):
            PathGeometry([GeoPoint(1.0, 1.0)])
        with pytest.raises(ValidationError):
            PathGeometry([GeoPoint(1.0, 1.0), GeoPoint(1.0, 1.0)])

    def test_invalid_coordinates_are_rejected(self) -> None:
        with pytest.raises(ValidationError):
            GeoPoint(91.0, 0.0)
        with pytest.raises(ValidationError):
            GeoPoint(0.0, 181.0)


class TestRouteTrack:
    def test_stops_are_in_outbound_order_with_increasing_progress(self, track: RouteTrack) -> None:
        stops = track.stops()
        assert [s.stop.stop_id for s in stops] == ["P-001", "P-009", "P-018", "P-022", "P-027", "P-031"]
        progress = [s.progress for s in stops]
        assert progress == sorted(progress)
        assert progress[0] == pytest.approx(0.0, abs=1e-9) and progress[-1] == pytest.approx(1.0, abs=1e-9)

    def test_stops_remaining_outbound_counts_the_target(self, track: RouteTrack) -> None:
        just_after_first = track.require_stop("P-001").progress + 0.01
        assert track.stops_remaining(Direction.OUTBOUND, just_after_first, "P-018") == 2
        assert track.stops_remaining(Direction.OUTBOUND, just_after_first, "P-009") == 1
        assert track.stops_remaining(Direction.OUTBOUND, just_after_first, "P-031") == 5

    def test_stops_remaining_is_minus_one_after_the_stop(self, track: RouteTrack) -> None:
        calle18 = track.require_stop("P-018").progress
        assert track.stops_remaining(Direction.OUTBOUND, calle18, "P-018") == -1  # exactly on it: passed
        assert track.stops_remaining(Direction.OUTBOUND, calle18 + 0.01, "P-018") == -1

    def test_return_direction_walks_the_list_backwards(self, track: RouteTrack) -> None:
        near_end = track.require_stop("P-031").progress - 0.01
        assert track.stops_remaining(Direction.RETURN, near_end, "P-027") == 1
        assert track.stops_remaining(Direction.RETURN, near_end, "P-018") == 3
        assert track.stops_remaining(Direction.RETURN, near_end, "P-001") == 5  # P-031 is already behind
        assert track.stops_remaining(Direction.RETURN, 0.0, "P-001") == -1

    def test_return_direction_on_a_stop_counts_it_as_passed(self, track: RouteTrack) -> None:
        p022 = track.require_stop("P-022").progress
        assert track.stops_remaining(Direction.RETURN, p022, "P-022") == -1
        assert track.stops_remaining(Direction.RETURN, p022, "P-018") == 1

    def test_stops_ahead_are_nearest_first_and_limited(self, track: RouteTrack) -> None:
        progress = track.require_stop("P-009").progress + 0.001
        ahead = track.stops_ahead(Direction.OUTBOUND, progress, limit=3)
        assert [s.stop.stop_id for s in ahead] == ["P-018", "P-022", "P-027"]
        back = track.stops_ahead(Direction.RETURN, progress, limit=2)
        assert [s.stop.stop_id for s in back] == ["P-009", "P-001"]
        assert track.stops_ahead(Direction.OUTBOUND, 1.0, limit=5) == []

    def test_binary_search_agrees_with_a_linear_scan(self, track: RouteTrack) -> None:
        stops = track.stops()
        for step in range(0, 1001):
            progress = step / 1000
            expected = [s.stop.stop_id for s in stops if s.progress > progress][:2]
            got = [s.stop.stop_id for s in track.stops_ahead(Direction.OUTBOUND, progress, 2)]
            assert got == expected

    def test_distance_to_stop(self, track: RouteTrack) -> None:
        stop = track.require_stop("P-031")
        expected = (1.0 - 0.5) * track.path.length_m
        assert track.distance_to_stop_m(Direction.OUTBOUND, 0.5, "P-031") == pytest.approx(expected, rel=1e-9)
        assert track.distance_to_stop_m(Direction.OUTBOUND, stop.progress, "P-031") == 0.0
        assert track.distance_to_stop_m(Direction.RETURN, 0.5, "P-001") == pytest.approx(
            0.5 * track.path.length_m, rel=1e-9
        )

    def test_next_stop_in_o1_for_both_directions(self, track: RouteTrack) -> None:
        after = track.next_stop(Direction.OUTBOUND, "P-018")
        before = track.next_stop(Direction.RETURN, "P-018")
        assert after is not None and after.stop.stop_id == "P-022"
        assert before is not None and before.stop.stop_id == "P-009"
        assert track.next_stop(Direction.OUTBOUND, "P-031") is None
        assert track.next_stop(Direction.RETURN, "P-001") is None
        assert track.next_stop(Direction.OUTBOUND, "nope") is None

    def test_travel_progress_mirrors_on_the_return_trip(self, track: RouteTrack) -> None:
        assert track.travel_progress(Direction.OUTBOUND, 0.2) == 0.2
        assert track.travel_progress(Direction.RETURN, 0.2) == pytest.approx(0.8)

    def test_unknown_stop(self, track: RouteTrack) -> None:
        with pytest.raises(NotFoundError):
            track.require_stop("nope")
        with pytest.raises(NotFoundError):
            track.stops_remaining(Direction.OUTBOUND, 0.1, "nope")

    def test_terminal_distance(self, track: RouteTrack) -> None:
        assert track.distance_from_terminal_m(0.0) == 0.0
        assert track.distance_from_terminal_m(0.5) == pytest.approx(0.5 * track.path.length_m)

    def test_stop_far_from_the_path_is_rejected(self, track: RouteTrack) -> None:
        stops = list(demo_stops().values())
        stops.append(Stop("X", "Far away", GeoPoint(1.3, -77.4)))
        with pytest.raises(ValidationError):
            RouteTrack("C5", "bad", track.path, stops)

    def test_out_of_order_and_repeated_stops_are_rejected(self, track: RouteTrack) -> None:
        stops = demo_stops()
        with pytest.raises(ValidationError):
            RouteTrack("C5", "bad", track.path, [stops["P-009"], stops["P-001"]])
        with pytest.raises(ValidationError):
            RouteTrack("C5", "bad", track.path, [stops["P-001"], stops["P-001"]])
        with pytest.raises(ValidationError):
            RouteTrack("C5", "bad", track.path, [])


class TestDirectionDetector:
    detector = DirectionDetector(window=4, min_progress_delta=0.002)

    def test_increasing_progress_is_outbound(self) -> None:
        assert self.detector.detect([0.40, 0.35, 0.30], None) is Direction.OUTBOUND

    def test_decreasing_progress_is_return(self) -> None:
        assert self.detector.detect([0.30, 0.35, 0.40], None) is Direction.RETURN

    def test_stopped_bus_keeps_the_previous_direction(self) -> None:
        assert self.detector.detect([0.4001, 0.4000], Direction.RETURN) is Direction.RETURN
        assert self.detector.detect([0.4001, 0.4000], None) is None

    def test_needs_two_samples(self) -> None:
        assert self.detector.detect([0.4], Direction.OUTBOUND) is Direction.OUTBOUND
        assert self.detector.detect([], None) is None

    def test_only_the_window_is_considered(self) -> None:
        # newest four samples go outbound even though older ones went the other way
        assert self.detector.detect([0.50, 0.48, 0.46, 0.44, 0.90], None) is Direction.OUTBOUND

    def test_invalid_window(self) -> None:
        with pytest.raises(ValueError):
            DirectionDetector(window=1)


class TestRouteCatalog:
    def test_replace_and_lookup(self, track: RouteTrack) -> None:
        catalog = RouteCatalog()
        catalog.replace_all([track], demo_buses())
        assert catalog.route_ids() == ["C5"]
        assert catalog.require_track("C5") is track
        assert catalog.require_bus("1045").capacity.seats == 41
        assert [b.bus_id for b in catalog.buses()] == ["1045", "1088", "1102"]
        assert catalog.find_track(None) is None
        with pytest.raises(NotFoundError):
            catalog.require_track("Z9")
        with pytest.raises(NotFoundError):
            catalog.require_bus("0000")
