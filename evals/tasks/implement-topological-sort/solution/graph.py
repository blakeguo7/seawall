"""Dependency ordering."""

import heapq


class CycleError(ValueError):
    """The graph contains a cycle. The message names the nodes on the cycle."""


def toposort(graph):
    """Order the nodes of a dependency graph so every node comes after its dependencies.

    `graph` maps each node to the list of nodes it depends on, e.g. {"app": ["lib", "log"],
    "lib": ["log"]} gives ["log", "lib", "app"].

    * nodes that only appear as dependencies are part of the result too
    * among nodes that are ready at the same time, the alphabetically smallest comes first, so
      the result is deterministic: {"b": [], "a": []} gives ["a", "b"]
    * a node that depends on itself, or any longer cycle, raises CycleError; the message lists
      the nodes of the cycle (at least the names of the nodes that cannot be ordered)
    * duplicate dependencies in a list are harmless
    * the input is not modified
    """
    dependencies = {}
    for node, deps in graph.items():
        dependencies.setdefault(node, set()).update(deps)
        for dep in deps:
            dependencies.setdefault(dep, set())
    dependents = {node: set() for node in dependencies}
    remaining = {}
    for node, deps in dependencies.items():
        remaining[node] = len(deps)
        for dep in deps:
            dependents[dep].add(node)

    ready = [node for node, count in remaining.items() if count == 0]
    heapq.heapify(ready)
    order = []
    while ready:
        node = heapq.heappop(ready)
        order.append(node)
        for dependent in dependents[node]:
            remaining[dependent] -= 1
            if remaining[dependent] == 0:
                heapq.heappush(ready, dependent)
    if len(order) != len(dependencies):
        stuck = sorted(node for node, count in remaining.items() if count > 0)
        raise CycleError("dependency cycle involving: " + ", ".join(stuck))
    return order
