from groups import Group, collect


def test_collect_starts_fresh():
    assert collect(1) == [1]
    assert collect(2) == [2]


def test_groups_are_independent():
    a, b = Group("a"), Group("b")
    a.add("x")
    assert len(b) == 0
