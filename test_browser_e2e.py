import asyncio
from playwright.async_api import async_playwright

async def run():
    print("Starting Playwright Test")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()

        print("Navigating to /onboarding")
        await page.goto("http://localhost:3000/onboarding")
        
        await page.wait_for_selector('input[placeholder*="AGENT"]', timeout=10000)
        
        # Fill name via Playwright
        await page.fill('input[placeholder*="AGENT"]', "TestWorker2")
        print("Filled Agent Name")

        # Pick avatar
        await page.click('.avatar-card:first-of-type')
        print("Selected Avatar")
        
        # Click Continue
        await page.click('button:has-text("Continue")')
        
        await page.wait_for_timeout(2000)
        print("URL is now:", page.url)

        if "role" not in page.url:
            print("Failed to advance to /onboarding/role!")
            await page.screenshot(path="failed_role.png")
            await browser.close()
            return
        
        print("Successfully advanced to /onboarding/role")

        print("Testing direct navigation bypass block (to /onboarding/deploying)")
        await page.goto("http://localhost:3000/onboarding/deploying")
        await page.wait_for_timeout(3000)
        print("URL after direct jump attempt:", page.url)

        print("Testing Back functionality to /onboarding/role")
        await page.go_back()
        await page.wait_for_timeout(2000)
        print("URL after back:", page.url)
        
        print("Testing Forward functionality to /onboarding/deploying")
        await page.go_forward()
        await page.wait_for_timeout(2000)
        print("URL after forward:", page.url)
        
        print("Now properly navigating through the rest of the steps")
        # Go back to role
        await page.goto("http://localhost:3000/onboarding/role")
        await page.wait_for_timeout(1000)
        
        # Click continue on Role
        await page.click('button:has-text("Continue")')
        await page.wait_for_timeout(1000)
        print("URL after role:", page.url)
        
        # Click continue on Maturity
        await page.click('button:has-text("Continue")')
        await page.wait_for_timeout(1000)
        print("URL after maturity:", page.url)

        # Select a KRA on Monitoring
        # The KRA button probably says "Select" or "Add" or we just click a KRA card
        await page.click('text="System Operations & Maintenance"')
        await page.click('button:has-text("Continue")')
        await page.wait_for_timeout(1000)
        print("URL after monitoring:", page.url)
        
        # AWS Tasks
        await page.click('text="Create an S3 bucket"')
        await page.click('button:has-text("Continue")')
        await page.wait_for_timeout(1000)
        print("URL after tasks:", page.url)

        # AWS Permissions
        await page.click('button:has-text("Approve & Finalize")')
        await page.wait_for_timeout(1000)
        print("URL after permissions:", page.url)
        
        print("Waiting for deployment to reach dashboard...")
        await page.wait_for_url("**/dashboard", timeout=90000)
        print("URL after deployment:", page.url)
        
        print("Dashboard reached successfully!")
        
        print("E2E Test reached checkpoints")
        await browser.close()

if __name__ == "__main__":
    asyncio.run(run())
