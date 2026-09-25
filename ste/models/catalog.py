"""Single source of truth for selectable models, menu labels, and token prices.

Every other component derives its model data from this module: server-side
validation uses :data:`ALLOWED_MODELS`, and the browser form builds its menu and
rough cost estimate from :func:`catalog_payload`, which ``/api/models`` serves.
Add, retire, or reprice a model here only; no other file lists identifiers.
"""

from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True)
class ModelSpec:
    """Describe one selectable OpenRouter model and its catalog price snapshot.

    Attributes:
        id: Exact OpenRouter model identifier sent upstream and persisted in records.
        label: Short human-readable menu label shown in the browser.
        input_usd_per_token: Base prompt-token price in US dollars.
        output_usd_per_token: Base completion-token price in US dollars.

    """

    id: str
    label: str
    input_usd_per_token: float
    output_usd_per_token: float

    @property
    def free(self) -> bool:
        """Return whether the listed base price has no per-token charge."""
        # Free OpenRouter variants list zero for both directions.
        return self.input_usd_per_token == 0 and self.output_usd_per_token == 0


# Date when every identifier and price below matched OpenRouter's live catalog.
# Update it whenever the catalog is re-verified, because prices can change.
PRICES_VERIFIED_ON = "2026-09-25"

# Selectable models in menu order; the first entry is the default selection.
# Exact identifiers (never moving aliases) keep experiment records reproducible,
# and historical persisted labels elsewhere must remain unchanged when this moves.
# The ``:free`` variants are zero-priced but rate limited by OpenRouter.
MODEL_CATALOG: tuple[ModelSpec, ...] = (
    # Paid models, verified with the model-price catalog on PRICES_VERIFIED_ON.
    ModelSpec("google/gemini-3.8-flash", "Gemini 3.8 Flash", 0.00000075, 0.00000375),
    ModelSpec("openai/gpt-5.6-luna", "GPT-5.6 Luna", 0.0000002, 0.0000012),
    ModelSpec("anthropic/claude-haiku-4.5", "Claude Haiku 4.5", 0.000001, 0.000005),
    ModelSpec("meta-llama/llama-4-maverick", "Llama 4 Maverick", 0.0000001875, 0.0000006525),
    # Free variants have zero token prices but stricter provider request limits.
    ModelSpec("nvidia/nemotron-3-ultra-550b-a55b:free", "Nemotron 3 Ultra (free)", 0.0, 0.0),
    ModelSpec("nvidia/nemotron-3.5-lightning:free", "Nemotron 3.5 Lightning (free)", 0.0, 0.0),
    ModelSpec("nvidia/nemotron-3-super-120b-a12b:free", "Nemotron 3 Super (free)", 0.0, 0.0),
    ModelSpec("thinkingmachines/inkling:free", "Inkling (free)", 0.0, 0.0),
    ModelSpec("qwen/qwen3.8-27b:free", "Qwen3.8 27B (free)", 0.0, 0.0),
    ModelSpec("google/gemma-4-26b-a4b-it:free", "Gemma 4 26B A4B (free)", 0.0, 0.0),
    ModelSpec("google/gemma-4-31b-it:free", "Gemma 4 31B (free)", 0.0, 0.0),
)

# Read-only lookup by identifier for validation and payload construction.
MODELS_BY_ID = MappingProxyType({spec.id: spec for spec in MODEL_CATALOG})

# Model policy is an explicit allow-list because callers supply paid credentials.
ALLOWED_MODELS = frozenset(MODELS_BY_ID)

# Completed 288-logical-unit studies anchor the browser's rough cost range.
# Each observation is normalized by its model's combined base price, then the
# browser rescales it by the selected model's price and selected workload.
COST_REFERENCE_LOGICAL_UNITS = 288
COST_OBSERVATIONS = MappingProxyType(
    {
        # The cheapest observed study sets the low end of the planning range.
        "low": ("meta-llama/llama-4-maverick", 0.08),
        # The most expensive observed study sets the high end of the range.
        "high": ("google/gemini-3.8-flash", 2.10),
    }
)


def catalog_payload() -> dict:
    """Return the JSON-safe browser catalog of models, prices, and cost anchors.

    The payload contains only public catalog facts, never credentials, so the
    browser can fetch it without authentication. Reference observations name
    catalog identifiers so their prices and labels are never restated elsewhere.

    Returns:
        A dictionary with ``verified_on``, ordered ``models``, and ``calibration``.

    """
    # Menu order is preserved because the first model is the default selection.
    models = [
        {
            "id": spec.id,
            "label": spec.label,
            "input": spec.input_usd_per_token,
            "output": spec.output_usd_per_token,
            "free": spec.free,
        }
        for spec in MODEL_CATALOG
    ]
    # Anchors carry labels for the disclosure text and ids for price lookup.
    observations = {
        bound: {"model": model_id, "label": MODELS_BY_ID[model_id].label, "usd": usd}
        for bound, (model_id, usd) in COST_OBSERVATIONS.items()
    }
    return {
        "verified_on": PRICES_VERIFIED_ON,
        "models": models,
        "calibration": {"logical_units": COST_REFERENCE_LOGICAL_UNITS, **observations},
    }
