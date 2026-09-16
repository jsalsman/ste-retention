"""Installable components for the STE retention experiment."""

from ste.protocol import PROTOCOL_VERSION, SCORING_VERSION, SCHEMA_VERSION

# Exported versions let applications identify compatible persisted data without
# importing transport or provider modules.
__all__ = ["PROTOCOL_VERSION", "SCORING_VERSION", "SCHEMA_VERSION"]
