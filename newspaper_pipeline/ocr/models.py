from __future__ import annotations

from dataclasses import asdict, dataclass, field

# block kinds
HEADLINE, BYLINE, BODY, CAPTION, IMAGE, FURNITURE, KICKER = (
    "headline", "byline", "body", "caption", "image", "furniture", "kicker")


@dataclass
class Block:
    page_no: int
    bbox: tuple[float, float, float, float]      # x0, y0, x1, y1 in PDF points
    text: str = ""
    kind: str = BODY
    conf: float = 1.0                             # 1.0 for embedded text, engine confidence for OCR
    font_size: float = 0.0
    bold: bool = False
    n_lines: int = 1

    @property
    def x0(self): return self.bbox[0]
    @property
    def y0(self): return self.bbox[1]
    @property
    def x1(self): return self.bbox[2]
    @property
    def y1(self): return self.bbox[3]
    @property
    def cx(self): return (self.bbox[0] + self.bbox[2]) / 2
    @property
    def width(self): return self.bbox[2] - self.bbox[0]

    def to_dict(self) -> dict:
        d = asdict(self)
        d["bbox"] = [round(v, 1) for v in self.bbox]
        return d


@dataclass
class OcrLine:
    text: str
    bbox: tuple[float, float, float, float]       # pixels in the page image
    conf: float


@dataclass
class Article:
    paper_id: str
    page_no: int
    headline_block: Block | None = None
    blocks: list[Block] = field(default_factory=list)   # everything assigned to this article (not the headline)
    pages: list[int] = field(default_factory=list)
    continued_to: int | None = None
    continued_from: int | None = None
    flags: list[str] = field(default_factory=list)
