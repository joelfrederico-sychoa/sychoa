from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from PySide6.QtCore import QPointF, QRectF


ADDRESS_TYPES = ("A", "B", "C", "D", "E")


@dataclass
class BoxRecord:
    page: int
    x0: float
    y0: float
    x1: float
    y1: float
    pdf_id: str = ""
    id: str = field(default_factory=lambda: uuid.uuid4().hex)

    @property
    def rect(self) -> QRectF:
        return QRectF(
            QPointF(min(self.x0, self.x1), min(self.y0, self.y1)),
            QPointF(max(self.x0, self.x1), max(self.y0, self.y1)),
        )

    def to_json(self) -> dict:
        rect = self.rect
        return {
            "id": self.id,
            "pdf_id": self.pdf_id,
            "page": self.page,
            "x0": round(rect.left(), 3),
            "y0": round(rect.top(), 3),
            "x1": round(rect.right(), 3),
            "y1": round(rect.bottom(), 3),
        }

    @classmethod
    def from_json(cls, data: dict, default_pdf_id: str = "") -> "BoxRecord":
        return cls(
            id=str(data.get("id") or uuid.uuid4().hex),
            pdf_id=str(data.get("pdf_id") or default_pdf_id),
            page=int(data["page"]),
            x0=float(data["x0"]),
            y0=float(data["y0"]),
            x1=float(data["x1"]),
            y1=float(data["y1"]),
        )


@dataclass
class AddressRecord:
    label: str
    address_type: str = "A"
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    boxes: list[BoxRecord] = field(default_factory=list)

    def to_json(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "address_type": normalize_address_type(self.address_type),
            "boxes": [box.to_json() for box in self.boxes],
        }

    @classmethod
    def from_json(cls, data: dict, default_pdf_id: str = "") -> "AddressRecord":
        return cls(
            id=str(data.get("id") or uuid.uuid4().hex),
            label=str(data.get("label") or "Untitled"),
            address_type=normalize_address_type(str(data.get("address_type") or "A")),
            boxes=[BoxRecord.from_json(box, default_pdf_id) for box in data.get("boxes", [])],
        )


def normalize_address_type(address_type: str) -> str:
    normalized = address_type.strip().upper()
    return normalized if normalized in ADDRESS_TYPES else "A"


def normalize_page_map(page_map: dict[str, list[int]]) -> dict[str, list[int]]:
    return {
        str(pdf_id): sorted(set(int(page) for page in pages))
        for pdf_id, pages in page_map.items()
        if pdf_id and pages
    }


def page_map_from_json(raw_pages: object, default_pdf_id: str = "") -> dict[str, list[int]]:
    if isinstance(raw_pages, dict):
        return normalize_page_map(
            {
                str(pdf_id): list(pages) if isinstance(pages, list) else []
                for pdf_id, pages in raw_pages.items()
            }
        )
    if isinstance(raw_pages, list) and default_pdf_id:
        return {default_pdf_id: sorted(set(int(page) for page in raw_pages))}
    return {}
