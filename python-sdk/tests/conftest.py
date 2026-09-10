"""Suite-wide isolation: skills projection is on by default, so any test
that installs into a TANGRAM_HOME fixture would otherwise project skills
into the developer's REAL ~/.claude and ~/.codex. Point the agent homes at
a throwaway directory for the whole run."""

import os
import tempfile

os.environ.setdefault("TANGRAM_AGENT_HOME", tempfile.mkdtemp(prefix="tangram-test-agent-home-"))
