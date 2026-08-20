"""Reply engine: AI reads and writes, deterministic code decides and checks.

Layer 0  thread.py   make the thread record trustworthy (order, telemetry)
Layer 1  thread.py   resolve thread state
Layer 2  extract.py  structured read - the only place unbounded variety lives
Layer 3  decide.py   priority-ordered decision table
Layer 4  compose.py  constrained writer
Layer 5  critic.py   deterministic referee

See ``docs/reply_engine_design.md`` for the rationale and the validation
against the 2026-08-07 backlog.
"""

from .context import (
    APPLY_NOW,
    CREATE_WEDGE,
    NOT_ACTIONABLE,
    PIPELINE_SIGNAL,
    CompanyFacts,
    company_facts,
    requisition_actionability,
    requisition_state,
    resolve_capability,
    select_ask,
    silent_intel_allowed,
)
from .critic import CriticResult, review
from .decide import decide
from .extract import deterministic_read, read_thread
from .models import (
    Action,
    Ask,
    Capability,
    Decision,
    NamedPerson,
    ReplyDraft,
    ThreadRead,
    ThreadState,
)
from .pipeline import ThreadInput, run, summarize
from .proof import ProofBeat, load_proof_beats, select_proof_beats
from .reopen import (
    ReopenAssessment,
    check_reopen_conditions,
    evaluate_reopen_conditions,
    persist_reopen_conditions,
)
from .thread import Message, is_telemetry, order_messages, resolve_state
from .touches import (
    DEFAULT_TOUCH_CAP,
    TRIGGERED_TOUCH_CAP,
    inbound_probably_missing,
    outbound_is_purely_responsive,
    outbound_followup_touch_counts,
)

__all__ = [
    "Action",
    "APPLY_NOW",
    "Ask",
    "Capability",
    "CompanyFacts",
    "CREATE_WEDGE",
    "CriticResult",
    "Decision",
    "DEFAULT_TOUCH_CAP",
    "Message",
    "NamedPerson",
    "NOT_ACTIONABLE",
    "PIPELINE_SIGNAL",
    "ProofBeat",
    "ReopenAssessment",
    "ReplyDraft",
    "ThreadInput",
    "ThreadRead",
    "ThreadState",
    "TRIGGERED_TOUCH_CAP",
    "inbound_probably_missing",
    "company_facts",
    "check_reopen_conditions",
    "decide",
    "deterministic_read",
    "is_telemetry",
    "evaluate_reopen_conditions",
    "load_proof_beats",
    "order_messages",
    "outbound_followup_touch_counts",
    "outbound_is_purely_responsive",
    "persist_reopen_conditions",
    "read_thread",
    "requisition_actionability",
    "requisition_state",
    "resolve_capability",
    "resolve_state",
    "review",
    "run",
    "select_ask",
    "silent_intel_allowed",
    "select_proof_beats",
    "summarize",
]
