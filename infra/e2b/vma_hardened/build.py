"""Build a versioned candidate without moving the default template tag."""

import os

from e2b import Template, default_build_logger

from template import (
    TEMPLATE_CANDIDATE,
    TEMPLATE_CPU_COUNT,
    TEMPLATE_MEMORY_MB,
    template,
)


def main() -> None:
    if not os.environ.get("E2B_API_KEY"):
        raise RuntimeError("E2B_API_KEY is required")

    info = Template.build(
        template,
        TEMPLATE_CANDIDATE,
        cpu_count=TEMPLATE_CPU_COUNT,
        memory_mb=TEMPLATE_MEMORY_MB,
        on_build_logs=default_build_logger(),
    )
    print(f"built template_id={info.template_id} build_id={info.build_id}")


if __name__ == "__main__":
    main()

