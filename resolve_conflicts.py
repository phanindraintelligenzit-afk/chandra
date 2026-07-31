import os

def resolve_file(filepath, resolutions):
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()
    
    import re
    # Split by <<<<<<< HEAD ... ======= ... >>>>>>> commit
    chunks = re.split(r'<<<<<<< HEAD\n(.*?)\n=======\n(.*?)\n>>>>>>> [^\n]+\n', content, flags=re.DOTALL)
    
    expected_chunks = 1 + 3 * len(resolutions)
    if len(chunks) != expected_chunks:
        print(f'Error: Expected {len(resolutions)} conflicts in {filepath}, found {(len(chunks)-1)//3}')
        return
        
    new_content = chunks[0]
    for i in range(len(resolutions)):
        head_part = chunks[1 + i*3]
        main_part = chunks[2 + i*3]
        rest = chunks[3 + i*3]
        
        res = resolutions[i]
        if res == 'head':
            new_content += head_part + '\n'
        elif res == 'main':
            new_content += main_part + '\n'
        elif callable(res):
            new_content += res(head_part, main_part) + '\n'
        
        new_content += rest
        
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(new_content)
    print(f'Resolved {filepath}')

resolve_file('copilot_agents/graph.py', ['head'])
resolve_file('digitalworker_agents/analyzer_agent.py', ['head'])
resolve_file('digitalworker_agents/observation_agent.py', ['head'])
resolve_file('src/chandra/digital_worker/verifier.py', ['head'])
resolve_file('src/chandra/briefing/composer.py', ['head', 'main', 'head', 'head', 'main'])

# For config.py, we take main (which has more LLM variables and typed_execution_enabled)
resolve_file('src/chandra/config.py', ['main'])

# For .env.example, take head
resolve_file('.env.example', ['head'])

# For aws_execution_agent.py:
def merge_aws_2(head, main):
    return head + '\n' + main

def merge_aws_3(head, main):
    return head + '\n' + main

def merge_aws_4(head, main):
    # head checks kra_data, main checks _action_scope_text
    return """        kra_data = action.get("kraData")
        if kra_data and isinstance(kra_data, dict):
            aws_ctx = ""  # User already specified resources — no need for full inventory
            self.logger.info("Custom KRA payload detected — skipping full AWS context gathering")
        else:
""" + '\n'.join('    ' + line for line in main.splitlines()) + """
            aws_ctx = self._gather_aws_context(force_refresh=True, action_text=_action_scope_text) if not answers else \"\""""

resolve_file('digitalworker_agents/aws_execution_agent.py', ['head', merge_aws_2, merge_aws_3, merge_aws_4])

