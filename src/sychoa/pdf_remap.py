from __future__ import annotations

import re
from dataclasses import dataclass, field

import fitz

from .models import ADDRESS_TYPES, AddressRecord


LOW_CONFIDENCE_THRESHOLD = 0.22


@dataclass
class PageMatch:
    old_page: int
    new_page: int
    confidence: float


@dataclass
class PdfRemapResult:
    page_map: dict[int, int]
    confidences: dict[int, float]
    boxes_remapped: int = 0
    extra_pages_remapped: int = 0
    type_pages_remapped: int = 0
    fallback_pages: list[int] = field(default_factory=list)

    @property
    def low_confidence_pages(self) -> list[int]:
        return sorted(
            page
            for page, confidence in self.confidences.items()
            if confidence < LOW_CONFIDENCE_THRESHOLD
        )


def remap_pdf_references(
    addresses: list[AddressRecord],
    extra_pages: dict[str, list[int]],
    type_extra_pages: dict[str, dict[str, list[int]]],
    pdf_id: str,
    old_doc: fitz.Document,
    new_doc: fitz.Document,
) -> PdfRemapResult:
    page_matches = build_page_matches(old_doc, new_doc)
    result = PdfRemapResult(
        page_map={match.old_page: match.new_page for match in page_matches},
        confidences={match.old_page: match.confidence for match in page_matches},
    )

    for address in addresses:
        for box in address.boxes:
            if box.pdf_id != pdf_id:
                continue
            old_page = box.page
            new_page = mapped_page(old_page, old_doc.page_count, new_doc.page_count, result)
            remap_box_to_page(box, old_doc, new_doc, old_page, new_page)
            result.boxes_remapped += 1

    if pdf_id in extra_pages:
        before = list(extra_pages[pdf_id])
        extra_pages[pdf_id] = remap_page_list(before, old_doc.page_count, new_doc.page_count, result)
        result.extra_pages_remapped += len(before)

    for address_type in ADDRESS_TYPES:
        type_pages = type_extra_pages.setdefault(address_type, {})
        if pdf_id not in type_pages:
            continue
        before = list(type_pages[pdf_id])
        type_pages[pdf_id] = remap_page_list(before, old_doc.page_count, new_doc.page_count, result)
        result.type_pages_remapped += len(before)

    return result


def build_page_matches(old_doc: fitz.Document, new_doc: fitz.Document) -> list[PageMatch]:
    if old_doc.page_count == 0 or new_doc.page_count == 0:
        return []

    old_signatures = [page_signature(old_doc[index]) for index in range(old_doc.page_count)]
    new_signatures = [page_signature(new_doc[index]) for index in range(new_doc.page_count)]
    scores = [
        [page_similarity(old_signatures[old_index], new_signatures[new_index]) for new_index in range(new_doc.page_count)]
        for old_index in range(old_doc.page_count)
    ]
    return align_pages(scores)


def page_signature(page: fitz.Page) -> tuple[frozenset[str], tuple[float, float]]:
    words = frozenset(re.findall(r"[a-z0-9]+", page.get_text("text").casefold()))
    rect = page.rect
    return words, (round(rect.width, 1), round(rect.height, 1))


def page_similarity(
    old_signature: tuple[frozenset[str], tuple[float, float]],
    new_signature: tuple[frozenset[str], tuple[float, float]],
) -> float:
    old_words, old_size = old_signature
    new_words, new_size = new_signature

    if old_words or new_words:
        union = old_words | new_words
        text_score = len(old_words & new_words) / len(union) if union else 0.0
    else:
        text_score = 0.0

    size_score = 0.0
    if old_size[0] > 0 and old_size[1] > 0 and new_size[0] > 0 and new_size[1] > 0:
        width_score = min(old_size[0], new_size[0]) / max(old_size[0], new_size[0])
        height_score = min(old_size[1], new_size[1]) / max(old_size[1], new_size[1])
        size_score = width_score * height_score

    return (text_score * 0.9) + (size_score * 0.1)


