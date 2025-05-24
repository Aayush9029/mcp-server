def test_imports():
    """Test that the package imports work correctly."""
    from task_management_mcp import server, models
    assert server is not None
    assert models is not None

def test_task_management_mcp_class():
    """Test that the main class can be imported."""
    from task_management_mcp import TaskManagementMCP
    assert TaskManagementMCP is not None

def test_main_function():
    """Test that the main function exists."""
    from task_management_mcp import main
    assert main is not None