"""Generate two fictional English invoices for the demo.

Amounts are kept under the PayPal sandbox balance limit (JPY 100,000 per account)
so the payout can be sent for real in the demo. Run from the repository root.
"""
from reportlab.lib import colors
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from reportlab.lib.styles import ParagraphStyle

BASE = ParagraphStyle("base", fontName="Helvetica", fontSize=9.5, leading=13)
SMALL = ParagraphStyle("small", parent=BASE, fontSize=8.5, leading=11.5, textColor=colors.HexColor("#444444"))
TITLE = ParagraphStyle("title", parent=BASE, fontName="Helvetica-Bold", fontSize=22, leading=26)
H = ParagraphStyle("h", parent=BASE, fontName="Helvetica-Bold", fontSize=9.5)
ACCENT = colors.HexColor("#2F5D62")

VENDOR = (
    "<b>Maya Chen Design Studio</b><br/>"
    "1420 SE Hawthorne Blvd, Suite 3<br/>"
    "Portland, OR 97214, USA<br/>"
    "maya@chendesign-studio.example<br/>"
    "EIN: 00-0000000 (sample)"
)
CLIENT = (
    "<b>Hoshizora Works K.K.</b><br/>"
    "Attn: Accounts Payable<br/>"
    "Kita 5 Nishi 2, Chuo-ku<br/>"
    "Sapporo, Hokkaido 060-0005, Japan"
)


def money(v: float) -> str:
    return f"${v:,.2f}"


def build(path, number, issued, due, items, notes):
    doc = SimpleDocTemplate(path, pagesize=LETTER, leftMargin=18 * mm, rightMargin=18 * mm,
                            topMargin=16 * mm, bottomMargin=16 * mm)
    story = []

    head = Table(
        [[Paragraph(VENDOR, BASE), Paragraph("INVOICE", TITLE)]],
        colWidths=[110 * mm, 70 * mm],
    )
    head.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("ALIGN", (1, 0), (1, 0), "RIGHT")]))
    story += [head, Spacer(1, 8 * mm)]

    meta = Table(
        [
            [Paragraph("BILL TO", H), Paragraph("Invoice No.", H), Paragraph(number, BASE)],
            [Paragraph(CLIENT, BASE), Paragraph("Issue Date", H), Paragraph(issued, BASE)],
            ["", Paragraph("Due Date", H), Paragraph(due, BASE)],
            ["", Paragraph("Currency", H), Paragraph("USD", BASE)],
        ],
        colWidths=[100 * mm, 35 * mm, 45 * mm],
    )
    meta.setStyle(TableStyle([
        ("SPAN", (0, 1), (0, 3)),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]))
    story += [meta, Spacer(1, 8 * mm)]

    rows = [[Paragraph(t, ParagraphStyle("th", parent=H, textColor=colors.white))
             for t in ("Description", "Qty", "Unit Price", "Amount")]]
    total = 0.0
    for desc, qty, unit in items:
        amt = qty * unit
        total += amt
        rows.append([Paragraph(desc, BASE), f"{qty:g}", money(unit), money(amt)])
    rows.append(["", "", Paragraph("<b>Subtotal</b>", BASE), money(total)])
    rows.append(["", "", Paragraph("<b>Sales Tax</b>", BASE), money(0)])
    rows.append(["", "", Paragraph("<b>Total Due</b>", BASE),
                 Paragraph(f"<b>{money(total)}</b>", ParagraphStyle("r", parent=BASE, alignment=2))])

    n = len(items)
    t = Table(rows, colWidths=[100 * mm, 18 * mm, 30 * mm, 32 * mm])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), ACCENT),
        ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
        ("FONTSIZE", (0, 1), (-1, -1), 9.5),
        ("ALIGN", (1, 1), (-1, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LINEBELOW", (0, 1), (-1, n), 0.4, colors.HexColor("#CCCCCC")),
        ("LINEABOVE", (2, n + 3), (-1, n + 3), 0.8, colors.black),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    story += [t, Spacer(1, 10 * mm)]

    story.append(Paragraph("PAYMENT", H))
    story.append(Paragraph(
        "Please pay via PayPal to maya@chendesign-studio.example. Payment terms: Net 30.", BASE))
    story.append(Spacer(1, 5 * mm))
    story.append(Paragraph("NOTES", H))
    for line in notes:
        story.append(Paragraph(line, BASE))
    story.append(Spacer(1, 12 * mm))
    story.append(Paragraph("Thank you for your business.", SMALL))
    story.append(Paragraph("This is a fictional sample invoice created for software testing.", SMALL))

    doc.build(story)


build(
    "samples/invoice_A_service_only.pdf",
    "MCDS-2026-041",
    "October 5, 2026",
    "November 4, 2026",
    [
        ("Website UI design work - landing page and pricing page layouts", 5, 75.00),
        ("Design review meetings (online)", 1, 75.00),
    ],
    [
        "All design work was performed remotely from Portland, Oregon, USA.",
        "Hourly rate: $75.00. Time log available on request.",
    ],
)

build(
    "samples/invoice_B_logo_copyright.pdf",
    "MCDS-2026-047",
    "October 19, 2026",
    "November 18, 2026",
    [
        ("Logo design - 3 initial concepts and 2 rounds of revisions", 1, 400.00),
        ("Assignment of copyright for final logo (exclusive, worldwide, perpetual)", 1, 200.00),
    ],
    [
        "Design work was performed remotely from Portland, Oregon, USA.",
        "Upon full payment, the designer assigns all copyright in the final logo to the client, "
        "including the right to register it as a trademark in Japan.",
    ],
)
print("done")
