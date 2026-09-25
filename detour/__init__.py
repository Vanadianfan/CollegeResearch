"""Detour cost and all-pairs shortest path calculation module."""

__all__ = ["calculate_all_shortest_paths", "ensure_shortest_path_table"]


def calculate_all_shortest_paths(*args, **kwargs):
    from .calculate import calculate_all_shortest_paths as _fn
    return _fn(*args, **kwargs)


def ensure_shortest_path_table(*args, **kwargs):
    from .calculate import ensure_shortest_path_table as _fn
    return _fn(*args, **kwargs)
