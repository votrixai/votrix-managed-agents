"""The one thing in this suite that is not real.

A live web search returns something different every hour, which leaves nothing
to assert against. This returns a fixed sentence built out of tokens that exist
nowhere else, so a test can say "the search result reached the model" and mean
it.

Nothing else is stubbed. The sandbox, the skill unpack, the file round trip and
the model are all the real ones.
"""

from __future__ import annotations

from langchain_core.tools import StructuredTool

# Deliberately contains no URL. `web_fetch` is installed alongside `web_search`
# and is not implemented, so a result that invites a follow-up fetch would turn
# a passing test into an exception that has nothing to do with searching.
PINNED_ANSWER = (
    "AURORA-7 is a deep-space probe. It launched on 2031-04-12 and its mission "
    "director is Wen Ibarra."
)
PINNED_TOKEN = "AURORA-7"
PINNED_DATE = "2031-04-12"


def pinned_web_search() -> StructuredTool:
    """A drop-in for `app.runtime.tools.web_search_tool`.

    The signature and description are copied from the real one on purpose: the
    model is choosing whether to call this from the description, so changing it
    would change the behaviour being tested.
    """

    async def web_search(query: str, max_results: int = 5) -> str:
        """Search the web and return the top results."""
        return PINNED_ANSWER

    return StructuredTool.from_function(
        coroutine=web_search,
        name="web_search",
        description="Search the web and return the top results.",
    )
