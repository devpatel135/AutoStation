'use strict';
/** Realistic Playwright codegen transcripts used by the recorder tests (kept in their own module so they can be reused/inspected). */

const LIB_LOGIN = `const { chromium } = require('playwright');

(async () => {
  const browser = await chromium.launch({
    headless: false
  });
  const context = await browser.newContext();
  const page = await context.newPage();
  await page.goto('https://portal.example.com/');
  await page.getByLabel('Username').click();
  await page.getByLabel('Username').fill('alice@example.com');
  await page.getByLabel('Password').fill('S3cret!pass');
  await page.getByRole('button', { name: 'Sign in' }).click();
  await page.waitForURL('https://portal.example.com/dashboard');
  await page.getByPlaceholder('Search').fill('invoice 42');
  await page.getByPlaceholder('Search').press('Enter');
  await page.getByRole('checkbox', { name: 'Include archived' }).check();
  await page.locator('#region').selectOption('emea');
  await expect(page.locator('h1')).toContainText('Results; done');
  await page.waitForTimeout(2000);
  const page1Promise = page.waitForEvent('popup');
  await page.getByRole('link', { name: 'Open report' }).click();
  const page1 = await page1Promise;
  await page1.getByRole('button', { name: 'Download' }).click();

  // ---------------------
  await context.close();
  await browser.close();
})();
`;

const TEST_ROWS = `import { test, expect } from '@playwright/test';

test('test', async ({ page }) => {
  await page.goto('https://erp.example.com/invoices');
  await page.getByRole('row', { name: 'INV-1001' }).getByRole('button', { name: 'Approve' }).click();
  await page.getByRole('textbox', { name: 'Comment' }).fill('ok');
  await page.getByRole('row', { name: 'INV-1002' }).getByRole('button', { name: 'Approve' }).click();
  await page.getByRole('textbox', { name: 'Comment' }).fill('ok');
  await page.getByRole('row', { name: 'INV-1003' }).getByRole('button', { name: 'Approve' }).click();
  await page.getByRole('textbox', { name: 'Comment' }).fill('ok');
  await page.getByRole('button', { name: 'Submit batch' }).click();
});
`;

const LEGACY = `const { chromium } = require('playwright');
(async () => {
  const browser = await chromium.launch({ headless: false });
  const context = await browser.newContext();
  const page = await context.newPage();
  await page.goto('https://legacy.example.com/');
  await page.click('text=Login');
  await page.fill('input[name="q"]', 'hello');
  await page.press('input[name="q"]', 'Enter');
  await page.check('#agree');
  await page.selectOption('select#size', 'large');
  await page.setInputFiles('input[type=file]', 'C:\\\\docs\\\\a.pdf');
  await page.dblclick('#row-2');
  await context.close();
  await browser.close();
})();
`;

const SENSITIVE = `import { test } from '@playwright/test';
test('s', async ({ page }) => {
  await page.goto('https://hr.example.com/');
  await page.getByLabel('Social Security Number').fill('123-45-6789');
  await page.locator('input[type="password"]').fill('x');
  await page.getByLabel('Card').fill('4111111111111111');
  await page.getByLabel('API key').fill('abcdEFGH1234abcdEFGH1234');
  await page.getByLabel('Passport office').fill('Leeds');
  await page.keyboard.type('eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.c2lnbmF0dXJlMTIzNDU2');
});
`;

const FRAMES = `import { test } from '@playwright/test';
test('frames', async ({ page }) => {
  await page.goto('https://shop.example.com/cart');
  page.once('dialog', dialog => {
    dialog.dismiss().catch(() => {});
  });
  await page.frameLocator('#checkout').getByText('Accept terms').click();
  await page.locator('iframe').contentFrame().getByRole('button', { name: 'Pay now' }).click();
  await page.getByText(/total: \\d+;/).first().click();
  await page.mystery.thing();
});
`;


module.exports = { LIB_LOGIN, TEST_ROWS, LEGACY, SENSITIVE, FRAMES };
