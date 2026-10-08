"""Small helpers that hand out lists."""


def collect(value, bucket=None):
    """Append value to bucket and return bucket.

    Called without a bucket it starts from a fresh, empty list every time.
    """
    if bucket is None:
        bucket = []
    bucket.append(value)
    return bucket


class Group:
    """A named group of members."""

    def __init__(self, name, members=None):
        self.name = name
        self.members = members if members is not None else []

    def add(self, member):
        self.members.append(member)
        return self

    def __len__(self):
        return len(self.members)
