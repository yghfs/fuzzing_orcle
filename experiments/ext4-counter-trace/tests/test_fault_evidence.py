"""Discoverable wrapper for the historical hyphenated test module."""
import pathlib
import runpy
import unittest


_source = pathlib.Path(__file__).with_name("test-fault-evidence.py")
_namespace = runpy.run_path(str(_source), run_name="fault_evidence_tests")
for _name, _value in _namespace.items():
    if isinstance(_value, type) and issubclass(_value, unittest.TestCase):
        _value.__module__ = __name__
        globals()[_name] = _value
