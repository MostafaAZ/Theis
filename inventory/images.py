"""Product pictures for both applications.

An uploaded image is served as is. Without one, a simple illustration is
drawn from the product's unit (vial, bottle, bag, pack…) in its category
colour, so the catalogue never shows an empty box.
"""
from django.core.exceptions import ValidationError
from django.http import HttpResponse

CATEGORY_COLOURS = {
    "Vaccines": ("#2563eb", "#dbeafe"),
    "Veterinary Medicaments": ("#0f766e", "#ccfbf1"),
    "Feed Additives": ("#b45309", "#fef3c7"),
    "Nutritional Products": ("#7c3aed", "#ede9fe"),
    "Capital Equipment": ("#475569", "#e2e8f0"),
}

SHAPES = {
    "vial": '<rect x="128" y="62" width="64" height="20" rx="4" fill="{c}"/>'
            '<rect x="118" y="80" width="84" height="130" rx="18" fill="#fff" stroke="{c}" '
            'stroke-width="6"/><rect x="124" y="140" width="72" height="64" rx="12" '
            'fill="{c}" opacity=".25"/><rect x="130" y="110" width="60" height="22" rx="4" '
            'fill="{c}"/>',
    "ampoule": '<path d="M150 50 L170 50 L172 92 Q200 110 200 150 L200 200 Q200 215 185 215 '
               'L135 215 Q120 215 120 200 L120 150 Q120 110 148 92 Z" fill="#fff" '
               'stroke="{c}" stroke-width="6"/><rect x="126" y="160" width="68" height="48" '
               'rx="10" fill="{c}" opacity=".3"/>',
    "bottle": '<rect x="140" y="40" width="40" height="26" rx="5" fill="{c}"/>'
              '<path d="M146 66 L174 66 L174 86 Q210 100 210 140 L210 205 Q210 220 195 220 '
              'L125 220 Q110 220 110 205 L110 140 Q110 100 146 86 Z" fill="#fff" stroke="{c}" '
              'stroke-width="6"/><rect x="122" y="130" width="76" height="58" rx="8" '
              'fill="{c}"/>',
    "bag": '<path d="M100 70 Q160 55 220 70 L232 215 Q160 230 88 215 Z" fill="#fff" '
           'stroke="{c}" stroke-width="6"/><rect x="118" y="120" width="84" height="60" '
           'rx="8" fill="{c}"/><path d="M100 70 Q160 90 220 70" fill="none" stroke="{c}" '
           'stroke-width="4"/>',
    "pack": '<rect x="98" y="70" width="124" height="150" rx="12" fill="#fff" stroke="{c}" '
            'stroke-width="6"/><path d="M98 100 L222 100" stroke="{c}" stroke-width="5"/>'
            '<rect x="118" y="125" width="84" height="56" rx="8" fill="{c}"/>',
    "unit": '<rect x="70" y="120" width="180" height="16" rx="8" fill="{c}"/>'
            '<circle cx="100" cy="155" r="10" fill="{c}"/><circle cx="160" cy="155" r="10" '
            'fill="{c}"/><circle cx="220" cy="155" r="10" fill="{c}"/>'
            '<path d="M100 165 L100 185 M160 165 L160 185 M220 165 L220 185" stroke="{c}" '
            'stroke-width="5" stroke-linecap="round"/>',
}


def illustration_svg(product):
    colour, soft = CATEGORY_COLOURS.get(product.category.name, ("#1d4ed8", "#e8efff"))
    shape = SHAPES.get(product.unit, SHAPES["pack"]).format(c=colour)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 320 260">'
            f'<rect width="320" height="260" rx="18" fill="{soft}"/>'
            f'<g transform="translate(0 10)">{shape}</g></svg>')


def image_response(product):
    if product.image:
        name = product.image.name.lower()
        ctype = "image/png" if name.endswith(".png") else (
            "image/webp" if name.endswith(".webp") else "image/jpeg")
        with product.image.open("rb") as fh:
            resp = HttpResponse(fh.read(), content_type=ctype)
    else:
        resp = HttpResponse(illustration_svg(product), content_type="image/svg+xml")
    resp["Cache-Control"] = "max-age=300"
    return resp


def validate_image(f):
    """Accept real JPG, PNG or WebP images up to 5 MB."""
    if f.size > 5 * 1024 * 1024:
        raise ValidationError("Images must be 5 MB or less.")
    try:
        from PIL import Image

        img = Image.open(f)
        img.verify()
        fmt = img.format
    except Exception as exc:  # noqa: BLE001
        raise ValidationError("Upload a JPG, PNG or WebP image.") from exc
    finally:
        f.seek(0)
    if fmt not in ("JPEG", "PNG", "WEBP"):
        raise ValidationError("Upload a JPG, PNG or WebP image.")
