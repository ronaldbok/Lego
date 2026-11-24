"""Clickable Tk table headings with natural ordering of numbers and part IDs."""
import re


def sortable_headings(tree, image_column=None):
    active = None
    descending = False

    def apply():
        if active is None:
            return
        column = image_column if active == "#0" else active
        def key(item):
            value = tree.set(item, column).casefold()
            if re.fullmatch(r"[+-]?\d+", value):
                return (0, int(value))
            return (1, tuple(int(p) if i % 2 else p for i, p in enumerate(re.split(r"(\d+)", value))))
        for idx, item in enumerate(sorted(tree.get_children(), key=key, reverse=descending)):
            tree.move(item, "", idx)

    def click(column):
        nonlocal active, descending
        descending = active == column and not descending
        active = column
        apply()

    columns = list(tree["columns"])
    if image_column:
        columns.append("#0")
    for column in columns:
        tree.heading(column, command=lambda c=column: click(c))
    return apply
