"""The content engine: everything that makes a Reel, and nothing that says it.

This package is the business half of Jarvis. It writes scripts, plans the
visuals, and - once the paid services are wired in - produces and publishes
the finished video. None of it runs on the voice path: the only thing the
conversation ever does is drop a job on the background worker and carry on
talking. See `workers.py` for why that separation is absolute.

The pieces:

* `styles`     - what "our style" means, as a file you edit rather than code.
* `models`     - a Reel script as a real object, not a wall of text.
* `store`      - jobs and assets, in the same SQLite file as everything else.
* `scriptwriter` - the agent that writes the script. Free by default.
* `pipeline`   - the stages from idea to published Reel, and which are ready.
* `tools`      - what the voice can ask for.
"""

from __future__ import annotations

__all__ = ["models", "pipeline", "scriptwriter", "store", "styles", "tools"]
