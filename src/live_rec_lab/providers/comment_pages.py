"""Read a bounded comment window without advancing an uncertain checkpoint."""

from collections.abc import Callable
from dataclasses import dataclass

from . import CommentPage, ProviderComment


class CommentPageError(ValueError):
    """A provider page could not be confirmed."""


@dataclass(frozen=True)
class CommentBatch:
    comments: tuple[ProviderComment, ...]
    latest_id: str | None
    complete: bool
    reason: str | None = None


def collect_comment_pages(
    fetch: Callable[[int], CommentPage],
    *,
    previous_id: str | None,
    max_pages: int = 4,
) -> CommentBatch:
    if type(max_pages) is not int or not 1 <= max_pages <= 60:
        raise ValueError("invalid maximum number of comment pages")
    observed: dict[str, ProviderComment] = {}
    offset = 0
    expected_total = None
    for _ in range(max_pages):
        try:
            page = fetch(offset)
        except CommentPageError as error:
            return CommentBatch(tuple(observed.values()), previous_id, False, str(error))

        changed = offset > 0 and expected_total != page.total_count
        if offset == 0:
            expected_total = page.total_count
        reached = False
        for comment in page.comments:
            if comment.source_id is None:
                return CommentBatch(tuple(observed.values()), previous_id, False, "missing_comment_id")
            if comment.source_id == previous_id:
                reached = True
                break
            if comment.source_id in observed:
                changed = True
            observed.setdefault(comment.source_id, comment)
        if changed:
            return CommentBatch(tuple(observed.values()), previous_id, False, "pagination_shifted")
        offset += len(page.comments)
        if page.total_count is not None and offset > page.total_count:
            return CommentBatch(tuple(observed.values()), previous_id, False, "invalid_page_count")
        if reached or (previous_id is None and page.total_count is not None and offset == page.total_count):
            return CommentBatch(tuple(observed.values()), next(iter(observed), previous_id), True)
        if not page.comments or (page.total_count is not None and offset == page.total_count):
            reason = "previous_boundary_missing" if previous_id is not None else "unconfirmed_end"
            return CommentBatch(tuple(observed.values()), previous_id, False, reason)
    return CommentBatch(tuple(observed.values()), previous_id, False, "page_budget_exhausted")
