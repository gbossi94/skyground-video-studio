"""Understanding a raw take and proposing an edit of it.

The split that matters: `audio` and `transcription` observe the source and decide
nothing; `takes` and `cut` decide, and record why; `invariants` is the set of
rules no plan may break whatever produced it; `adviser` is an optional opinion
that can only ever raise a question.

Nothing here removes a piece of video it cannot justify, and a plan with an
unanswered question cannot be applied.
"""

from skyground.analysis.cut import CutPolicy, plan_cut
from skyground.analysis.models import Analysis, CutPlan, Question, Word

__all__ = ["Analysis", "CutPlan", "CutPolicy", "Question", "Word", "plan_cut"]
