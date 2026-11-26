from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from ayeops.downtime import (
    HOSTS_DOWN,
    NODE_UP,
    Downtime,
    alert_guards,
    downtime_of,
    downtime_problems,
    fleet_guards,
    gone_after,
    gone_expr,
    gone_term,
    guarded,
    host_set,
    hostless_alert,
    hosts_down,
    in_maintenance,
    in_window,
    maintenance_hosts,
    module_alert,
    seconds,
    service_alert,
    service_guards,
    span,
    volatile_down,
    volatile_hosts,
    volatile_problems,
    window_guards,
    window_problems,
    window_terms,
)
from ayeops.links import UNREACHABLE, link_down_expr
from ayeops.manifest import Manifest

TERM = re.compile(r"vector\(time\(\) - (\d+)\) % (\d+) < (\d+)")
SUN_2300_3H = "(vector(time() - 342000) % 604800 < 10800)"
DAILY_0230_1H = "(vector(time() - 9000) % 86400 < 3600)"
EDGE_WINDOW = f'label_replace({SUN_2300_3H}, "host", "edge", "", "")'
VOLATILE_EDGE = 'label_replace(vector(1), "host", "edge", "", "")'
EDGE_DOWN = (
    'max by (host) (last_over_time(up{job="node", host="edge"}[8d])'
    ' * (lag(up{job="node", host="edge"}[8d]) < bool 180)) == 0'
)


def fleet(**hosts: dict[str, Any]) -> Manifest:
    return Manifest({"version": 1, "project": "acme", "hosts": hosts}, Path())


def node_host(**fields: Any) -> dict[str, Any]:
    return {"arch": "amd64", "modules": {"node": {}}, **fields}


def window(start: str, duration: str, *days: str) -> dict[str, Any]:
    found: dict[str, Any] = {"start": start, "duration": duration}
    if days:
        found["days"] = list(days)
    return found


def active(expr: str, moment: datetime) -> bool:
    now = moment.timestamp()
    terms = TERM.findall(expr)
    assert terms
    return any((now - int(o)) % int(p) < int(length) for o, p, length in terms)


def at(day: int, hour: int, minute: int = 0, second: int = 0) -> datetime:
    return datetime(2026, 11, day, hour, minute, second, tzinfo=UTC)


def test_the_node_freshness_and_the_down_hosts_keep_their_rendered_form() -> None:
    assert NODE_UP == ('last_over_time(up{job="node"}[1d]) * (lag(up{job="node"}[1d]) < bool 180)')
    assert f"max by (host) ({NODE_UP}) == 0" == HOSTS_DOWN
    assert hosts_down('job="node", host="a"', "2d") == (
        'max by (host) (last_over_time(up{job="node", host="a"}[2d])'
        ' * (lag(up{job="node", host="a"}[2d]) < bool 180)) == 0'
    )


@pytest.mark.parametrize(
    ("duration", "total"),
    [("90m", 5400), ("2h", 7200), ("1d", 86400), ("0m", 0), ("10080m", 604800)],
)
def test_a_duration_reads_as_seconds(duration: str, total: int) -> None:
    assert seconds(duration) == total


@pytest.mark.parametrize("duration", ["1w", "h", "1.5h", "1h\n", " 1h", "-1h", ""])
def test_anything_else_is_not_a_duration(duration: str) -> None:
    with pytest.raises(ValueError, match=r"is not a duration such as 30m, 2h or 1d"):
        seconds(duration)


@pytest.mark.parametrize(
    ("total", "text"),
    [(86400, "1d"), (864000, "10d"), (7200, "2h"), (90000, "25h"), (5400, "90m"), (60, "1m")],
)
def test_a_span_is_written_in_the_largest_whole_unit(total: int, text: str) -> None:
    assert span(total) == text


@pytest.mark.parametrize(
    ("spec", "after"),
    [
        ({"volatile": True}, 86400),
        ({"volatile": "3d"}, 259200),
        ({"volatile": "90m"}, 5400),
        ({"volatile": False}, None),
        ({}, None),
    ],
)
def test_volatile_true_means_a_day_and_a_duration_means_itself(
    spec: dict[str, Any], after: int | None
) -> None:
    assert gone_after(spec) == after


