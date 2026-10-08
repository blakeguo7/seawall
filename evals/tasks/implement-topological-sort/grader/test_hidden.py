import copy

import pytest

from graph import CycleError, toposort


def test_empty_graph():
    assert toposort({}) == []


def test_alphabetical_tie_break():
    assert toposort({"b": [], "a": [], "c": []}) == ["a", "b", "c"]


def test_dependency_only_nodes_are_included():
    assert toposort({"app": ["lib"]}) == ["lib", "app"]


def test_diamond():
    graph = {"d": ["b", "c"], "b": ["a"], "c": ["a"], "a": []}
    assert toposort(graph) == ["a", "b", "c", "d"]


def test_ready_nodes_use_alphabetical_order_even_when_discovered_late():
    graph = {"z": [], "m": ["z"], "a": ["z"], "b": []}
    assert toposort(graph) == ["b", "z", "a", "m"]


def test_duplicates_in_dependency_lists():
    assert toposort({"b": ["a", "a"], "a": []}) == ["a", "b"]


def test_input_is_not_modified():
    graph = {"b": ["a"], "c": ["b", "x"]}
    snapshot = copy.deepcopy(graph)
    toposort(graph)
    assert graph == snapshot


def test_self_dependency_is_a_cycle():
    with pytest.raises(CycleError) as excinfo:
        toposort({"a": ["a"]})
    assert "a" in str(excinfo.value)


def test_longer_cycle_names_its_nodes():
    with pytest.raises(CycleError) as excinfo:
        toposort({"a": ["b"], "b": ["c"], "c": ["a"], "ok": []})
    message = str(excinfo.value)
    assert all(name in message for name in "abc")


def test_cycle_error_is_a_value_error():
    assert issubclass(CycleError, ValueError)


def test_large_chain_does_not_blow_the_stack():
    graph = {f"n{i:04d}": [f"n{i - 1:04d}"] if i else [] for i in range(3000)}
    order = toposort(graph)
    assert order[0] == "n0000" and order[-1] == "n2999" and len(order) == 3000
