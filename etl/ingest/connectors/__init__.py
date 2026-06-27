"""Exchange-specific ingestion connectors."""

from etl.ingest.connectors.bse_eod import BseEodConnector
from etl.ingest.connectors.nse_eod import NseEodConnector

__all__ = ["BseEodConnector", "NseEodConnector"]
