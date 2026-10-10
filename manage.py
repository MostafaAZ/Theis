#!/usr/bin/env python
"""Django command-line utility.

Defaults to the IMS settings. Run the B2B platform with
``--settings=kubbar.settings.b2b`` (see README).
"""
import os
import sys


def main():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "kubbar.settings.ims")
    from django.core.management import execute_from_command_line

    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