def test_the_volatile_hosts_are_listed_by_name_with_their_durations() -> None:
    manifest = fleet(
        b=node_host(volatile="2h"), a=node_host(volatile=True), c=node_host(), d=node_host()
    )
    found = volatile_hosts(manifest)
    assert found == {"a": 86400, "b": 7200}
    assert list(found) == ["a", "b"]


def test_a_daily_window_repeats_every_day_from_its_start() -> None:
    assert window_terms(window("02:30", "1h")) == [(9000, 86400, 3600)]


def test_a_window_on_days_starts_on_each_of_them_counted_from_the_epoch() -> None:
    terms = window_terms(window("23:00", "3h", "mon", "fri", "sun", "thu", "sat", "tue", "wed"))
    day, start = 86400, 82800
    assert terms == [
        (4 * day + start, 604800, 10800),
        (1 * day + start, 604800, 10800),
        (3 * day + start, 604800, 10800),
        (0 * day + start, 604800, 10800),
        (2 * day + start, 604800, 10800),
        (5 * day + start, 604800, 10800),
        (6 * day + start, 604800, 10800),
    ]
    for (offset, _, _), name in zip(
        terms, ("Mon", "Fri", "Sun", "Thu", "Sat", "Tue", "Wed"), strict=True
    ):
        assert datetime.fromtimestamp(offset, UTC).strftime("%a %H:%M") == f"{name} 23:00"


def test_the_windows_of_a_host_are_one_or_of_their_terms() -> None:
    windows = [window("02:30", "1h"), window("23:00", "3h", "sun", "mon")]
    assert in_window(windows) == (
        f"{DAILY_0230_1H} or {SUN_2300_3H} or (vector(time() - 428400) % 604800 < 10800)"
    )


@pytest.mark.parametrize(
    ("moment", "inside"),
    [
        (at(29, 22, 59, 59), False),
        (at(29, 23), True),
        (at(29, 23, 59, 59), True),
        (at(30, 0), True),
        (at(30, 1, 59, 59), True),
        (at(30, 2), False),
        (at(23, 23, 30), False),
        (at(28, 23, 30), False),
        (at(22, 23, 30), True),
    ],
)
def test_a_sunday_night_window_crosses_midnight_and_the_week_boundary(
    moment: datetime, inside: bool
) -> None:
    assert at(29, 0).strftime("%a") == "Sun"
    assert active(in_window([window("23:00", "3h", "sun")]), moment) is inside


@pytest.mark.parametrize(
    ("moment", "inside"),
    [
        (at(24, 21, 59), False),
        (at(24, 22), True),
        (at(25, 1, 59), True),
        (at(25, 2), False),
        (at(25, 22, 30), True),
    ],
)
def test_a_daily_window_crosses_midnight_every_night(moment: datetime, inside: bool) -> None:
    assert active(in_window([window("22:00", "4h")]), moment) is inside


@pytest.mark.parametrize(
    ("moment", "inside"),
    [
        (at(28, 23, 59), False),
        (at(29, 0), True),
        (at(29, 23, 59, 59), True),
        (at(30, 0), False),
        (at(30, 12), False),
    ],
)
def test_a_whole_day_window_ends_at_midnight(moment: datetime, inside: bool) -> None:
    assert active(in_window([window("00:00", "24h", "sun")]), moment) is inside


def test_hosts_without_windows_have_no_maintenance() -> None:
    manifest = fleet(
        b=node_host(maintenance=[window("02:30", "1h")]),
        a=node_host(maintenance=[window("23:00", "3h", "sun")]),
        c=node_host(maintenance=[]),
        d=node_host(),
    )
    found = maintenance_hosts(manifest)
    assert found == {"a": SUN_2300_3H, "b": DAILY_0230_1H}
    assert list(found) == ["a", "b"]


