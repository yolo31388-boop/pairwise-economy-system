"""Pytest fixtures for the test suite.

The shipped test classes call ``self.assertIsNotNone`` without inheriting
from ``unittest.TestCase``. Inject the standard assertion methods at
collection time so the tests run as written (test files must not be
modified).
"""

import unittest


def pytest_pycollect_makeitem(collector, name, obj):
    if isinstance(obj, type) and name.startswith("Test"):
        if not issubclass(obj, unittest.TestCase) \
                and not hasattr(obj, "assertIsNotNone"):
            obj.assertIsNotNone = unittest.TestCase.assertIsNotNone
            obj.assertIsNone = unittest.TestCase.assertIsNone
