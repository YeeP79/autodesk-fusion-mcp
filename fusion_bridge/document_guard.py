"""Read-only detection for the active document.

Personal Use licences open most documents read-only (``DataFile.isReadOnly``):
edits apply in the session but saves silently do nothing, and the API cannot
change the flag. Tools use this to warn instead of letting work be lost.
"""

from .dispatch import get_app

UNSAVED_READ_ONLY_WARNING = (
    "active document is read-only and has unsaved changes: these edits will not save"
)


def read_only_state(document):
    """Return True/False from ``document.dataFile.isReadOnly``, or None when unknown.

    Unknown covers a never-saved document (no dataFile) and a build where the
    property is missing or raises.
    """
    try:
        data_file = document.dataFile
    except Exception:
        return None
    if data_file is None:
        return None
    try:
        value = data_file.isReadOnly
    except Exception:
        return None
    return value if isinstance(value, bool) else None


def unsaved_read_only_warning(is_read_only, is_modified):
    """Decide the post-run warning. Only a confirmed read-only, modified doc warns."""
    if is_read_only is True and is_modified is True:
        return UNSAVED_READ_ONLY_WARNING
    return None


def active_document_warning():
    """Warning string for the active document, or None. Never raises."""
    try:
        document = get_app().activeDocument
        if document is None:
            return None
        return unsaved_read_only_warning(read_only_state(document), bool(document.isModified))
    except Exception:
        return None
