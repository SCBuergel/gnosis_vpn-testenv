#!/usr/bin/env python3
"""Run the regression suite: run.py [--fast] [--very-fast] [--only tNN,tNN] [--skip tNN] [--run-id ID]
[--knob NAME=VALUE] [--runbook] [pytest args]. All options are pytest's (see conftest.py); this only fixes the
working directory and the test path so `just suite` and `just test` need no pytest knowledge."""
import os
import sys

here = os.path.dirname(os.path.abspath(__file__))
os.chdir(here)
os.execvp(sys.executable, [sys.executable, "-m", "pytest", "tests", *sys.argv[1:]])
