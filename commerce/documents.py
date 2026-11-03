"""Printable order documents: packing slip (FR-101) and credit note (FR-100)."""
import io

from django.utils import timezone


def _pdf(title, intro, headers, rows, footer_lines):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, title=title, leftMargin=16 * mm,
                            rightMargin=16 * mm, topMargin=16 * mm, bottomMargin=16 * mm)
    st = getSampleStyleSheet()
    story = [Paragraph("Kubbar International", st["Heading2"]), Paragraph(title, st["Title"])]
    story += [Paragraph(line, st["Normal"]) for line in intro]
    story.append(Spacer(1, 10))
    table = Table([headers] + rows, repeatRows=1)
    table.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.3, colors.grey),
                               ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E8EEF5")),
                               ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                               ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story.append(table)
    story.append(Spacer(1, 12))
    story += [Paragraph(line, st["Normal"]) for line in footer_lines]
    doc.build(story)
    return buf.getvalue()


def packing_slip_pdf(order):
    """Lot-level record of what left the warehouse, for traceability."""
    rows = []
    for line in order.lines.select_related("batch__product"):
        if not line.active_qty:
            continue
        b = line.batch
        rows.append([b.product.name, b.product.sku, b.lot_number, str(b.expiry_date),
                     b.get_storage_condition_display(), f"{line.active_qty} {b.product.unit}"])
    buyer = order.buyer
    intro = [f"<b>Order</b> {order.reference}"
             + (f" (replacement for {order.replacement_for.reference})"
                if order.replacement_for_id else ""),
             f"<b>Deliver to</b> {buyer.company_name}, {order.delivery_address}",
             f"<b>Contact</b> {buyer.contact_person} · {buyer.phone}",
             f"<b>Dispatched</b> {timezone.localtime(order.dispatched_at):%d %b %Y %H:%M}"
             if order.dispatched_at else "<b>Status</b> " + order.get_status_display()]
    footer = ["Check every lot number and expiry date on receipt.",
              "Cold-chain items: store at 2–8°C immediately." if order.has_cold_chain else "",
              "Received by (name, signature, date): ______________________________"]
    return _pdf("Packing Slip", intro, ["Product", "SKU", "Lot", "Expiry", "Storage", "Qty"],
                rows, [f for f in footer if f])


def credit_note_pdf(note):
    inv = note.invoice
    intro = [f"<b>Credit note</b> {note.number} · issued "
             f"{timezone.localtime(note.issued_at):%d %b %Y}",
             f"<b>Customer</b> {note.buyer.company_name}, commercial register "
             f"{note.buyer.commercial_register_no}, tax card {note.buyer.tax_card_no}",
             f"<b>Against invoice</b> {inv.number} for order {note.order.reference}"]
    rows = [[note.reason, f"{note.amount:,.2f} EGP"]]
    footer = [f"<b>Status:</b> {note.get_status_display()}"
              + (f" on {timezone.localtime(note.refunded_at):%d %b %Y} (ref "
                 f"{note.refund_reference})" if note.refunded_at else "")]
    return _pdf("Credit Note", intro, ["Reason", "Amount"], rows, footer)
