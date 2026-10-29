"""Filtering a catalog by colour or size."""

MAX_SIZE = 10


def filter_widgets(widgets, colour=None, min_size=None):
    """The widgets matching every given condition, in their original order."""
    chosen = []
    for widget in widgets:
        if colour is not None and widget.colour != colour:
            continue
        if min_size is not None and widget.size < min_size:
            continue
        chosen.append(widget)
    return chosen
