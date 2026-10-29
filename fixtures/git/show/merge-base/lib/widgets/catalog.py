"""The widget catalog."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Widget:
    name: str
    colour: str
    size: int


DEFAULT_WIDGETS = [
    Widget("bolt", "red", 2),
    Widget("gear", "blue", 5),
    Widget("spring", "red", 3),
    Widget("washer", "green", 1),
]


class Catalog:
    """Holds widgets and answers simple questions about them."""

    def __init__(self, widgets=None):
        self.widgets = list(widgets if widgets is not None else DEFAULT_WIDGETS)

    def names(self):
        return [w.name for w in self.widgets]

    def largest(self):
        return max(self.widgets, key=lambda w: w.size)

    def total_size(self):
        return sum(w.size for w in self.widgets)
