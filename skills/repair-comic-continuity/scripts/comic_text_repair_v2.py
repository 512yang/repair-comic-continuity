"""Compatibility alias for the embedded text engine."""

import sys

import text_engine_pipeline as _implementation


if __name__ == "__main__":
    _implementation.main()
else:
    sys.modules[__name__] = _implementation
