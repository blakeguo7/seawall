"""Small helpers that hand out lists."""


def collect(value, bucket=[]):
    """Append value to bucket and return bucket.

    Called without a bucket it starts from a fresh, empty list every time.
    """
    bucket.append(value)
    return bucket


class Group:
    """A named group of members."""

    def __init__(self, name, members=[]):
        self.name = name
        self.members = members

    def add(self, member):
        self.members.append(member)
        return self

    def __len__(self):
        return len(self.members)
