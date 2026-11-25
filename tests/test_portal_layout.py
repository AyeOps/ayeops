from __future__ import annotations

from ayeops.portal.layout import BOX_W, Edge, edge_path, layout, path_length


def chain(count: int) -> list[Edge]:
    return [Edge(f"n{i}", f"n{i + 1}") for i in range(count - 1)]


def test_columns_follow_the_longest_path_from_the_sources() -> None:
    plan = layout([Edge("a", "b"), Edge("b", "c"), Edge("a", "c")])
    assert plan.rank["a"] == 0
    assert plan.rank["b"] == 1
    assert plan.rank["c"] == 2


def test_an_edge_that_skips_a_column_runs_in_its_own_lane() -> None:
    edges = [Edge("a", "b"), Edge("b", "c"), Edge("a", "c")]
    plan = layout(edges)
    assert len(edges[2].via) == 1
    assert edges[2].via[0] in plan.slots
    assert edges[2].via[0] not in plan.pos


def test_rows_follow_the_barycenter_of_neighbours() -> None:
    plan = layout([Edge("a", "x"), Edge("b", "y"), Edge("c", "x")])
    assert plan.columns[0] == ["a", "c", "b"]
    assert plan.pos["c"].y < plan.pos["b"].y


def test_a_cycle_is_broken_by_exactly_one_back_edge() -> None:
    edges = [Edge("a", "b"), Edge("b", "c"), Edge("c", "a")]
    plan = layout(edges)
    assert [edge.back for edge in edges] == [False, False, True]
    assert [plan.rank[n] for n in ("a", "b", "c")] == [0, 1, 2]
    path, _ = edge_path(edges[2], plan)
    assert path.startswith("M") and " C " in path


def test_every_band_holds_only_its_own_nodes_and_bands_do_not_overlap() -> None:
    edges = [Edge("a1/agent", "a1/store"), Edge("b2/agent", "a1/store")]
    band_of = {"a1/agent": "a1", "a1/store": "a1", "b2/agent": "b2"}
    plan = layout(edges, band_of=band_of, bands=["a1", "b2"])
    first, second = plan.bands
    assert (first.name, second.name) == ("a1", "b2")
    assert first.y + first.height <= second.y
    for node, band in band_of.items():
        top = next(b for b in plan.bands if b.name == band)
        assert top.y <= plan.pos[node].y <= top.y + top.height - plan.size(node)


def test_fixed_columns_win_over_ranks_and_a_leftward_edge_enters_from_the_right() -> None:
    edges = [Edge("query", "store"), Edge("agent", "store")]
    plan = layout(edges, column_of={"agent": 1, "store": 4, "query": 9})
    assert [plan.rank[n] for n in ("agent", "store", "query")] == [0, 1, 2]
    assert edges[0].back is True
    path, _ = edge_path(edges[0], plan)
    store = plan.pos["store"]
    assert path.endswith(f"{store.x + BOX_W:g} {path.split()[-1]}")


def test_edges_on_one_side_of_a_box_get_separate_ports() -> None:
    edges = [Edge("a", "hub"), Edge("b", "hub"), Edge("c", "hub")]
    layout(edges)
    ends = [edge.ports[1] for edge in edges if edge.ports]
    assert len(set(ends)) == 3


def test_a_weak_edge_pointing_backwards_is_hidden_instead_of_drawn() -> None:
    edges = [Edge("agent", "store"), Edge("store", "agent", weak=True)]
    layout(edges)
    assert edges[1].hidden is True
    assert edges[0].hidden is False


def test_a_node_with_only_weak_edges_sits_one_column_before_its_target() -> None:
    edges = [Edge("agent", "store"), Edge("exporter", "store", weak=True)]
    plan = layout(edges)
    assert plan.rank["exporter"] == plan.rank["store"] - 1


def test_isolated_nodes_are_still_placed() -> None:
    plan = layout([], ["lonely"])
    assert "lonely" in plan.pos
    assert layout([]).empty


def test_path_length_of_a_straight_line_is_its_length() -> None:
    assert path_length("M0 0 C 10 0 20 0 30 0") == 30
