import sys
from pathlib import Path

# Add the parent directory to the path
sys.path.insert(0, str(Path(__file__).parent.parent))

def test_imports():
    """Test that the package imports work correctly."""
    import server
    import models
    assert server is not None
    assert models is not None

def test_task_management_mcp_class():
    """Test that the main class can be imported."""
    from server import TaskManagementMCP
    assert TaskManagementMCP is not None