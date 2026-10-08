from graph import toposort


def test_simple_chain():
    assert toposort({"app": ["lib", "log"], "lib": ["log"]}) == ["log", "lib", "app"]
