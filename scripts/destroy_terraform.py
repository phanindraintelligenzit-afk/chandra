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
    cmd = ["terraform", "destroy"]
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
            logger.info(f"Deleting sandbox folder: {folder_path}")
            import shutil
            try:
                shutil.rmtree(folder_path)
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
