"""Builds synthetic e-paper PDFs for tests (and for trying the pipeline without real newspapers).

Each page has a masthead, a section folio, articles laid out as: headline across N columns, byline,
then body text split over the N columns. One article per paper may be "continued on page K".
The same real-world stories are written differently per paper so clustering has something to find.
"""
from __future__ import annotations

import io
import os
import textwrap
from pathlib import Path

import pymupdf as fitz
from PIL import Image

W, H = 842, 1190
MARGIN = 36
COLS = 4
GUTTER = 12
COL_W = (W - 2 * MARGIN - (COLS - 1) * GUTTER) / COLS
DEVANAGARI_FONT = "/System/Library/Fonts/Supplemental/DevanagariMT.ttc"


def col_x(c: int) -> float:
    return MARGIN + c * (COL_W + GUTTER)


def _photo_png() -> bytes:
    im = Image.new("RGB", (200, 120), (120, 130, 150))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def _fonts(page, hindi: bool):
    if hindi and os.path.exists(DEVANAGARI_FONT):
        page.insert_font(fontname="hin", fontfile=DEVANAGARI_FONT)
        return {"body": "hin", "head": "hin", "by": "hin"}
    return {"body": "tiro", "head": "hebo", "by": "heit"}


def _wrap_cols(text: str, ncols: int, chars_per_line: int, lines_per_col: int) -> list[str]:
    lines = textwrap.wrap(text, chars_per_line)
    per = max(1, -(-len(lines) // ncols))
    return ["\n".join(lines[i * per:(i + 1) * per]) for i in range(ncols)]


def draw_article(page, fonts, y, col, span, headline, byline, body, head_size=22, body_size=9,
                 photo=False, caption=None, kicker=None, hindi=False) -> float:
    """Draws one article; returns the y below it."""
    x0 = col_x(col)
    width = span * COL_W + (span - 1) * GUTTER
    if kicker:
        page.insert_textbox(fitz.Rect(x0, y, x0 + width, y + 30), kicker, fontsize=8, fontname=fonts["by"])
        y += 14
    head_chars = max(8, int(width / (head_size * 0.52)))
    head_lines = textwrap.wrap(headline, head_chars)
    hh = len(head_lines) * (head_size + 4)
    page.insert_textbox(fitz.Rect(x0, y, x0 + width, y + hh * 2 + 20), "\n".join(head_lines), fontsize=head_size,
                        fontname=fonts["head"])
    y += hh + 6
    if byline:
        page.insert_textbox(fitz.Rect(x0, y, x0 + width, y + 30), byline, fontsize=8.5, fontname=fonts["by"])
        y += 16
    if photo:
        page.insert_image(fitz.Rect(x0, y, x0 + COL_W, y + 70), stream=_photo_png())
        y += 72
        if caption:
            page.insert_textbox(fitz.Rect(x0, y, x0 + COL_W, y + 24), caption, fontsize=6.5, fontname=fonts["by"])
            y += 26
    cpl = int(COL_W / (body_size * (0.5 if not hindi else 0.62)))
    est_lines = max(4, -(-len(body) // (cpl * span)) + 1)
    cols = _wrap_cols(body, span, cpl, est_lines)
    line_h = body_size * 1.3
    maxlines = 0
    for i, chunk in enumerate(cols):
        n = chunk.count("\n") + 1
        maxlines = max(maxlines, n)
        page.insert_textbox(fitz.Rect(col_x(col + i), y, col_x(col + i) + COL_W, y + n * line_h * 2 + 20), chunk,
                            fontsize=body_size, fontname=fonts["body"])
    return y + maxlines * line_h + 14


def draw_page(doc, paper_name, date_str, page_no, section, articles, hindi=False):
    page = doc.new_page(width=W, height=H)
    fonts = _fonts(page, hindi)
    page.insert_textbox(fitz.Rect(MARGIN, 10, W - MARGIN, 50), paper_name.upper(), fontsize=24, fontname="hebo",
                        align=1)
    page.insert_textbox(fitz.Rect(MARGIN, 50, W - MARGIN, 64), f"{date_str}   |   {section}   |   Page {page_no}",
                        fontsize=8, fontname="helv", align=1)
    page.draw_line((MARGIN, 66), (W - MARGIN, 66))
    y_by_col = [78.0] * COLS
    for a in articles:
        col, span = a["col"], a["span"]
        y = max(y_by_col[col:col + span])
        y2 = draw_article(page, fonts, y, col, span, a["headline"], a.get("byline"), a["body"],
                          head_size=a.get("head_size", 22), photo=a.get("photo", False), caption=a.get("caption"),
                          kicker=a.get("kicker"), hindi=hindi)
        for c in range(col, col + span):
            y_by_col[c] = y2
    page.insert_textbox(fitz.Rect(MARGIN, H - 30, W - MARGIN, H - 16), f"{paper_name} - {date_str} - {page_no}",
                        fontsize=7, fontname="helv", align=1)
    return page


# ----------------------------------------------------------------------------- content
DATE_LONG = "Monday, 2 November 2026"

STORY_RESIGN = {
    "the_hindu": ("Punjab MLA Kunwar Vijay Pratap Singh resigns from Assembly",
                  "By Staff Reporter",
                  "CHANDIGARH, Nov 2 (ANI): Kunwar Vijay Pratap Singh resigned from the Punjab Assembly on Monday, "
                  "becoming the third legislator to leave the Aam Aadmi Party in the State this year. He submitted "
                  "his resignation to the Speaker in Chandigarh. Party leaders said the loss would not affect the "
                  "government's majority. Opposition parties demanded that a by-election be held without delay. "
                  "Mr. Singh said he was quitting over differences with the party leadership on governance issues. "
                  "He did not say which party he would join next and added that he would speak to supporters first. "
                  "Observers said the exit adds to pressure on the AAP ahead of the next Assembly elections."),
    "toi": ("AAP loses another MLA as Vijay Pratap Singh quits Punjab Assembly",
            "By TNN",
            "CHANDIGARH: Kunwar Vijay Pratap Singh on Monday resigned as a member of the Punjab Assembly, the third "
            "AAP MLA to desert the party in the State this year, officials said. Singh handed his resignation letter "
            "to Speaker in Chandigarh. AAP leaders insisted the exit would not shake the government's majority in the "
            "House. Opposition parties demanded a by-election without delay. Singh cited differences with the AAP "
            "leadership over governance. He gave no hint on his next political move, saying he would first consult "
            "supporters. Analysts said the resignation piles pressure on the party before the next Assembly polls."),
    "indian_express": ("Third AAP MLA walks out: Vijay Pratap Singh resigns, Punjab by-election likely",
                       "By Express News Service",
                       "CHANDIGARH, NOVEMBER 2: Punjab MLA Kunwar Vijay Pratap Singh resigned from the Assembly on "
                       "Monday, the third AAP legislator to quit this year. The resignation was submitted to the "
                       "Speaker in Chandigarh. The AAP said its majority in the House remained intact. Opposition "
                       "parties called for a by-election at the earliest. Singh blamed differences with the party "
                       "leadership over governance and said he would consult his supporters before deciding his next "
                       "step. The exit adds to the pressure on the AAP ahead of the next Assembly elections."),
    "dainik_jagran": ("पंजाब विधानसभा से कुंवर विजय प्रताप सिंह ने दिया इस्तीफा",
                      "संवाददाता",
                      "चंडीगढ़, 2 नवंबर (एएनआई): कुंवर विजय प्रताप सिंह ने सोमवार को पंजाब विधानसभा से इस्तीफा दे दिया। "
                      "वह इस साल आम आदमी पार्टी छोड़ने वाले तीसरे विधायक हैं। उन्होंने अपना इस्तीफा चंडीगढ़ में स्पीकर को सौंपा। "
                      "पार्टी नेताओं ने कहा कि सरकार के बहुमत पर कोई असर नहीं पड़ेगा। विपक्ष ने तुरंत उपचुनाव कराने की मांग की।"),
}
STORY_RBI = {
    "the_hindu": ("RBI keeps repo rate unchanged at 6.5%",
                  "By Business Bureau",
                  "MUMBAI, Nov 2: The Reserve Bank of India on Monday kept the repo rate unchanged at 6.5 per cent, "
                  "saying inflation risks remained elevated. Governor Sanjay Malhotra said the Monetary Policy "
                  "Committee voted 5-1 to hold rates. Growth projection for the year was retained at 6.8 per cent. "
                  "Markets reacted with a modest rally in bank stocks after the announcement of the decision."),
    "toi": ("RBI holds repo rate at 6.5%, keeps growth forecast at 6.8%",
            "By Business Desk",
            "MUMBAI: The Reserve Bank of India held the repo rate steady at 6.5 per cent on Monday and said "
            "inflation risks remained elevated. Governor Sanjay Malhotra said the Monetary Policy Committee voted "
            "5-1 to keep rates unchanged. The central bank retained its growth projection at 6.8 per cent for the "
            "year. Bank stocks rallied modestly after the policy announcement, traders said."),
    "indian_express": ("MPC status quo: RBI leaves repo rate at 6.5 per cent",
                       "By Express News Service",
                       "MUMBAI, NOVEMBER 2: The RBI left the repo rate unchanged at 6.5 per cent on Monday, citing "
                       "elevated inflation risks. RBI Governor Sanjay Malhotra said the MPC voted 5-1 for the pause. "
                       "The growth forecast for the year stays at 6.8 per cent. Bank stocks gained after the policy."),
}
LOCAL = {
    "the_hindu": ("Metro line extension gets nod", "By City Staff",
                  "NEW DELHI, Nov 2: The Delhi government on Monday approved a nine-km extension of the metro line to "
                  "the airport corridor at a cost of Rs. 4,200 crore. Officials said work would begin early next year "
                  "and finish in three years. Residents welcomed the decision but asked for a clear timeline."),
    "toi": ("Delhi pollution: AQI slips to 'severe' as stubble smoke blows in", "By Environment Desk",
            "NEW DELHI: Air quality in Delhi slipped to the severe category on Monday with the AQI crossing 430 as "
            "smoke from stubble burning drifted into the city. Schools were told to hold classes online and "
            "construction work was banned. Doctors advised residents with breathing problems to stay indoors."),
    "indian_express": ("IPL: Mumbai Indians sign new coach ahead of auction", "By Sports Desk",
                       "MUMBAI, NOVEMBER 2: Mumbai Indians on Monday named a new head coach ahead of the player auction "
                       "next month. The franchise said the former international would take charge immediately. "
                       "Captain Rohit Sharma welcomed the appointment and said the squad was ready to start preparation."),
    "dainik_jagran": ("दिल्ली में प्रदूषण का स्तर खतरनाक", "संवाददाता",
                      "नई दिल्ली, 2 नवंबर: दिल्ली में सोमवार को वायु गुणवत्ता सूचकांक 430 के पार पहुंच गया। पराली के धुएं के कारण "
                      "हवा बेहद खराब श्रेणी में पहुंच गई। स्कूलों को ऑनलाइन कक्षाएं चलाने के निर्देश दिए गए हैं।"),
}


def build_paper(path: Path, paper_id: str, paper_name: str, hindi: bool = False, continued: bool = False,
                date_str: str = DATE_LONG) -> Path:
    doc = fitz.open()
    resign = STORY_RESIGN.get(paper_id)
    rbi = STORY_RBI.get(paper_id)
    local = LOCAL.get(paper_id)

    p1 = []
    if resign:
        h, by, body = resign
        if continued:
            cut = body.index("Mr. Singh said") if "Mr. Singh said" in body else len(body) // 2
            first, rest = body[:cut].strip(), body[cut:].strip()
            p1.append(dict(col=0, span=3, headline=h, byline=by, body=first + " (Continued on page 3)", photo=True,
                           caption="Kunwar Vijay Pratap Singh outside the Assembly."))
        else:
            p1.append(dict(col=0, span=3, headline=h, byline=by, body=body, photo=True,
                           caption="Kunwar Vijay Pratap Singh outside the Assembly."))
    if local:
        h, by, body = local
        p1.append(dict(col=3, span=1, headline=h, byline=by, body=body, head_size=14))
    draw_page(doc, paper_name, date_str, 1, "FRONT PAGE", p1, hindi=hindi)

    p2 = []
    if rbi:
        h, by, body = rbi
        p2.append(dict(col=0, span=3, headline=h, byline=by, body=body))
    draw_page(doc, paper_name, date_str, 2, "BUSINESS", p2, hindi=hindi)

    p3 = []
    if continued and resign:
        h, by, body = resign
        rest = body[body.index("Mr. Singh said"):]
        p3.append(dict(col=0, span=2, headline="Punjab MLA resigns (Contd.)", byline=None, body=rest,
                       kicker="CONTINUED FROM PAGE 1", head_size=14))
    elif hindi:
        p3.append(dict(col=0, span=2, headline="मौसम: शीत लहर जारी रहेगी", byline="संवाददाता",
                       body="मौसम विभाग ने कहा कि अगले तीन दिनों तक उत्तर भारत में शीत लहर जारी रहेगी। मैदानी इलाकों में "
                            "न्यूनतम तापमान दो डिग्री तक गिर सकता है।", head_size=14))
    else:
        p3.append(dict(col=0, span=2, headline="Weather: cold wave to continue", byline="By Staff",
                       body="The weather office said a cold wave would continue in north India over the next three days "
                            "with minimum temperatures dropping by two degrees across the plains and hills.", head_size=14))
    draw_page(doc, paper_name, date_str, 3, "NATION", p3, hindi=hindi)
    doc.save(path)
    doc.close()
    return path


PAPERS = [
    ("the_hindu", "The Hindu", False, True),
    ("toi", "The Times of India", False, False),
    ("indian_express", "The Indian Express", False, False),
    ("dainik_jagran", "Dainik Jagran", True, False),
]


def build_all(out_dir: Path, include_hindi: bool = True) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = {}
    for pid, name, hindi, cont in PAPERS:
        if hindi and (not include_hindi or not os.path.exists(DEVANAGARI_FONT)):
            continue
        paths[pid] = build_paper(out_dir / f"{pid}.pdf", pid, name, hindi=hindi, continued=cont)
    return paths


if __name__ == "__main__":
    import sys
    target = Path(sys.argv[1] if len(sys.argv) > 1 else "data/inbox")
    for k, v in build_all(target).items():
        print(k, v)
