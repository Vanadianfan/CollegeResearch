"""Structural centrality calculation and sensitivity analysis module."""

__all__ = ["calculate_and_update_centrality", "compute_centralities"]


def calculate_and_update_centrality(*args, **kwargs):
    from .calculate import calculate_and_update_centrality as _calc
    return _calc(*args, **kwargs)


def compute_centralities(*args, **kwargs):
    from .calculate import compute_centralities as _comp
    return _comp(*args, **kwargs)