def test_host_sets_and_maintenance_vectors_label_each_host() -> None:
    assert host_set(["a", "b"]) == (
        'label_replace(vector(1), "host", "a", "", "")'
        ' or label_replace(vector(1), "host", "b", "", "")'
    )
    assert in_maintenance({"a": "(x)", "b": "(y)"}) == (
        'label_replace((x), "host", "a", "", "") or label_replace((y), "host", "b", "", "")'
    )


def test_a_volatile_host_is_gone_after_its_duration_and_seen_for_a_week_more() -> None:
    up = 'up{job="node", host=~"a|b"}'
    assert gone_term(["a", "b"], 7200) == (
        f"(max by (host) (max_over_time({up}[2h]))"
        f" + on (host) group_right () (0 * last_over_time({up}[170h])))"
        f" or 0 * last_over_time({up}[170h])"
    )


def test_volatile_hosts_with_the_same_duration_share_one_term_shortest_first() -> None:
    expr = gone_expr({"a": 259200, "b": 86400, "c": 259200})
    assert expr == f"1 - (({gone_term(['b'], 86400)}) or ({gone_term(['a', 'c'], 259200)}))"


def test_a_service_on_a_volatile_host_waits_while_the_host_is_down() -> None:
    assert volatile_down("edge", 86400) == EDGE_DOWN


def test_the_downtime_of_a_manifest_names_volatile_and_maintained_hosts() -> None:
    manifest = fleet(
        edge=node_host(volatile=True, maintenance=[window("23:00", "3h", "sun")]),
        central=node_host(),
    )
    assert downtime_of(manifest) == Downtime({"edge": 86400}, {"edge": SUN_2300_3H})


def test_guards_wrap_the_expression_and_keep_its_series_at_zero() -> None:
    alert = {"name": "x", "expr": " up == bool 0\n"}
    assert guarded(alert, []) is alert
    assert guarded(alert, [("host", "(a)"), ("", "b")]) == {
        "name": "x",
        "expr": "(up == bool 0) unless on (host) ((a)) unless on () (b) or 0 * (up == bool 0)",
    }


def test_the_fleet_guard_is_the_down_hosts_only_with_node() -> None:
    assert fleet_guards(True) == [("host", HOSTS_DOWN)]
    assert fleet_guards(False) == []


def test_the_window_guard_is_the_maintained_hosts_only_with_windows() -> None:
    assert window_guards(Downtime({}, {"edge": SUN_2300_3H})) == [("host", EDGE_WINDOW)]
    assert window_guards(Downtime({"edge": 86400}, {})) == []


BOTH = Downtime({"edge": 86400}, {"edge": SUN_2300_3H})
NONE = Downtime({}, {})


def test_host_down_leaves_out_the_volatile_hosts_and_any_host_in_maintenance() -> None:
    assert alert_guards("host-down", True, BOTH) == [
        ("host", VOLATILE_EDGE),
        ("host", EDGE_WINDOW),
    ]
    assert alert_guards("host-down", True, Downtime({}, {"edge": SUN_2300_3H})) == [
        ("host", EDGE_WINDOW)
    ]
    assert alert_guards("host-down", True, NONE) == []


def test_host_gone_is_held_by_maintenance_only() -> None:
    assert alert_guards("host-gone", True, BOTH) == [("host", EDGE_WINDOW)]
    assert alert_guards("host-gone", True, Downtime({"edge": 86400}, {})) == []


def test_every_other_alert_is_held_by_down_hosts_and_by_maintenance() -> None:
    assert alert_guards("disk-full", True, BOTH) == [("host", HOSTS_DOWN), ("host", EDGE_WINDOW)]
    assert alert_guards("disk-full", False, BOTH) == [("host", EDGE_WINDOW)]
    assert alert_guards("disk-full", True, NONE) == [("host", HOSTS_DOWN)]
    assert alert_guards("disk-full", False, NONE) == []


def test_host_gone_is_rendered_for_the_volatile_hosts() -> None:
    alert = {"name": "host-gone", "expr": "generic", "severity": "warning"}
    gone = gone_expr({"edge": 86400})
    assert module_alert(alert, True, Downtime({"edge": 86400}, {})) == {
        "name": "host-gone",
        "expr": gone,
        "severity": "warning",
    }
    assert module_alert(alert, True, BOTH) == {
        "name": "host-gone",
        "expr": f"({gone}) unless on (host) ({EDGE_WINDOW}) or 0 * ({gone})",
        "severity": "warning",
    }


