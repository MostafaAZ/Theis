import re

from django.core.exceptions import ValidationError


class LettersAndNumbersValidator:
    """NFR-23: passwords must mix letters and numbers."""

    def validate(self, password, user=None):
        if not (re.search(r"[A-Za-z]", password) and re.search(r"\d", password)):
            raise ValidationError("The password must contain both letters and numbers.",
                                  code="password_letters_numbers")

    def get_help_text(self):
        return "Your password must contain both letters and numbers."
