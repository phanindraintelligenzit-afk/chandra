filepath = "src/chandra/observability/__init__.py"
with open(filepath, 'r') as f:
    content = f.read()

# Add import for platform detection
if "import sys" not in content:
    content = "import sys\n" + content

# Skip timeout on Windows
content = content.replace(
    "def sync_wrapper(*args: Any, **kwargs: Any) -> Any:",
    """def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
                # Skip timeout on Windows (SIGALRM not available)
                if sys.platform == "win32":
                    timeout_s = None"""
)

with open(filepath, 'w') as f:
    f.write(content)

print("✓ Disabled timeout on Windows")
