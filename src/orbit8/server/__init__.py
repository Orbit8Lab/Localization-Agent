"""HTTP surface over the job controller.

A presentation layer, not a second implementation: every endpoint calls
the same `Job` methods the CLI calls. `Job.derive()` already answers
"what stage is this job in and what is next", so status is a computed
query here too — there is no server-side job table that can disagree
with the artifact tree.
"""
from .app import create_app

__all__ = ["create_app"]
