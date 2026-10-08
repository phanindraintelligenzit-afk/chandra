import argparse
import logging
import os
import subprocess
import sys
from pathlib import Path

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("terraform_destroy")

def main():
    parser = argparse.ArgumentParser(description="Run terraform destroy in a specified folder.")
    parser.add_argument("folderPath", help="Path to the folder containing Terraform configuration")
    parser.add_argument("--auto-approve", action="store_true", default=True, help="Automatically approve the destruction (default: True)")
    parser.add_argument("--no-auto-approve", action="store_false", dest="auto_approve", help="Prompt for approval before destroying")
    parser.add_argument("--jiraUrl", help="Jira URL to update after successful destruction", default=None)
    
    args = parser.parse_args()
    folder_path = Path(args.folderPath).resolve()
    
    if not folder_path.exists() or not folder_path.is_dir():
        logger.error(f"The directory '{folder_path}' does not exist.")
        sys.exit(1)
        
    logger.info(f"Target folder: {folder_path}")
    
    # Check if there are any .tf files in the directory
    tf_files = list(folder_path.glob("*.tf"))
    if not tf_files:
        logger.info("No .tf files found in the directory. Skipping terraform destroy.")
        sys.exit(0)

    logger.info("Running 'terraform destroy'...")
    
    # Strip prevent_destroy and enable force_destroy on resources to allow clean cleanup
    import re
    for tf_file in tf_files:
        try:
            content = tf_file.read_text(encoding="utf-8")
            new_content = re.sub(r'prevent_destroy\s*=\s*true', 'prevent_destroy = false', content)
            if 'force_destroy' in new_content:
                new_content = re.sub(r'force_destroy\s*=\s*false', 'force_destroy = true', new_content)
            else:
                new_content = re.sub(r'(resource\s+"aws_s3_bucket"\s+"[^"]+"\s*\{)', r'\1\n  force_destroy = true', new_content)
            if new_content != content:
                tf_file.write_text(new_content, encoding="utf-8")
                logger.info(f"Updated destroy settings in {tf_file.name}")
        except Exception as e:
            logger.warning(f"Could not process {tf_file.name} for destroy settings: {e}")

    import shutil
    tf_bin = os.environ.get("TERRAFORM_BIN")
    if not tf_bin:
        tf_bin = shutil.which("terraform")
    if not tf_bin:
        winget_path = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Links" / "terraform.exe"
        if winget_path.exists():
            tf_bin = str(winget_path)
    if not tf_bin:
        tf_bin = "terraform"

    proc_env = os.environ.copy()
    proc_env.setdefault("TF_IN_AUTOMATION", "1")
    proc_env.setdefault("TF_INPUT", "0")
    proc_env.setdefault("NO_COLOR", "1")

    # Set TF_PLUGIN_CACHE_DIR so terraform init finds pre-cached providers
    for cache_candidate in [
        folder_path.parent.parent.parent / ".terraform_cache",
        Path(__file__).resolve().parent.parent / ".terraform_cache",
        Path(os.getcwd()) / ".terraform_cache"
    ]:
        if cache_candidate.exists():
            proc_env.setdefault("TF_PLUGIN_CACHE_DIR", str(cache_candidate.resolve()))
            break

    # Redirect TMP and TEMP to project workspace drive to avoid running out of space
    for tmp_candidate in [
        folder_path.parent.parent.parent / "terraform_runs" / ".tmp",
        Path(__file__).resolve().parent.parent / "terraform_runs" / ".tmp"
    ]:
        try:
            tmp_candidate.mkdir(parents=True, exist_ok=True)
            proc_env.setdefault("TMP", str(tmp_candidate.resolve()))
            proc_env.setdefault("TEMP", str(tmp_candidate.resolve()))
            break
        except Exception:
            pass

    if "AWS_DEFAULT_REGION" in proc_env and "AWS_REGION" not in proc_env:
        proc_env["AWS_REGION"] = proc_env["AWS_DEFAULT_REGION"]
    elif "AWS_REGION" in proc_env and "AWS_DEFAULT_REGION" not in proc_env:
        proc_env["AWS_DEFAULT_REGION"] = proc_env["AWS_REGION"]

    # Seed lockfile if missing
    try:
        sys.path.append(str(Path(__file__).resolve().parent.parent))
        from src.chandra.execution.terraform import seed_lockfile_if_missing
        seed_lockfile_if_missing(folder_path)
    except Exception:
        pass

    # Ensure terraform is initialized so providers are present for destroy
    if not (folder_path / ".terraform").exists():
        logger.info("Initializing terraform in sandbox before destroy...")
        init_res = subprocess.run(
            [tf_bin, "init", "-backend=false", "-input=false", "-no-color"],
            cwd=str(folder_path),
            env=proc_env,
            capture_output=True,
            text=True,
            check=False
        )
        if init_res.returncode != 0:
            logger.warning(f"Terraform init output: {init_res.stderr or init_res.stdout}")
        else:
            logger.info("Terraform initialized successfully.")

    # Pre-clean S3 buckets so versioned objects don't block deletion
    try:
        state_file = folder_path / "terraform.tfstate"
        if state_file.exists():
            import json
            import boto3
            with open(state_file, "r", encoding="utf-8") as sf:
                state_data = json.load(sf)
            s3_client = boto3.client(
                "s3", 
                region_name=proc_env.get("AWS_REGION") or proc_env.get("AWS_DEFAULT_REGION") or "us-east-1"
            )
            for res in state_data.get("resources", []):
                if res.get("type") == "aws_s3_bucket":
                    for inst in res.get("instances", []):
                        b_name = inst.get("attributes", {}).get("bucket") or inst.get("attributes", {}).get("id")
                        if b_name:
                            logger.info(f"Emptying S3 bucket {b_name} before destroy...")
                            try:
                                paginator = s3_client.get_paginator('list_object_versions')
                                for page in paginator.paginate(Bucket=b_name):
                                    objects_to_delete = []
                                    for v in page.get('Versions', []):
                                        objects_to_delete.append({'Key': v['Key'], 'VersionId': v['VersionId']})
                                    for d in page.get('DeleteMarkers', []):
                                        objects_to_delete.append({'Key': d['Key'], 'VersionId': d['VersionId']})
                                    if objects_to_delete:
                                        s3_client.delete_objects(Bucket=b_name, Delete={'Objects': objects_to_delete})
                            except Exception as be:
                                logger.warning(f"Could not empty bucket {b_name} via boto3: {be}")
    except Exception as e:
        logger.debug(f"S3 pre-clean skipped: {e}")

    cmd = [tf_bin, "destroy", "-input=false", "-no-color"]
    if args.auto_approve:
        cmd.append("-auto-approve")
        
    try:
        # Stream the output so the user can see progress
        proc = subprocess.Popen(
            cmd,
            cwd=str(folder_path),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=proc_env,
            encoding="utf-8",
            errors="replace"
        )
        
        for line in proc.stdout:
            # Use rstrip to avoid double newlines since logging automatically adds one
            logger.info(line.rstrip('\n'))
            
        proc.wait()
        
        if proc.returncode != 0:
            logger.error(f"Terraform destroy failed with exit code {proc.returncode}.")
            sys.exit(proc.returncode)
        else:
            logger.info("Terraform destroy completed successfully.")
            
            # Delete Jira ticket completely as requested
            if args.jiraUrl:
                try:
                    sys.path.append(str(Path(__file__).resolve().parent.parent))
                    from tools.jira_tools.create_jira_ticket import delete_jira_ticket
                    
                    issue_key = args.jiraUrl.rstrip("/").split("/")[-1]
                    logger.info(f"Deleting Jira ticket {issue_key} upon infrastructure destruction...")
                    res = delete_jira_ticket(issue_key)
                    logger.info(f"Jira deletion result for {issue_key}: {res}")
                except Exception as e:
                    logger.warning(f"Failed to delete Jira ticket {args.jiraUrl}: {e}")

            logger.info(f"Deleting sandbox folder: {folder_path}")
            import shutil
            try:
                shutil.rmtree(folder_path, ignore_errors=True)
                logger.info("Sandbox folder deleted successfully.")
            except Exception as e:
                logger.error(f"Failed to delete sandbox folder: {e}")
            
    except FileNotFoundError:
        logger.error("'terraform' command not found. Please ensure Terraform is installed and in your PATH.")
        sys.exit(1)
    except Exception as e:
        logger.error(f"An unexpected error occurred: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
