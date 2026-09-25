"""Structural centrality calculation module."""

__all__ = ["calculate_and_update_centrality"]


def calculate_and_update_centrality(*args, **kwargs):
    from .calculate import calculate_and_update_centrality as _calc
    return _calc(*args, **kwargs)
