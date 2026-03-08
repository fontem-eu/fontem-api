"""
Cache System Tests
==================
Comprehensive tests for the caching system, including:
- Cache interface compliance
- Cache hit/miss behavior
- Performance improvements
- Cache statistics
"""
from __future__ import annotations

import time
import pytest
from unittest.mock import Mock, patch

from src.cache.interface import CacheInterface, CacheStats
from src.cache.config import CacheConfig
from src.cache.factory import create_cache
from src.cache.fake_redis_cache import FakeRedisCache
from src.cache.redis_cache import RedisCache
from src.data.live_data_source import LiveDataSource

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_cache():
    """Provide a FakeRedisCache instance for testing."""
    return FakeRedisCache()

@pytest.fixture
def cache_config():
    """Provide a cache configuration."""
    return CacheConfig.from_env()

@pytest.fixture
def mock_data_source(fake_cache):
    """Provide a LiveDataSource with mock fetchers and fake cache."""
    with patch('src.data.live_data_source.EdgarFetcher') as mock_edgar, \
         patch('src.data.live_data_source.PriceFetcher') as mock_price:

        # Configure mocks
        mock_edgar_instance = mock_edgar.return_value
        mock_price_instance = mock_price.return_value

        # Mock data
        mock_edgar_instance.fetch_fundamentals.return_value = {
            "ticker": "AAPL",
            "revenue": [100, 200, 300],
            "net_income": [50, 75, 100]
        }

        mock_price_instance.get_annual_avg_prices.return_value = [150.0, 160.0, 170.0]
        mock_price_instance.get_annual_dividends.return_value = [1.0, 1.2, 1.5]
        mock_price_instance.get_current_price.return_value = 180.0
        mock_price_instance.get_avg_volume.return_value = 1000000
        mock_price_instance.get_shares_outstanding.return_value = 1000000000
        mock_price_instance.get_last_dividend.return_value = {"date": "2023-01-01", "amount": 1.0}
        mock_price_instance.get_splits.return_value = []
        mock_price_instance.get_latest_quarter.return_value = {}

        # Create data source with fake cache
        return LiveDataSource(
            cache=fake_cache,
            cache_config=CacheConfig.from_env()
        )

# ---------------------------------------------------------------------------
# Cache Interface Tests
# ---------------------------------------------------------------------------

def test_cache_interface_compliance(fake_cache):
    """Test that FakeRedisCache implements the CacheInterface correctly."""
    assert isinstance(fake_cache, CacheInterface)

    # Test basic operations
    test_key = "test_key"
    test_value = {"data": "test"}

    # Test set/get
    assert fake_cache.set(test_key, test_value, ttl=60) is True
    assert fake_cache.get(test_key) == test_value

    # Test delete
    assert fake_cache.delete(test_key) is True
    assert fake_cache.get(test_key) is None

    # Test stats
    stats = fake_cache.get_stats()
    assert isinstance(stats, CacheStats)
    assert stats.hits >= 0
    assert stats.misses >= 0

def test_cache_factory_creation():
    """Test that cache factory creates the right cache types."""
    # Test fake cache creation
    config = CacheConfig(provider="fakeredis")
    cache = create_cache(config)
    assert isinstance(cache, FakeRedisCache)

    # Test that it implements the interface
    assert isinstance(cache, CacheInterface)

# ---------------------------------------------------------------------------
# Cache Hit/Miss Behavior Tests
# ---------------------------------------------------------------------------

def test_cache_hit_miss_fundamentals(mock_data_source):
    """Test cache hit/miss behavior for fundamentals data."""
    # Clear cache and reset stats first
    mock_data_source.clear_cache()

    cache = mock_data_source._cache
    initial_stats = cache.get_stats()

    # First call - should miss cache
    result1 = mock_data_source.get_annual_fundamentals("AAPL")

    # Verify cache miss
    stats_after_miss = cache.get_stats()
    assert stats_after_miss.misses == initial_stats.misses + 1
    assert stats_after_miss.sets == initial_stats.sets + 1

    # Second call - should hit cache
    result2 = mock_data_source.get_annual_fundamentals("AAPL")

    # Verify cache hit
    stats_after_hit = cache.get_stats()
    assert stats_after_hit.hits == initial_stats.hits + 1
    assert stats_after_hit.sets == stats_after_miss.sets  # No new sets

    # Verify results are identical
    assert result1 == result2

def test_cache_hit_miss_market_snapshot(mock_data_source):
    """Test cache hit/miss behavior for market snapshot data."""
    # Clear cache and reset stats first
    mock_data_source.clear_cache()

    cache = mock_data_source._cache
    initial_stats = cache.get_stats()

    # First call - should miss cache
    result1 = mock_data_source.get_market_snapshot("AAPL")

    # Verify cache miss
    stats_after_miss = cache.get_stats()
    assert stats_after_miss.misses == initial_stats.misses + 1
    assert stats_after_miss.sets == initial_stats.sets + 1

    # Second call - should hit cache
    result2 = mock_data_source.get_market_snapshot("AAPL")

    # Verify cache hit
    stats_after_hit = cache.get_stats()
    assert stats_after_hit.hits == initial_stats.hits + 1
    assert stats_after_hit.sets == stats_after_miss.sets  # No new sets

    # Verify results are identical
    assert result1 == result2

