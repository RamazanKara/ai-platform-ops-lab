"""Make the gateway's tests runnable from any working directory.

The tests import the service as ``app`` and their shared helpers as ``tests.*``, which
resolve only when this service directory is on ``sys.path``. Running pytest from the
service directory gets that for free from rootdir detection; running it from the repo
root (the natural thing to type, and what CI matrix steps and editors tend to do) used
to die at collection instead. pytest imports this conftest before collecting anything
beneath it, so the path is set either way.
"""

import sys
from pathlib import Path

_SERVICE_DIR = str(Path(__file__).resolve().parent)
if _SERVICE_DIR not in sys.path:
    sys.path.insert(0, _SERVICE_DIR)
