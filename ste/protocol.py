"""Authoritative, immutable definition of the STE retention protocol."""

from types import MappingProxyType

# These versions are persisted on every run and observation.  Schema changes
# describe representation; protocol and scoring changes describe semantics.
SCHEMA_VERSION = 2
PROTOCOL_VERSION = "ste-retention-2.1"
SCORING_VERSION = "mechanical-2.0"

# The complete instruction text is shared by the two detailed arms.  This is a
# practical subset of STE rules, not a claim of ASD-STE100 certification.
STE_RULES = (
    "Use only common approved words. Use one instruction per sentence. "
    "Keep procedural sentences to 20 words or fewer. "
    "Keep descriptive sentences to 25 words or fewer. Use the active voice. "
    "Use the present tense. Do not use noun clusters of more than three nouns."
)
VARIANTS = MappingProxyType(
    {
        "bare": "Write all replies in simplified technical English.",
        "rules": "Write all replies in simplified technical English. " + STE_RULES,
        "named": "Write all replies in ASD-STE100 Simplified Technical English.",
        "named_rules": (
            "Write all replies in ASD-STE100 Simplified Technical English. " + STE_RULES
        ),
    }
)
FACTORS = MappingProxyType(
    {"bare": (0, 0), "rules": (0, 1), "named": (1, 0), "named_rules": (1, 1)}
)

# This complete 32-item pool comes from the original experiment.  A stored seed
# and sequence identifier make selection reproducible across all four arms.
PROMPT_POOL = (
    "Explain how a centrifugal pump moves fluid.",
    "Describe the steps to replace a cabin air filter.",
    "Explain what causes cavitation in a hydraulic system.",
    "Describe how to inspect a drive belt for wear.",
    "Explain the purpose of a torque wrench and how to use one.",
    "Describe the steps to bleed air from a brake line.",
    "Explain how a thermostat regulates coolant flow.",
    "Describe how to test a battery with a multimeter.",
    "Explain why fasteners are tightened in a cross pattern.",
    "Describe the steps to clean a fuel injector.",
    "Explain how a check valve prevents backflow.",
    "Describe how to measure runout on a rotating shaft.",
    "Explain the function of a pressure relief valve.",
    "Describe the steps to align a coupling between two shafts.",
    "Explain how corrosion forms on dissimilar metals in contact.",
    "Describe how to inspect a weld for surface defects.",
    "Explain how a differential splits torque between two wheels.",
    "Describe the steps to purge a refrigerant line.",
    "Explain what a duty cycle means for a compressor.",
    "Describe how to set the gap on a spark plug.",
    "Explain how a strain gauge measures deformation.",
    "Describe the steps to replace a mechanical seal.",
    "Explain how a heat exchanger transfers energy.",
    "Describe how to verify the calibration of a pressure gauge.",
    "Explain why hydraulic fluid must be kept free of air.",
    "Describe the steps to remove a bearing from a shaft.",
    "Explain how a solenoid valve opens and closes.",
    "Describe how to inspect a hose for internal damage.",
    "Explain what causes a pump to lose prime.",
    "Describe the steps to torque a flange joint.",
    "Explain how a filter bypass valve protects a system.",
    "Describe how to measure the thickness of a brake disc.",
)

# Preview limits fit beneath the request deadline.  Research defaults are not
# subject to them and are consumed only by the asynchronous/CLI runner.
PREVIEW_MAX_BATCHES = 1
PREVIEW_MAX_TURNS = 3
PREVIEW_MAX_VARIANTS = 4
PREVIEW_MAX_WORK_UNITS = 12
PREVIEW_DEADLINE_SECONDS = 240.0
PREVIEW_PROVIDER_TIMEOUT_SECONDS = 9.0
DEFAULT_RESEARCH_DEPTHS = (1, 6, 12)
DEFAULT_SESSIONS_PER_BATCH = 6
DEFAULT_MAX_BATCHES = 6

# Model policy is an explicit allow-list because callers supply paid credentials.
# Exact OpenRouter identifiers keep each experiment reproducible as aliases move.
ALLOWED_MODELS = frozenset(
    {
        "anthropic/claude-sonnet-5",
        "openai/gpt-6-sol",
        "google/gemini-3.8-flash",
        "meta-llama/llama-4-maverick",
    }
)

# Required record fields define the durable analytical contract in one place.
RECORD_FIELDS = frozenset(
    {
        "schema_version",
        "protocol_version",
        "scoring_version",
        "run_mode",
        "run_id",
        "session_id",
        "model",
        "variant",
        "depth",
        "prompt_id",
        "prompt_sequence_id",
        "score",
        "metrics",
        "created_at",
    }
)
