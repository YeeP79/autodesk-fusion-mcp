"""Defensive reads of Fusion API objects, duck-typed so fakes can drive them.

Cloud-backed properties and proxies of deleted entities raise on access, so a
report reads them through ``safe`` rather than failing on one bad member.
"""

from itertools import islice


def safe(obj, name, default=None):
    """Read an attribute; *default* when it raises or is missing."""
    try:
        return getattr(obj, name)
    except Exception:
        return default


def present(obj, name, default=None):
    """Like ``safe``, but a value of None also gives *default*."""
    value = safe(obj, name, default)
    return default if value is None else value


def attempt(getter, default=None):
    """Call *getter*; *default* when it raises. For reads chained through several objects."""
    try:
        return getter()
    except Exception:
        return default


def _indexed(collection):
    """True for a count/item collection. Vectors iterate but have neither."""
    count = safe(collection, "count")
    return isinstance(count, int) and callable(safe(collection, "item"))


def items(collection, limit=None):
    """Readable members of a collection, vector or list, up to *limit*.

    Fusion vectors such as the DataFileVector from
    ``personalUseLimits.editableFiles`` have len() and iteration but no
    count/item. Members that raise or are None are skipped, and anything that
    is not a collection has no members. For a report that lists what it can;
    use ``strict_items`` when a missing member would change the answer.
    """
    if collection is None:
        return []
    if _indexed(collection):
        found = []
        for i in range(collection.count if limit is None else min(collection.count, limit)):
            member = attempt(lambda: collection.item(i))
            if member is not None:
                found.append(member)
        return found
    try:
        return list(islice((member for member in collection if member is not None), limit))
    except Exception:
        return []


def strict_items(collection):
    """Every member of a collection, vector or list; a failed read raises.

    For callers that decide from the whole set: a document or body dropped
    without notice would make them answer wrongly. None members are left out.
    """
    if collection is None:
        return []
    members = ((collection.item(i) for i in range(collection.count)) if _indexed(collection)
               else iter(collection))
    return [member for member in members if member is not None]
