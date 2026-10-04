from django import template

register = template.Library()


@register.filter
def get_item(mapping, key):
    """Look a key up in a dict from a template, tolerating missing keys."""
    if mapping is None:
        return None
    try:
        return mapping.get(key)
    except AttributeError:
        return None


@register.filter
def field_names(mapping):
    """Field names of a role definition, for the column-mapping selectors."""
    return list(mapping) if mapping else []