EICAR_TEST_SIGNATURE = b"EICAR-STANDARD-ANTIVIRUS-TEST-FILE"


class UnsafeContentError(ValueError):
    pass


def validate_upload_content(content: bytes, *, label: str) -> None:
    if EICAR_TEST_SIGNATURE in content:
        raise UnsafeContentError(f"{label} failed content scan")
