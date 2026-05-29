filepath = "tests/unit/test_observability.py"
with open(filepath, 'r') as f:
    lines = f.readlines()

# Find the line with 'def test_traced_node_timeout_sync'
new_lines = []
for i, line in enumerate(lines):
    if 'def test_traced_node_timeout_sync' in line and '@pytest.mark.skipif' not in lines[i-1]:
        # Insert decorator before this line
        indent = len(line) - len(line.lstrip())
        new_lines.append(' ' * indent + '@pytest.mark.skipif(sys.platform == "win32", reason="SIGALRM not available on Windows")\n')
    new_lines.append(line)

# Make sure sys is imported (check if already there)
content = ''.join(new_lines)
if 'import sys' not in content:
    # Find the line after 'from __future__ import annotations'
    content = content.replace(
        'from __future__ import annotations\n',
        'from __future__ import annotations\n\nimport sys\n',
        1
    )

with open(filepath, 'w') as f:
    f.write(content)

print("✓ Fixed test_observability.py")
