filepath = "src/chandra/observability/__init__.py"
with open(filepath, 'r') as f:
    content = f.read()

# Add imports at the top
new_content = "import asyncio
import functools
" + content

with open(filepath, 'w') as f:
    f.write(new_content)

print("✓ Fixed imports in observability/__init__.py")
