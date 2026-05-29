filepath = "tests/unit/test_observability.py"
with open(filepath, 'r') as f:
    content = f.read()

# Remove the incorrectly placed import sys at the top
content = content.replace("import sys\nfrom __future__", "from __future__")

# Add import sys after the future import (correct position)
content = content.replace(
    "from __future__ import annotations\n",
    "from __future__ import annotations\n\nimport sys\n"
)

# Skip timeout test on Windows
content = content.replace(
    "def test_traced_node_timeout_sync",
    "@pytest.mark.skipif(sys.platform == \"win32\", reason=\"SIGALRM not available on Windows\")\n    def test_traced_node_timeout_sync"
)

with open(filepath, 'w') as f:
    f.write(content)

print("✓ Fixed: import sys in correct position, skipped timeout test on Windows")
