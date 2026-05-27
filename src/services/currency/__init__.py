"""HTTP client for the standalone fontem-currency service.

CurrencyService used to live here as an in-process class loading
per-currency JSON files from disk. It moved to the dedicated
fontem-currency repo (single deployment in `currency-service` ns
serving every env over cluster DNS). Consumers now hit the HTTP
API via ``CurrencyClient``.
"""
from .client import CurrencyClient, ConversionResult

__all__ = ["CurrencyClient", "ConversionResult"]
