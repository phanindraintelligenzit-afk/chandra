import sys

def main():
    file_path = 'digitalworker_agents/aws_execution_agent.py'
    with open(file_path, 'r', encoding='utf-8') as f:
        content = f.read()

    old_str = """            aws_ctx = self._gather_aws_context(force_refresh=True) if not answers else ""

        self.logger.info("")
        self.logger.info("=" * 60)
        self.logger.info("               EXECUTION PIPELINE STARTED               ")
        self.logger.info("=" * 60)
        self.logger.info("Job ID           : %s", self.job_id)
        self.logger.info("Action           : %s", action.get("actionName", "Unknown"))
        self.logger.info("Service          : %s", action.get("service", "Unknown"))
        self.logger.info("KRA Code         : %s", action.get("kraCode", "None"))"""

    new_str = """            aws_ctx = ""
            aws_ctx = self._gather_aws_context(force_refresh=True) if not answers else ""

        self.logger.info("")
        self.logger.info("=" * 60)
        self.logger.info("               EXECUTION PIPELINE STARTED               ")
        self.logger.info("=" * 60)
        self.logger.info("Job ID           : %s", self.job_id)
        self.logger.info("Action           : %s", action.get("actionName", "Unknown"))
        self.logger.info("Service          : %s", action.get("service", "Unknown"))
        if action.get("action_type") == "AWS_TASK":
            self.logger.info("Task Type        : AWS_TASK")
        else:
            self.logger.info("KRA Code         : %s", action.get("kraCode", "None"))"""

    if old_str in content:
        content = content.replace(old_str, new_str)
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(content)
        print("Fixed successfully")
    else:
        print("Pattern not found!")

if __name__ == "__main__":
    main()
