"""
API tests live in sibling modules, split by what they are about:

  test_endpoints.py — functional coverage of the endpoints themselves:
                      authentication required, patient scoping, read paths.
  test_security.py  — registration, privilege escalation, token handling.

This module is kept only so `manage.py test apps.api` keeps working for anyone
who types it out of habit; put new tests in one of the files above.
"""
