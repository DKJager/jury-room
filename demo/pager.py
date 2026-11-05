"""Tiny demo project for Jury Room: pagination helper with a deliberate off-by-one bug."""


def page(items, number, size):
    """Return page `number` (1-based) of `items`, `size` items per page."""
    if number < 1 or size < 1:
        raise ValueError("number and size must be >= 1")
    start = (number - 1) * size
    end = start + size
    return items[start:end + 1]
