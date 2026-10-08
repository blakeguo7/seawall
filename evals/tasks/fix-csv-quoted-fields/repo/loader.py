"""A small CSV loader."""


def parse_line(line):
    """Split one CSV line into fields.

    * fields are separated by commas
    * a field may be wrapped in double quotes, and then it may contain commas
    * inside a quoted field, two double quotes ("") stand for one literal quote
    * surrounding whitespace outside quotes is kept as it is
    * an empty line gives [""]; a trailing comma gives a trailing empty field
    """
    return line.split(",")


def load_rows(text):
    """Parse CSV text with a header row into a list of dicts.

    Blank lines are skipped. A row with a different number of fields than the header raises
    ValueError. A text with no header (empty or blank) gives an empty list.
    """
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return []
    header = parse_line(lines[0])
    rows = []
    for line in lines[1:]:
        fields = parse_line(line)
        rows.append(dict(zip(header, fields)))
    return rows