def align_pages(scores: list[list[float]]) -> list[PageMatch]:
    old_count = len(scores)
    new_count = len(scores[0]) if old_count else 0
    if not old_count or not new_count:
        return []

    gap_penalty = -0.25
    dp = [[0.0 for _ in range(new_count + 1)] for _ in range(old_count + 1)]
    move = [["" for _ in range(new_count + 1)] for _ in range(old_count + 1)]

    for old_index in range(1, old_count + 1):
        dp[old_index][0] = dp[old_index - 1][0] + gap_penalty
        move[old_index][0] = "old_gap"
    for new_index in range(1, new_count + 1):
        dp[0][new_index] = dp[0][new_index - 1] + gap_penalty
        move[0][new_index] = "new_gap"

    distance_base = max(old_count, new_count, 1)
    for old_index in range(1, old_count + 1):
        for new_index in range(1, new_count + 1):
            distance_penalty = 0.04 * abs((old_index - 1) - (new_index - 1)) / distance_base
            match_score = dp[old_index - 1][new_index - 1] + scores[old_index - 1][new_index - 1] - distance_penalty
            skip_old_score = dp[old_index - 1][new_index] + gap_penalty
            skip_new_score = dp[old_index][new_index - 1] + gap_penalty
            best_score = max(match_score, skip_old_score, skip_new_score)
            dp[old_index][new_index] = best_score
            if best_score == match_score:
                move[old_index][new_index] = "match"
            elif best_score == skip_old_score:
                move[old_index][new_index] = "old_gap"
            else:
                move[old_index][new_index] = "new_gap"

    matches: list[PageMatch] = []
    old_index = old_count
    new_index = new_count
    while old_index > 0 or new_index > 0:
        step = move[old_index][new_index]
        if step == "match":
            matches.append(
                PageMatch(
                    old_page=old_index - 1,
                    new_page=new_index - 1,
                    confidence=scores[old_index - 1][new_index - 1],
                )
            )
            old_index -= 1
            new_index -= 1
        elif step == "old_gap":
            old_index -= 1
        else:
            new_index -= 1

    matches.reverse()
    return matches


def mapped_page(old_page: int, old_count: int, new_count: int, result: PdfRemapResult) -> int:
    if old_page in result.page_map:
        return result.page_map[old_page]
    if new_count <= 0:
        return 0
    if old_count <= 1:
        fallback = 0
    else:
        fallback = round(old_page * max(new_count - 1, 0) / max(old_count - 1, 1))
    fallback = max(0, min(fallback, new_count - 1))
    result.page_map[old_page] = fallback
    result.confidences[old_page] = 0.0
    result.fallback_pages.append(old_page)
    return fallback


def remap_page_list(pages: list[int], old_count: int, new_count: int, result: PdfRemapResult) -> list[int]:
    return sorted(
        set(mapped_page(page, old_count, new_count, result) for page in pages if 0 <= page < old_count and new_count > 0)
    )


def remap_box_to_page(box, old_doc: fitz.Document, new_doc: fitz.Document, old_page: int, new_page: int) -> None:  # noqa: ANN001
    if not (0 <= old_page < old_doc.page_count) or not (0 <= new_page < new_doc.page_count):
        return

    old_rect = old_doc[old_page].rect
    new_rect = new_doc[new_page].rect
    scale_x = new_rect.width / old_rect.width if old_rect.width else 1.0
    scale_y = new_rect.height / old_rect.height if old_rect.height else 1.0

    left = min(box.x0, box.x1) * scale_x
    top = min(box.y0, box.y1) * scale_y
    right = max(box.x0, box.x1) * scale_x
    bottom = max(box.y0, box.y1) * scale_y

    box.page = new_page
    box.x0 = clamp(left, 0.0, new_rect.width)
    box.y0 = clamp(top, 0.0, new_rect.height)
    box.x1 = clamp(right, 0.0, new_rect.width)
    box.y1 = clamp(bottom, 0.0, new_rect.height)


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(value, high))