def test_a_module_alert_takes_its_guards() -> None:
    alert = {"name": "disk-full", "expr": "x"}
    assert module_alert(alert, True, NONE) == {
        "name": "disk-full",
        "expr": f"(x) unless on (host) ({HOSTS_DOWN}) or 0 * (x)",
    }
    assert module_alert({"name": "host-down", "expr": "y"}, True, NONE) == {
        "name": "host-down",
        "expr": "y",
    }


def test_a_service_on_a_volatile_maintained_host_is_held_by_that_host_alone() -> None:
    assert service_guards("edge", True, BOTH) == [
        ("host", HOSTS_DOWN),
        ("host", EDGE_WINDOW),
        ("", EDGE_DOWN),
        ("", SUN_2300_3H),
    ]


def test_a_hostless_alert_takes_no_guard() -> None:
    alert = {"name": "duty-overdue", "expr": "vector(time() >= bool 1)"}
    assert hostless_alert(alert) == {"name": "duty-overdue", "expr": "vector(time() >= bool 1)"}


def test_a_service_elsewhere_gets_the_fleet_guards_only() -> None:
    assert service_guards("central", True, BOTH) == [("host", HOSTS_DOWN), ("host", EDGE_WINDOW)]
    assert service_guards("central", False, NONE) == []


def test_without_node_a_volatile_host_holds_no_service() -> None:
    assert service_guards("edge", False, Downtime({"edge": 86400}, {})) == []


def test_a_service_alert_is_wrapped_in_its_guards() -> None:
    alert = {"name": "web-down", "expr": "p == bool 0"}
    assert service_alert(alert, "edge", True, Downtime({"edge": 86400}, {})) == {
        "name": "web-down",
        "expr": (
            f"(p == bool 0) unless on (host) ({HOSTS_DOWN}) unless on () ({EDGE_DOWN})"
            " or 0 * (p == bool 0)"
        ),
    }
    assert service_alert(alert, "edge", False, NONE) is alert


@pytest.mark.parametrize("value", [True, "1h", "60m", "7d", "168h", "10080m"])
def test_a_volatile_node_host_within_an_hour_and_a_week_passes(value: Any) -> None:
    manifest = fleet(edge=node_host(volatile=value))
    assert volatile_problems(manifest, "edge", manifest.hosts["edge"]) == []


@pytest.mark.parametrize("value", ["59m", "10081m", "8d", "169h"])
def test_a_volatile_duration_outside_an_hour_and_a_week_fails(value: str) -> None:
    manifest = fleet(edge=node_host(volatile=value))
    assert volatile_problems(manifest, "edge", manifest.hosts["edge"]) == [
        (
            "hosts.edge.volatile",
            "host-volatile",
            f"volatile: {value} is outside 1h to 7d, the time a host may be gone",
        )
    ]


def test_a_volatile_host_without_node_fails() -> None:
    manifest = fleet(edge={"arch": "amd64", "modules": {"metrics": {}}, "volatile": "8d"})
    assert volatile_problems(manifest, "edge", manifest.hosts["edge"]) == [
        (
            "hosts.edge.volatile",
            "host-volatile",
            "volatile softens host-down, which needs the node module on the host",
        )
    ]


def test_a_host_that_is_not_volatile_has_no_volatile_problem() -> None:
    manifest = fleet(edge={"arch": "amd64", "modules": {}, "volatile": False})
    assert volatile_problems(manifest, "edge", manifest.hosts["edge"]) == []


@pytest.mark.parametrize(
    "spec",
    [
        window("02:00", "24h", "mon"),
        window("02:00", "1d", "mon", "tue", "wed", "thu", "fri", "sat"),
        window("02:00", "1439m"),
        window("02:00", "23h", "mon", "tue", "wed", "thu", "fri", "sat", "sun"),
        window("02:00", "1m"),
    ],
)
def test_a_window_that_opens_and_closes_passes(spec: dict[str, Any]) -> None:
    assert window_problems("w", spec) == []


