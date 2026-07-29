import uuid


def new_id(prefix: str) -> str:
    """Build a prefixed identifier, e.g. `sess_3f2a...`."""
    return f"{prefix}_{uuid.uuid4().hex}"
