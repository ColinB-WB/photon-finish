"""Pure functional core: types + decisions. No I/O of any kind.

Import allowlist enforced by tests/test_architecture.py: stdlib data/math
modules only. Anything touching files, subprocesses, numpy/astropy, or display
text belongs in the shell.
"""
