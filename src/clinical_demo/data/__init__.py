"""Data-source adapters.

Each module here translates one external data source into the internal
`clinical_demo.domain` model. Nothing else in the codebase should import
from a source-specific module directly; downstream code consumes only
domain types.
"""
