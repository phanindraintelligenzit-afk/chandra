const { chromium } = require('playwright');

(async () => {
  const browser = await chromium.launch({ headless: false });
  const page = await browser.newPage();

  try {
    console.log('Opening http://localhost:3000...');
    await page.goto('http://localhost:3000', { waitUntil: 'networkidle' });

    // Wait for page to load
    await page.waitForTimeout(2000);

    // Fill agent name
    console.log('Filling agent name...');
    const inputs = await page.$$('input[type="text"]');
    if (inputs.length > 0) {
      await inputs[0].fill('CloudEngineer');
      await page.waitForTimeout(500);
    }

    // Select role dropdown
    console.log('Selecting role...');
    const selects = await page.$$('select');
    if (selects.length > 0) {
      await selects[0].selectOption('0'); // Select first option
      await page.waitForTimeout(500);
    }

    // Click an avatar
    console.log('Clicking avatar...');
    const avatarButtons = await page.$$('div[class*="avatar"] button, button img[alt*="avatar"]');
    if (avatarButtons.length > 0) {
      await avatarButtons[0].click();
      await page.waitForTimeout(500);
    } else {
      // Try clicking an avatar div directly
      const avatars = await page.$$('div[class*="avatar"]');
      if (avatars.length > 0) {
        await avatars[0].click();
        await page.waitForTimeout(500);
      }
    }

    // Take screenshot of filled form
    await page.screenshot({ path: 'form-filled.png', fullPage: true });
    console.log('Form filled screenshot saved!');

    // Click continue button
    console.log('Clicking CONTINUE button...');
    const buttons = await page.$$('button');
    for (const btn of buttons) {
      const text = await btn.textContent();
      if (text && text.includes('CONTINUE')) {
        console.log('Found CONTINUE button, clicking...');
        await btn.click();
        break;
      }
    }

    await page.waitForTimeout(3000);

    console.log('Taking screenshot of main dashboard...');
    await page.screenshot({ path: 'dashboard.png', fullPage: true });
    console.log('Dashboard screenshot saved!');

    console.log('\nTest completed successfully!');
    await page.waitForTimeout(5000);

  } catch (error) {
    console.error('Error:', error.message);
  } finally {
    await browser.close();
  }
})();
