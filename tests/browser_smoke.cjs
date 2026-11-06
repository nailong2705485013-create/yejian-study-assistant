// UI checks against a running local app. No real model requests are sent.
// Usage: node browser_smoke.cjs <absolute playwright package directory> [base URL]
const path = require('node:path');
const fs = require('node:fs/promises');
const assert = require('node:assert/strict');
const {chromium} = require(process.argv[2] || 'playwright');
const base = process.argv[3] || 'http://127.0.0.1:8765';

(async () => {
  const output = path.resolve(__dirname, '../.qa');
  await fs.mkdir(output, {recursive: true});
  const browser = await chromium.launch({channel: 'msedge', headless: true});
  try {
    const page = await browser.newPage({viewport: {width: 1440, height: 1080}, deviceScaleFactor: 1});
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(base, {waitUntil: 'networkidle'});
    await page.screenshot({path: path.join(output, 'desktop-empty.png'), fullPage: true});
    assert.equal(await page.locator('#generate').isDisabled(), true);
    await page.locator('#sample').click();
    await page.waitForFunction(() => document.getElementById('file-info').textContent.includes('预计4次起'));
    assert.equal(await page.locator('#generate').isDisabled(), false);
    const config = await page.evaluate(() => fetch('/api/config').then(response => response.json()));
    await page.route('**/api/probe', route => route.fulfill({json: {ok: true, message: '离线文字测试通过。'}}));
    await page.route('**/api/models', route => route.fulfill({json: {models: ['offline-model'], message: '离线列表测试。'}}));
    await page.locator('#key-settings').evaluate(el => {el.open = true;});
    await page.locator('#text-models').click();
    await page.waitForFunction(() => document.getElementById('text-model-list').value === 'model:0');
    assert.equal(await page.locator('#model-name').inputValue(), 'offline-model');
    // No real generation request, even when the server has an environment key.
    await page.route('**/api/jobs', route => route.request().method() === 'POST'
      ? route.fulfill({status:400,json:{error:'离线验收：请配置API Key。'}}) : route.continue());
    {
      await page.locator('#generate').click();
      await page.locator('#error').waitFor({state: 'visible'});
      assert.match(await page.locator('#error').innerText(), /Key/);
    }
    await page.screenshot({path: path.join(output, 'desktop-imported.png'), fullPage: true});
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    await page.setViewportSize({width: 390, height: 844});
    await page.screenshot({path: path.join(output, 'mobile.png'), fullPage: true});
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    await page.setViewportSize({width: 1440, height: 1080});
    // Saving is mocked in this browser only; production DPAPI is tested separately.
    const savedPublic = {text:{base_url:'https://saved.example/v1',model:'saved-model',json_mode:true,has_saved_key:true},vision:null,reuse_text_connection:false};
    await page.route('**/api/connections', route => route.fulfill({json:{message:'离线保存交互通过。',connections:savedPublic}}));
    await page.locator('#base-url').fill('https://saved.example/v1');
    await page.locator('#model-name').fill('saved-model');
    await page.locator('#api-key').fill('OFFLINE-UI-TEST-KEY');
    await page.locator('#save-connections').click();
    await page.waitForFunction(() => document.getElementById('save-status').textContent.includes('离线保存'));
    assert.equal(await page.locator('#api-key').inputValue(), '');
    assert.match(await page.locator('#api-key').getAttribute('placeholder'),/已保存/);
    await page.route('**/api/config', route => route.fulfill({json:{...config,connections:savedPublic}}));
    await page.reload({waitUntil:'networkidle'});
    assert.equal(await page.locator('#base-url').inputValue(),'https://saved.example/v1');
    assert.equal(await page.locator('#api-key').inputValue(),'');
    await page.locator('#file').setInputFiles(path.join(__dirname, 'fixtures/diagram.pptx'));
    await page.waitForFunction(() => document.getElementById('file-info').textContent.includes('diagram.pptx') && !document.getElementById('generate').disabled);
    assert.equal(await page.locator('#visual-consent-label').isVisible(), true);
    await page.locator('#vision-url').fill('https://vision.example/v1');
    await page.locator('#visual-consent').check();
    await page.locator('#vision-url').fill('https://another.example/v1');
    assert.equal(await page.locator('#visual-consent').isChecked(), false);
    await page.locator('.range-settings > summary').click();
    await page.locator('#page-range').fill('2');
    await page.locator('#reinspect').click();
    await page.waitForFunction(() => document.getElementById('file-info').textContent.includes('1/2页'));
    await page.screenshot({path:path.join(output,'pptx-config.png'),fullPage:true});

    const quote = '可靠传输需要序号确认与重传机制';
    const citations = [{source_id: 'S001', quote}];
    const id = 'f'.repeat(32);
    const result = {
      filename: '界面验收示例（非模型生成）.txt', focus: '验证结果阅读与引用交互',
      completed_at: '2026-10-15T12:00:00+08:00', model: 'offline-ui-test',
      notes: [{id: 'N001', title: '<script>window.pwned=1</script>', content: '可靠传输通过序号、确认与重传等机制工作。', citations}],
      questions: [{id: 'Q001', question: '可靠传输有哪些机制？', answer: '序号、确认与重传。', explanation: '三者协作。', citations}],
      cards: [{id: 'C001', front: '可靠传输的机制？', back: '序号、确认与重传。', citations}],
      sources: [{id: 'S001', page: null, locator: '文本片段1', text: quote + '。这段文字只供界面验收。'}],
      audit: {issues: [], summary: '仅供离线界面验收。'}, removed_count: 0, warnings: [],
      usage: {calls: 4, prompt_tokens: 1200, completion_tokens: 600}
    };
    const job = {id, filename: result.filename, status: 'done', progress: 100, created_at: result.completed_at,
                 events: [{time: result.completed_at, message: '离线界面验收完成。'}], result};
    await page.route('**/api/jobs', route => route.fulfill({json: [job]}));
    await page.route(`**/api/jobs/${id}`, route => route.fulfill({json: job}));
    await page.route(`**/api/jobs/${id}/notes.md`, route => route.fulfill({body: '# 离线界面验收',
      headers: {'Content-Type': 'application/octet-stream', 'Content-Disposition': 'attachment; filename="notes.md"'}}));
    await page.route(`**/api/jobs/${id}/notes.docx`, route => route.fulfill({body: 'Offline download interaction test only',
      headers: {'Content-Type': 'application/octet-stream', 'Content-Disposition': 'attachment; filename="notes.docx"'}}));
    await page.reload({waitUntil: 'networkidle'});
    await page.locator('.history-item').click();
    await page.locator('#results').waitFor({state: 'visible'});
    assert.equal(await page.evaluate(() => window.pwned), undefined);
    await page.locator('.citation').first().click();
    assert.equal(await page.locator('#source-dialog').isVisible(), true);
    assert.match(await page.locator('#source-text').innerText(), /可靠传输/);
    await page.locator('#close-source').click();
    await page.locator('#tab-questions').click();
    await page.locator('#result-content summary').click();
    assert.match(await page.locator('#result-content').innerText(), /答案：序号/);
    await page.locator('#tab-cards').click();
    await page.locator('.flip-button').click();
    assert.match(await page.locator('.flip-button').innerText(), /序号、确认/);
    const downloadEvent = page.waitForEvent('download');
    await page.locator('#download-md').click();
    assert.equal((await downloadEvent).suggestedFilename(), 'notes.md');
    const wordDownload = page.waitForEvent('download');
    await page.locator('#download-word').click();
    assert.equal((await wordDownload).suggestedFilename(), 'notes.docx');
    await page.screenshot({path: path.join(output, 'desktop-results.png'), fullPage: true});
    assert.deepEqual(errors, []);
    console.log('Browser checks passed: import, missing-key feedback, desktop/mobile layout, safe text rendering, citations, answers, flashcards, downloads.');
    console.log('No real model requests were sent; screenshots: ' + output);
  } finally {
    await browser.close();
  }
})().catch(error => {console.error(error); process.exitCode = 1;});
