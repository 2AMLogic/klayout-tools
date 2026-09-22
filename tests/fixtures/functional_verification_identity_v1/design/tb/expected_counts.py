"""Declared testbench helper for the #2097 identity-contract fixtures.

``MULTIPLIER`` is the item-9 mutation point: editing this constant changes
the declared manifest AND flips the synthetic command's verdict. Omitting
this file from a disposable tree makes the command fail outright with an
ImportError -- the mandatory-helper control.
"""

MULTIPLIER = 1