@pytest.mark.parametrize(
    ("spec", "message"),
    [
        (window("02:00", "0m"), "a window of 0 never opens"),
        (window("02:00", "0h", "mon"), "a window of 0 never opens"),
        (
            window("02:00", "1441m", "mon"),
            "1441m is longer than 24h: split it into windows on consecutive days",
        ),
        (
            window("02:00", "2d", "sat"),
            "2d is longer than 24h: split it into windows on consecutive days",
        ),
        (window("02:00", "24h"), "a window of 24h on every day never closes"),
        (
            window("02:00", "1d", "mon", "tue", "wed", "thu", "fri", "sat", "sun"),
            "a window of 24h on every day never closes",
        ),
    ],
)
def test_a_window_that_never_opens_or_never_closes_fails(
    spec: dict[str, Any], message: str
) -> None:
    assert window_problems("w", spec) == [("w.duration", "maintenance-window", message)]


def test_the_problems_of_every_host_and_window_are_reported_by_path() -> None:
    manifest = fleet(
        b=node_host(volatile="30m", maintenance=[window("02:00", "1h"), window("03:00", "0m")]),
        a={
            "arch": "amd64",
            "modules": {},
            "volatile": True,
            "maintenance": [window("01:00", "2d")],
        },
        c=node_host(volatile=True, maintenance=[window("02:00", "1h")]),
    )
    assert downtime_problems(manifest) == [
        (
            "hosts.a.volatile",
            "host-volatile",
            "volatile softens host-down, which needs the node module on the host",
        ),
        (
            "hosts.a.maintenance.0.duration",
            "maintenance-window",
            "2d is longer than 24h: split it into windows on consecutive days",
        ),
        (
            "hosts.b.volatile",
            "host-volatile",
            "volatile: 30m is outside 1h to 7d, the time a host may be gone",
        ),
        ("hosts.b.maintenance.1.duration", "maintenance-window", "a window of 0 never opens"),
    ]


LINKED = Downtime({"edge": 86400}, {"edge": SUN_2300_3H}, True)


def test_the_downtime_of_a_manifest_says_whether_any_host_probes_links() -> None:
    manifest = fleet(
        central=node_host(address="10.8.0.1", modules={"probes": {"links": ["edge"]}}),
        edge=node_host(address="10.8.0.2"),
    )
    assert downtime_of(manifest) == Downtime({}, {}, True)
    assert downtime_of(fleet(central=node_host())) == Downtime({}, {}, False)


def test_host_down_is_also_held_for_the_hosts_of_an_unreachable_zone() -> None:
    assert alert_guards("host-down", True, LINKED) == [
        ("host", VOLATILE_EDGE),
        ("host", UNREACHABLE),
        ("host", EDGE_WINDOW),
    ]
    assert alert_guards("host-down", True, Downtime({}, {}, True)) == [("host", UNREACHABLE)]
    assert alert_guards("disk-full", True, Downtime({}, {}, True)) == [("host", HOSTS_DOWN)]
    assert alert_guards("host-gone", True, LINKED) == [("host", EDGE_WINDOW)]


def test_link_down_drops_the_peers_in_maintenance_and_keeps_its_guards() -> None:
    alert = {"name": "link-down", "expr": "plain"}
    dropped = link_down_expr({"edge": SUN_2300_3H})
    assert module_alert(alert, True, LINKED) == {
        "name": "link-down",
        "expr": (
            f"({dropped}) unless on (host) ({HOSTS_DOWN})"
            f" unless on (host) ({EDGE_WINDOW}) or 0 * ({dropped})"
        ),
    }
    assert module_alert(alert, False, Downtime({}, {}, True)) == alert
    assert module_alert({"name": "disk-full", "expr": "x"}, False, LINKED) == {
        "name": "disk-full",
        "expr": f"(x) unless on (host) ({EDGE_WINDOW}) or 0 * (x)",
    }
