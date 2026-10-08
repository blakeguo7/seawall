from groups import Group, collect


def test_collect_with_explicit_bucket():
    bucket = [9]
    assert collect(3, bucket) is bucket
    assert bucket == [9, 3]


def test_collect_default_is_fresh_each_time():
    for n in range(5):
        assert collect(n) == [n]


def test_groups_do_not_share_members():
    groups = [Group(str(i)) for i in range(3)]
    groups[0].add("x").add("y")
    assert [len(g) for g in groups] == [2, 0, 0]


def test_members_passed_in_are_kept():
    members = ["m"]
    group = Group("g", members)
    group.add("n")
    assert group.members == ["m", "n"]


def test_signature_has_no_mutable_defaults():
    import inspect

    for fn in (collect, Group.__init__):
        for param in inspect.signature(fn).parameters.values():
            assert not isinstance(param.default, (list, dict, set))
