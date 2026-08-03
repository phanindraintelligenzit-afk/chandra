const puppeteer = require('puppeteer');

(async () => {
  const browser = await puppeteer.launch({ headless: 'new' });
  const page = await browser.newPage();
  
  try {
    console.log("Navigating to Onboarding...");
    await page.goto('http://localhost:3000/onboarding', { waitUntil: 'networkidle0' });
    
    console.log("Step 0: Worker Details");
    await page.waitForSelector('input[placeholder="ENTER AGENT NAME..."]', { timeout: 10000 });
    await page.type('input[placeholder="ENTER AGENT NAME..."]', 'Test Agent');
    // Select the first avatar
    const avatar = await page.$('.relative.cursor-pointer.rounded-full');
    if (avatar) await avatar.click();
    await page.click('button:has-text("CONTINUE TO ROLE")');
    
    console.log("Step 1: Role");
    await page.waitForSelector('button:has-text("CONTINUE TO MATURITY")');
    await page.click('button:has-text("CONTINUE TO MATURITY")');
    
    console.log("Step 2: Maturity");
    await page.waitForSelector('button:has-text("CONTINUE TO TASKS")');
    await page.click('button:has-text("CONTINUE TO TASKS")');
    
    console.log("Step 3: Monitoring/KRA");
    await page.waitForSelector('button:has-text("CONTINUE TO AWS TASKS")');
    // We need to select at least one KRA
    const kraCheckbox = await page.$('input[type="checkbox"]');
    if (kraCheckbox) await kraCheckbox.click();
    await page.click('button:has-text("CONTINUE TO AWS TASKS")');

    console.log("Step 4: AWS Tasks");
    await page.waitForSelector('button:has-text("CONTINUE TO AWS PERMISSIONS")');
    
    // Select an AWS Task
    const taskCheckbox = await page.$('input[type="checkbox"]');
    if (taskCheckbox) {
      await taskCheckbox.click();
      console.log("Selected an AWS Task");
    } else {
      console.log("No AWS Task found to select, attempting to create one");
      await page.click('button:has-text("Create Task")');
      await page.waitForSelector('input[placeholder="e.g. S3 Bucket Backup"]');
      await page.type('input[placeholder="e.g. S3 Bucket Backup"]', 'Test Task');
      await page.type('textarea[placeholder="Detailed instructions..."]', 'Test Description');
      await page.click('button:has-text("SAVE TASK")');
      await page.waitForTimeout(1000);
      const newTask = await page.$('input[type="checkbox"]');
      if (newTask) await newTask.click();
    }
    
    await page.click('button:has-text("CONTINUE TO AWS PERMISSIONS")');
    
    console.log("Step 5: AWS Permissions");
    await page.waitForSelector('button:has-text("DEPLOY DIGITAL EMPLOYEE")');
    
    // Select an AWS Permission
    const permCheckbox = await page.$('input[type="checkbox"]');
    if (permCheckbox) {
      await permCheckbox.click();
      console.log("Selected an AWS Permission");
    } else {
      console.log("No AWS Permission found, creating one");
      await page.click('button:has-text("Create Permission Set")');
      await page.waitForSelector('input[placeholder="e.g. S3 Read Only"]');
      await page.type('input[placeholder="e.g. S3 Read Only"]', 'Test Permission');
      await page.type('textarea[placeholder="Describe permissions..."]', 'Test Permission Description');
      // Click 'Select All' for actions
      await page.click('button:has-text("Select All")');
      await page.click('button:has-text("SAVE PERMISSIONS")');
      await page.waitForTimeout(1000);
      const newPerm = await page.$('input[type="checkbox"]');
      if (newPerm) await newPerm.click();
    }
    
    await page.click('button:has-text("DEPLOY DIGITAL EMPLOYEE")');
    
    console.log("Step 6: Deploying");
    const finalTarget = await page.waitForSelector('.text-emerald-400, .bg-emerald-500, :has-text("OBSERVATIONS & REASONING")', { timeout: 45000 });
    if (finalTarget) console.log("Deployment success message visible.");
    
    // Should navigate to dashboard eventually
    await page.waitForNavigation({ timeout: 60000 }).catch(() => console.log("Navigation timeout"));
    console.log("Navigated to:", page.url());
    
    if (page.url().includes('dashboard')) {
      console.log("SUCCESS: Reached Dashboard!");
    } else {
      console.log("FAILED: Did not reach Dashboard");
    }

  } catch (error) {
    console.error("Test failed:", error);
  } finally {
    await browser.close();
  }
})();
