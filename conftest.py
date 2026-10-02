"""Pytest bootstrap.

The supplied test classes call ``self.assert*`` helpers without inheriting
from ``unittest.TestCase``. The test files must not be modified, so the
missing assertion helpers are injected here at collection time.
"""


def _assert_is_not_none(self, obj, msg=None):
    if obj is None:
        raise AssertionError(msg or "unexpectedly None")


def _assert_is_none(self, obj, msg=None):
    if obj is not None:
        raise AssertionError(msg or f"unexpectedly not None: {obj!r}")


def _assert_true(self, expr, msg=None):
    if not expr:
        raise AssertionError(msg or f"{expr!r} is not true")


def _assert_false(self, expr, msg=None):
    if expr:
        raise AssertionError(msg or f"{expr!r} is not false")


def _assert_equal(self, first, second, msg=None):
    if first != second:
        raise AssertionError(msg or f"{first!r} != {second!r}")


def _assert_not_equal(self, first, second, msg=None):
    if first == second:
        raise AssertionError(msg or f"{first!r} == {second!r}")


def _assert_in(self, member, container, msg=None):
    if member not in container:
        raise AssertionError(msg or f"{member!r} not in {container!r}")


def _assert_not_in(self, member, container, msg=None):
    if member in container:
        raise AssertionError(msg or f"{member!r} in {container!r}")


def _assert_is_instance(self, obj, cls, msg=None):
    if not isinstance(obj, cls):
        raise AssertionError(msg or f"{obj!r} is not instance of {cls!r}")


_HELPERS = {
    "assertIsNotNone": _assert_is_not_none,
    "assertIsNone": _assert_is_none,
    "assertTrue": _assert_true,
    "assertFalse": _assert_false,
    "assertEqual": _assert_equal,
    "assertNotEqual": _assert_not_equal,
    "assertIn": _assert_in,
    "assertNotIn": _assert_not_in,
    "assertIsInstance": _assert_is_instance,
}


def pytest_collection_modifyitems(items):
    patched = set()
    for item in items:
        cls = getattr(item, "cls", None)
        if cls is None or id(cls) in patched:
            continue
        patched.add(id(cls))
        for name, helper in _HELPERS.items():
            if not hasattr(cls, name):
                setattr(cls, name, helper)
