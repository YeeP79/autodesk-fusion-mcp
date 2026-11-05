"""Read open documents. A snapshot ("info") is a dict built by the tool that needs it."""

import time

import adsk

from .api import strict_items


def open_documents(app):
    """Every open document, hidden ones included (a parent loads what it links)."""
    return strict_items(app.documents)


def data_file_id(info):
    """The DataFile id of a snapshot holding a ``data_file`` dict."""
    return (info.get("data_file") or {}).get("id")


def instances_of(infos, wanted, key=data_file_id):
    """Indexes of the open documents that are instances of file *wanted*.

    *key* reads a snapshot's file id. Nothing matches an empty id: unsaved
    documents have none and are not instances of each other.
    """
    if not wanted:
        return []
    return [i for i, info in enumerate(infos) if key(info) == wanted]


def pump():
    """Run Fusion's event loop once; returns how long that took."""
    started = time.monotonic()
    adsk.doEvents()
    return time.monotonic() - started
