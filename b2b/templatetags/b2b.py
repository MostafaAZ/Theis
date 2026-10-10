from django import template

from b2b.i18n import translate

register = template.Library()


@register.simple_tag(takes_context=True)
def t(context, text):
    """Translate a UI string into the session language (NFR-04)."""
    return translate(text, context.get("LANG", "en"))


@register.filter
def tr(text, lang):
    return translate(str(text), lang)
