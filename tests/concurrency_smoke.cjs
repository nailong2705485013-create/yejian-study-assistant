// Real local settings API, intercepted model/generation calls, no real keys.
const path = require('node:path');
const fs = require('node:fs/promises');
const assert = require('node:assert/strict');
const {chromium} = require(process.argv[2] || 'playwright');
const base = process.argv[3] || 'http://127.0.0.1:8767';
(async () => {
  const browser = await chromium.launch({channel: 'msedge', headless: true});
  try {
    const page = await browser.newPage({viewport: {width: 1440, height: 1080}});
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    let submitted, jobsPosted = 0;
    await page.route('**/api/jobs', route => {
      if (route.request().method() !== 'POST') return route.continue();
      jobsPosted++; submitted = route.request().postDataJSON();
      return route.fulfill({status: 400, json: {error: '离线验收：已截获任务，没有调用模型。'}});
    });
    await page.goto(base, {waitUntil: 'networkidle'});
    assert.equal(await page.locator('#concurrency option').count(), 8);
    await page.locator('#concurrency').selectOption('4');
    await page.waitForFunction(() => document.getElementById('concurrency-status').textContent.includes('已保存4路'));
    await page.reload({waitUntil: 'networkidle'});
    assert.equal(await page.locator('#concurrency').inputValue(), '4');
    // A fresh browser context loads the server's saved preference as well.
    const fresh = await browser.newPage();
    await fresh.goto(base, {waitUntil: 'networkidle'});
    assert.equal(await fresh.locator('#concurrency').inputValue(), '4');
    await fresh.close();
    await page.locator('#sample').click();
    await page.waitForFunction(() => !document.getElementById('generate').disabled);
    await page.locator('#generate').click();
    await page.waitForFunction(() => document.getElementById('error').textContent.includes('已截获'));
    assert.equal(submitted.concurrency, 4);
    assert.equal(jobsPosted, 1);
    await page.locator('#concurrency').selectOption('1');
    await page.waitForFunction(() => document.getElementById('concurrency-status').textContent.includes('已保存1路'));
    await page.reload({waitUntil: 'networkidle'});
    assert.equal(await page.locator('#concurrency').inputValue(), '1');
    // Failed writes are visible and block generation until a new setting saves.
    await page.route('**/api/settings', route => route.fulfill({status: 400, json: {error: '离线模拟：目录不可写'}}));
    await page.locator('#concurrency').selectOption('3');
    await page.waitForFunction(() => document.getElementById('concurrency-status').textContent.includes('未保存'));
    await page.locator('#sample').click();
    await page.waitForFunction(() => !document.getElementById('generate').disabled);
    await page.locator('#generate').click();
    await page.waitForFunction(() => document.getElementById('error').textContent.includes('并发设置尚未保存'));
    assert.equal(jobsPosted, 1);
    await page.unroute('**/api/settings');
    await page.locator('#concurrency').selectOption('4');
    await page.waitForFunction(() => document.getElementById('concurrency-status').textContent.includes('已保存4路'));
    // Keep two writes in flight to verify the final selection wins in order.
    await page.locator('#concurrency').selectOption('3');
    await page.locator('#concurrency').selectOption('2');
    await page.waitForFunction(() => document.getElementById('concurrency-status').textContent.includes('已保存2路'));
    await page.reload({waitUntil: 'networkidle'});
    assert.equal(await page.locator('#concurrency').inputValue(), '2');
    await page.setViewportSize({width: 390, height: 844});
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    const output = path.resolve(__dirname, '../.qa');
    await fs.mkdir(output, {recursive: true});
    await page.screenshot({path: path.join(output, 'concurrency-mobile.png'), fullPage: true});
    await page.setViewportSize({width: 1440, height: 1080});
    await page.screenshot({path: path.join(output, 'concurrency-desktop.png'), fullPage: true});
    assert.deepEqual(errors, []);
    console.log('Concurrency UI passed: 1-8 choices, saved 4/1/2, reload and fresh context, task snapshot, visible save failure, queued writes, mobile layout. No model calls.');
  } finally {await browser.close();}
})().catch(error => {console.error(error); process.exitCode = 1;});
