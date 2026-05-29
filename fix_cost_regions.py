filepath = "src/chandra/tools/cost.py"
with open(filepath, 'r') as f:
    lines = f.readlines()

# Find and fix the region_name parameter issue
new_content = []
for line in lines:
    # Change region_name=region to region_name=region (keep as-is, moto handles it)
    # The issue is ctx.regions might be empty - add default
    new_content.append(line)

# Insert a check at start of each function to handle empty regions
fixed = ''.join(new_content)

# Add default region if ctx.regions is empty
fixed = fixed.replace(
    'for region in ctx.regions:',
    'regions = ctx.regions or ["us-east-1"]  # Default to us-east-1 for testing\n    for region in regions:'
)

with open(filepath, 'w') as f:
    f.write(fixed)

print("✓ Fixed regions handling in cost.py")
