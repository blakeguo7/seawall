"""Render rows of data as a Markdown table."""


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
    raise NotImplementedError
