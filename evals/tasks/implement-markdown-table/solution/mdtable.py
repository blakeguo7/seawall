"""Render rows of data as a Markdown table."""


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def render_table(headers, rows):
    """Return a Markdown table as a string, without a trailing newline.

    * the first line has the headers, the second line is the separator, then one line per row
    * every cell is padded with spaces to the width of its column (the longest cell, header
      included, but at least 3 characters); lines look like "| a   | b   |"
    * text cells are left-aligned; cells that are int or float (not bool) are right-aligned, and
      their separator cell ends with a colon ("---:") while text columns use "---" padded with
      dashes to the column width
    * a column counts as numeric when every row's value in it is an int or float (the header is
      always text); an empty table (no rows) has text columns only
    * None is shown as an empty cell; every other value is shown with str()
    * a row shorter than the headers is padded with empty cells; a longer one raises ValueError

    Example:
        render_table(["name", "qty"], [["apple", 3], ["fig", 12]])
        | name  | qty |
        | ----- | --: |
        | apple |   3 |
        | fig   |  12 |
    """
    columns = len(headers)
    padded = []
    for row in rows:
        if len(row) > columns:
            raise ValueError(f"row has {len(row)} cells but there are {columns} headers")
        padded.append(list(row) + [None] * (columns - len(row)))

    numeric = [bool(padded) and all(_is_number(row[i]) for row in padded) for i in range(columns)]
    text = [[("" if cell is None else str(cell)) for cell in row] for row in padded]
    widths = [max([3, len(str(headers[i]))] + [len(row[i]) for row in text]) for i in range(columns)]

    def line(cells, align_right):
        out = []
        for i, cell in enumerate(cells):
            out.append(cell.rjust(widths[i]) if align_right[i] else cell.ljust(widths[i]))
        return "| " + " | ".join(out) + " |"

    lines = [line([str(h) for h in headers], [False] * columns)]
    separator = []
    for i in range(columns):
        separator.append(("-" * (widths[i] - 1) + ":") if numeric[i] else "-" * widths[i])
    lines.append("| " + " | ".join(separator) + " |")
    lines.extend(line(row, numeric) for row in text)
    return "\n".join(lines)
