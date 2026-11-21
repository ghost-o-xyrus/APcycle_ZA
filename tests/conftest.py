import json
import os
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from apflow.extract.routes import extraction_from_llm_json  # noqa: E402

AS_OF = date(2026, 11, 20)


class Oracle:
    """Stands in for the vision model: returns the ground truth as model JSON."""

    def __init__(self, truth: dict, fence: bool = False):
        self.truth, self.fence, self.calls = truth, fence, 0

    def complete_json(self, prompt, images):
        self.calls += 1
        d = {k: v for k, v in self.truth.items() if k not in ("layout", "scanned")}
        d["confidence"] = {k: 0.99 for k in d}  # deliberately over-confident: must be capped
        s = json.dumps(d)
        return f"```json\n{s}\n```" if self.fence else s


class Broken:
    def complete_json(self, prompt, images):
        raise TimeoutError("model timed out")


def ex_from_truth(t: dict, route="text", conf=0.95):
    """An Extraction exactly equal to the truth, for testing checks in isolation."""
    ex = extraction_from_llm_json(json.dumps({k: v for k, v in t.items() if k not in ("layout", "scanned")}))
    ex.route = route
    for f in ex.fields.values():
        if f.present:
            f.confidence = conf
    return ex


@pytest.fixture(scope="session")
def tmp_out(tmp_path_factory):
    return str(tmp_path_factory.mktemp("out"))
