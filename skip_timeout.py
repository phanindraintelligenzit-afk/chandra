filepath = "tests/unit/test_observability.py"
with open(filepath, 'r') as f:
    content = f.read()

# Add import at top
if "import sys" not in content:
    content = "import sys\n" + content

# Skip timeout test on Windows
content = content.replace(
    'def test_traced_node_timeout_sync',
    '@pytest.mark.skipif(sys.platform == "win32", reason="SIGALRM not available on Windows")\n    def test_traced_node_timeout_sync'
)

with open(filepath, 'w') as f:
    f.write(content)

print("✓ Skipped timeout test on Windows")
