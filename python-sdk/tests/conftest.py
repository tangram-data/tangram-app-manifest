"""Suite-wide isolation: skills projection is on by default, so any test
that installs into a TANGRAM_HOME fixture would otherwise project skills
into the developer's REAL ~/.claude and ~/.codex. Point the agent homes at
a throwaway directory for the whole run (unconditionally — an inherited
value could be a real location). This covers the pytest lane; unittest
discover does not load conftest, so store-installing tests ALSO set
TANGRAM_AGENT_HOME in their own setUp, beside TANGRAM_HOME."""

import os
import tempfile

os.environ["TANGRAM_AGENT_HOME"] = tempfile.mkdtemp(prefix="tangram-test-agent-home-")
