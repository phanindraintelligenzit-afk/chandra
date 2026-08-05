import pytest
import os
from pathlib import Path
from digitalworker_agents.aws_execution_agent import ExecutionAgents

def test_normalize_terraform_command():
    agent = ExecutionAgents(job_id="test-job-123")
    
    # Test 1: cd s3_bucket && terraform plan -out=tf.plan
    cmd, mod, reason = agent._normalize_terraform_command("cd s3_bucket && terraform plan -out=tf.plan")
    assert cmd == "terraform plan -out=tf.plan"
    assert mod is True
    assert reason == ""
    
    # Test 2: mkdir s3_bucket
    cmd, mod, reason = agent._normalize_terraform_command("mkdir s3_bucket")
    assert cmd == ""
    assert mod is True
    assert reason == ""
    
    # Test 3: pushd, popd
    cmd, mod, reason = agent._normalize_terraform_command("pushd . && terraform init ; popd")
    assert cmd == "terraform init"
    assert mod is True
    
    # Test 4: absolute path rejection (Windows)
    cmd, mod, reason = agent._normalize_terraform_command("terraform apply C:\\some\\path")
    assert cmd == ""
    assert mod is True
    assert "absolute path" in reason
    
    # Test 5: absolute path rejection (Unix)
    cmd, mod, reason = agent._normalize_terraform_command("terraform apply /some/path")
    assert cmd == ""
    assert mod is True
    assert "absolute path" in reason
    
    # Test 6: .. traversal rejection
    cmd, mod, reason = agent._normalize_terraform_command("terraform plan -var-file=../vars.tfvars")
    assert cmd == ""
    assert mod is True
    assert "traversal" in reason
    
    # Test 7: normal terraform commands (untouched)
    normal_cmds = [
        "terraform init",
        "terraform validate",
        "terraform plan -out=tf.plan",
        "terraform apply tf.plan",
        "terraform output -json"
    ]
    for n_cmd in normal_cmds:
        cmd, mod, reason = agent._normalize_terraform_command(n_cmd)
        assert cmd == n_cmd
        assert mod is False
        assert reason == ""
        
    # Test 8: Non-terraform executable steps
    cmd, mod, reason = agent._normalize_terraform_command("echo 'Hello World'")
    assert cmd == "echo 'Hello World'"
    assert mod is False
    assert reason == ""

def test_run_commands_sandbox_enforcement(tmp_path):
    agent = ExecutionAgents(job_id="test-job-456")
    
    commands = [
        {
            "command": "echo Hello",
            "working_dir": "some/child/dir",
            "order": 1
        }
    ]
    
    results, halted = agent._run_commands(commands, tmp_path, 30)
    
    assert len(results) == 1
    # working_dir is forcibly evaluated to the absolute path of tmp_path
    assert results[0]["working_dir"] == str(tmp_path.resolve())
    assert results[0]["command"] == "echo Hello"
