const payload = {
  region: "us-east-1",
  kras: [
    { code: "kra-001", description: "Test KRA" }
  ],
  selected_kras: ["kra-001"],
  deployment: {
    role: "AWS Cloud Engineer",
    permissions: ["s3:GetObject"],
    agent_name: "Test Agent",
    employee_id: "E12345"
  },
  aws_tasks: ["c7a4ab48-a006-444e-a7fc-11bfb9b7e7ab"],
  aws_permissions: ["01d13c50-540d-466e-997f-aa805e969970"]
};

(async () => {
  console.log("Submitting AgentObservationsRequest...");
  const response = await fetch("http://localhost:8000/getAgentObservations", {
    method: "POST",
    headers: {
      "Content-Type": "application/json"
    },
    body: JSON.stringify(payload)
  });
  
  if (!response.ok) {
    const text = await response.text();
    console.error("HTTP Error", response.status, text);
    process.exit(1);
  }
  
  const data = await response.json();
  console.log("Response:", JSON.stringify(data, null, 2));
  
  // If it's an async job, we'd need to poll.
  if (data.job_id) {
    console.log("Got Job ID:", data.job_id);
    // Polling logic
    let status = "running";
    while (status === "running") {
      await new Promise(r => setTimeout(r, 2000));
      const pollResponse = await fetch(`http://localhost:8000/jobs/status/${data.job_id}`);
      const pollData = await pollResponse.json();
      console.log("Poll status:", pollData.status);
      if (pollData.status === "completed" || pollData.status === "failed") {
        console.log("Final Poll Data:", JSON.stringify(pollData, null, 2));
        break;
      }
    }
  }
})();
