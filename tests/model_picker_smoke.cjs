// Focused model-list UI regression; all upstream calls use explicit offline responses.
// node model_picker_smoke.cjs <playwright package> [local app URL]
const assert = require('node:assert/strict');
const path = require('node:path');
const {chromium} = require(process.argv[2] || 'playwright');
const base = process.argv[3] || 'http://127.0.0.1:8767';
(async () => {
  const browser = await chromium.launch({channel:'msedge',headless:true});
  try {
    const page = await browser.newPage({viewport:{width:1440,height:1080}});
    let mode='success', probeRequest, releaseLate, reportLate;
    const lateSeen=new Promise(resolve => reportLate=resolve);
    const errors=[]; page.on('pageerror', error=>errors.push(error.message));
    await page.route('**/api/models',async route=>{
      const request=route.request().postDataJSON();
      assert.equal(request.connection.model,'');
      if(mode==='unsupported') return route.fulfill({status:404,json:{error:'此服务未提供模型列表接口。'}});
      if(mode==='empty') return route.fulfill({json:{models:[],message:'empty'}});
      if(mode==='late') { reportLate(); await new Promise(resolve=>releaseLate=resolve); return route.fulfill({json:{models:['old-service-model'],message:'late'}}); }
      return route.fulfill({json:{models:request.connection.base_url==='https://vision.example/v1'?['vision-a','vision-b']:['model-a','model-b'],message:'offline'}});
    });
    await page.route('**/api/probe',route=>{probeRequest=route.request().postDataJSON();return route.fulfill({json:{ok:true,message:'离线测试通过。'}});});
    await page.goto(base,{waitUntil:'networkidle'});
    await page.locator('#key-settings').evaluate(el=>el.open=true);
    await page.locator('#base-url').fill('https://relay.example/v1');
    await page.locator('#api-key').fill('OFFLINE-UI-KEY');
    await page.locator('#model-name').fill('');
    await page.locator('#text-models').click();
    await page.waitForFunction(()=>document.getElementById('text-model-list').options.length===4);
    assert.equal(await page.locator('#model-name').isVisible(),false);
    await page.locator('#text-model-list').selectOption({label:'model-b'});
    assert.equal(await page.locator('#model-name').inputValue(),'model-b');
    await page.locator('#text-probe').click();
    await page.waitForFunction(()=>document.getElementById('text-status').textContent==='离线测试通过。');
    assert.equal(probeRequest.connection.model,'model-b');

    await page.locator('#text-model-list').selectOption('manual');
    assert.equal(await page.locator('#model-name').isVisible(),true);
    await page.locator('#model-name').fill('custom-model');
    mode='unsupported'; await page.locator('#text-models').click();
    await page.waitForFunction(()=>document.getElementById('text-status').textContent.includes('未提供'));
    assert.equal(await page.locator('#model-name').inputValue(),'custom-model');
    assert.equal(await page.locator('#text-model-list').isVisible(),false);
    mode='empty'; await page.locator('#text-models').click();
    await page.waitForFunction(()=>document.getElementById('text-status').textContent.includes('没有返回'));
    assert.equal(await page.locator('#model-name').isVisible(),true);

    await page.locator('#vision-settings').evaluate(el=>el.open=true);
    await page.locator('#vision-url').fill('https://vision.example/v1');
    await page.locator('#vision-key').fill('OFFLINE-VISION-KEY');
    mode='success'; await page.locator('#vision-models').click();
    await page.waitForFunction(()=>document.getElementById('vision-model-list').options.length===4);
    await page.locator('#vision-model-list').selectOption({label:'vision-b'});
    await page.locator('#vision-probe').click();
    await page.waitForFunction(()=>document.getElementById('vision-status').textContent==='离线测试通过。');
    assert.equal(probeRequest.connection.model,'vision-b');
    assert.equal(probeRequest.connection.base_url,'https://vision.example/v1');

    await page.locator('#file').setInputFiles(path.join(__dirname,'fixtures/diagram.pptx'));
    await page.locator('#visual-consent-label').waitFor({state:'visible'});
    await page.locator('#visual-consent').check();
    await page.locator('#vision-model-list').selectOption({label:'vision-a'});
    assert.equal(await page.locator('#visual-consent').isChecked(),false);
    await page.locator('#reuse-text').check();
    await page.locator('#vision-models').click();
    await page.waitForFunction(()=>document.getElementById('text-model-list').options.length===4);
    await page.locator('#text-model-list').selectOption({label:'model-a'});
    await page.locator('#vision-probe').click();
    await page.waitForFunction(()=>document.getElementById('vision-status').textContent==='离线测试通过。');
    assert.equal(probeRequest.connection.model,'model-a');
    assert.equal(probeRequest.connection.base_url,'https://relay.example/v1');

    mode='late'; await page.locator('#text-models').click(); await lateSeen;
    await page.locator('#base-url').fill('https://changed.example/v1');
    releaseLate();
    await page.waitForFunction(()=>!document.getElementById('text-models').disabled);
    assert.equal(await page.locator('#text-model-list').isVisible(),false);
    assert.match(await page.locator('#text-status').innerText(),/连接信息已更改/);
    assert.deepEqual(errors,[]);
    console.log('Model picker passed: blank model lookup, dropdown selection and probe, manual fallback, unsupported/empty lists, vision reuse, consent reset, stale response rejection. No real model calls.');
  } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
