"""CDK app of team-assistant."""

import sys
from pathlib import Path

# The CDK app reads the agent's settings (config/agent.toml) with the agent's own loader, so
# both sides check them the same way. team_assistant.config uses only the standard library.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
