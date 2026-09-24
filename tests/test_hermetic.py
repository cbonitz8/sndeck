"""The suite must never read the user's sndeck config or touch real scratch."""
import os
import shutil

import pytest

from conftest import _REAL_HOME
from sndeck.settings import load_sndeck_config, resolve_scratch


def test_scratch_resolves_outside_real_home():
    scratch = resolve_scratch([], os.environ, load_sndeck_config(), os.getcwd())
    assert not os.path.realpath(scratch).startswith(str(_REAL_HOME) + os.sep)


def test_rmtree_under_real_scratch_is_refused():
    probe = _REAL_HOME / ".sn-scratch" / "__sndeck_guard_probe__"
    with pytest.raises(AssertionError, match="real sndeck data"):
        shutil.rmtree(probe)
