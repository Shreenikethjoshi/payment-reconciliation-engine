import sys
from pathlib import Path
import pytest

# Ensure project root is in sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.database import get_engine, init_db

@pytest.fixture
def in_memory_engine():
    """Provides an isolated, in-memory SQLite database engine."""
    engine = get_engine("sqlite:///:memory:")
    init_db(engine)
    return engine