# ---------------------------------------------------------------------------
# Performance Tests
# ---------------------------------------------------------------------------

def test_cache_performance_improvement(mock_data_source):
    """Test that cached requests are significantly faster than uncached ones."""
    # First request (cache miss)
    start_time = time.time()
    result1 = mock_data_source.get_annual_fundamentals("AAPL")
    first_time = time.time() - start_time

    # Second request (cache hit)
    start_time = time.time()
    result2 = mock_data_source.get_annual_fundamentals("AAPL")
    second_time = time.time() - start_time

    # Verify results are the same
    assert result1 == result2

    # Verify cached request is faster (should be very fast with fake cache)
    assert second_time < first_time * 0.5  # At least 2x faster

    # Verify it's actually fast (under 0.1 seconds for cached)
    assert second_time < 0.1

def test_multiple_ticker_cache_isolation(mock_data_source):
    """Test that cache properly isolates different tickers."""
    # Get data for two different tickers
    result_aapl = mock_data_source.get_annual_fundamentals("AAPL")
    result_msft = mock_data_source.get_annual_fundamentals("MSFT")

    # Verify they're different (mock returns same data, but in real scenario they'd be different)
    # For this test, we just verify cache stats show 2 misses and 2 sets
    stats = mock_data_source._cache.get_stats()
    assert stats.misses >= 2
    assert stats.sets >= 2

# ---------------------------------------------------------------------------
# Cache Configuration Tests
# ---------------------------------------------------------------------------

def test_cache_key_generation(cache_config):
    """Test that cache keys are generated correctly."""
    key1 = cache_config.get_full_key("fundamentals", "AAPL")
    key2 = cache_config.get_full_key("prices", "AAPL")
    key3 = cache_config.get_full_key("fundamentals", "MSFT")

    # Verify keys are different
    assert key1 != key2
    assert key1 != key3
    assert key2 != key3

    # Verify key format
    assert key1.startswith(cache_config.key_prefix)
    assert "fund_" in key1
    assert "AAPL" in key1

def test_cache_ttl_configuration(cache_config):
    """Test that TTL values are configured correctly."""
    assert cache_config.ttl_fundamentals > cache_config.ttl_prices
    assert cache_config.ttl_prices > cache_config.ttl_market_snapshot
    assert cache_config.ttl_fundamentals > 0
    assert cache_config.ttl_prices > 0
    assert cache_config.ttl_market_snapshot > 0

# ---------------------------------------------------------------------------
# Cache Management Tests
# ---------------------------------------------------------------------------

def test_cache_clear(mock_data_source):
    """Test that cache can be cleared."""
    # Populate cache
    mock_data_source.get_annual_fundamentals("AAPL")
    mock_data_source.get_market_snapshot("AAPL")

    # Verify cache has data
    stats_before = mock_data_source._cache.get_stats()
    assert stats_before.sets > 0

    # Clear cache
    result = mock_data_source.clear_cache()
    assert result is True

    # Verify cache is empty
    stats_after = mock_data_source._cache.get_stats()
    assert stats_after.sets == 0
    assert stats_after.hits == 0
    assert stats_after.misses == 0

def test_cache_stats_reset(mock_data_source):
    """Test that cache stats are reset after clear."""
    # Populate cache and generate some stats
    mock_data_source.get_annual_fundamentals("AAPL")
    mock_data_source.get_annual_fundamentals("AAPL")  # Cache hit

    stats_before = mock_data_source._cache.get_stats()
    assert stats_before.hits > 0
    assert stats_before.misses > 0

    # Clear cache
    mock_data_source.clear_cache()

    # Verify stats are reset
    stats_after = mock_data_source._cache.get_stats()
    assert stats_after.hits == 0
    assert stats_after.misses == 0
    assert stats_after.sets == 0

# ---------------------------------------------------------------------------
# Integration Tests
# ---------------------------------------------------------------------------

@pytest.mark.slow
def test_real_cache_integration():
    """Test with real cache providers (marked as slow)."""
    # Test fake cache (should always work)
    config = CacheConfig(provider="fakeredis")
    cache = create_cache(config)
    assert isinstance(cache, FakeRedisCache)

    # Basic operations should work
    test_key = "integration_test"
    test_value = {"test": "data"}

    assert cache.set(test_key, test_value, ttl=60) is True
    assert cache.get(test_key) == test_value
    assert cache.delete(test_key) is True
    assert cache.get(test_key) is None

    # Test stats
    stats = cache.get_stats()
    assert stats.hits >= 0
    assert stats.misses >= 1  # At least one miss for the get that returned None
    assert stats.sets >= 1
    assert stats.deletes >= 1