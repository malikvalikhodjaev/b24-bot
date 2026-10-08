const fs=require('fs'),path=require('path'),http=require('http'),assert=require('assert/strict');
const {chromium}=require('C:/Users/Lenovo/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const tag=process.argv[2]||'office-types-ui-qa-20261008';assert.match(tag,/^[a-z0-9-]+$/);
const root=path.resolve(__dirname,'../webapp'),out=path.resolve(__dirname,'../data/'+tag);
fs.mkdirSync(out,{recursive:true});
const names=new Set(['index.html','app.js','style.css','reference.json']);
const server=http.createServer((req,res)=>{const name=new URL(req.url,'http://localhost').pathname.split('/').pop()||'index.html';if(!names.has(name)){res.writeHead(404);res.end();return;}res.writeHead(200,{'Content-Type':{'html':'text/html; charset=utf-8','js':'text/javascript; charset=utf-8','css':'text/css; charset=utf-8','json':'application/json; charset=utf-8'}[name.split('.').pop()]});res.end(fs.readFileSync(path.join(root,name)));});
(async()=>{
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));let browser;
  try{
    browser=await chromium.launch({headless:true});
    const context=await browser.newContext({viewport:{width:390,height:844}});
    await context.route('https://core.telegram.org/js/telegram-web-app.js',route=>route.fulfill({contentType:'text/javascript',body:''}));
    await context.addInitScript(()=>{window.sent=[];window.Telegram={WebApp:{platform:'android',ready(){},expand(){},sendData(value){window.sent.push(JSON.parse(value));},MainButton:{show(){},setText(){},showProgress(){},hideProgress(){},onClick(fn){window.mainClick=fn;}}}};});
    const page=await context.newPage(),errors=[];page.on('pageerror',error=>errors.push(error.message));
    const base='http://127.0.0.1:'+server.address().port+'/?mode=support&lang=ru&token=qa-support-only';
    async function open(){await page.goto(base);await page.waitForFunction(()=>document.querySelector('#unknownType').disabled===false);await page.locator('[name=pharmacy_kind][value=existing]').check();await page.locator('[name=fom_id]').fill('00081');}
    async function noOverflow(){assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);}
    await open();assert.equal(await page.locator('#unknownType').innerText(),'Неопознанная ошибка: нужно уточнить');
    assert.equal(await page.locator('[name=support_type]').inputValue(),'');assert.equal(await page.locator('#supportTypeResults button').count(),12);
    await page.locator('form [type=submit]').click();assert.match(await page.locator('#error').innerText(),/Выберите тип обращения/);assert.equal(await page.evaluate(()=>window.sent.length),0);
    await page.locator('[name=support_program]').selectOption('3122');await page.locator('#supportSearch').fill('НЕ МОГУТ ВОЙТИ');
    assert.equal(await page.locator('#supportTypeResults button').count(),1);assert.equal(await page.locator('#supportTypeResults').innerText(),'Не могут войти в ПО');
    await page.locator('#supportSearch').press('Enter');assert.equal(await page.locator('[name=support_type]').inputValue(),'office:b96ef294-f632-484a-b08b-345cd915f00c');
    assert.equal(await page.locator('[name=request_title]').inputValue(),'Не могут войти в ПО');assert.equal(await page.locator('#typeSearchPanel').isVisible(),false);
    await page.locator('[name=request_description]').fill('После пароля не открывается касса. Нужна помощь с подключением.');
    await page.locator('[name=request_title]').fill('Касса не открывается у клиента');await page.locator('#language').click();
    assert.equal(await page.locator('[name=support_type]').inputValue(),'office:b96ef294-f632-484a-b08b-345cd915f00c');assert.equal(await page.locator('[name=support_program]').inputValue(),'3122');
    assert.match(await page.locator('#supportTypeLabel').innerText(),/Мурожаат/);await page.locator('#language').click();
    await page.locator('[name=support_program]').selectOption('3118');assert.equal(await page.locator('[name=support_type]').inputValue(),'');
    assert.equal(await page.locator('[name=request_title]').inputValue(),'Касса не открывается у клиента');assert.match(await page.locator('[name=request_description]').inputValue(),/После пароля/);
    await page.locator('#supportSearch').fill('такой причины нет');assert.equal(await page.locator('#supportTypeResults button').count(),0);assert.match(await page.locator('#typeCount').innerText(),/Ничего не найдено/);
    await page.locator('#unknownType').click();assert.equal(await page.locator('[name=support_type]').inputValue(),'5890');assert.match(await page.locator('#requestDescriptionHint').innerText(),/своими словами/);
    await page.screenshot({path:path.join(out,'mobile-selected.png'),fullPage:true});await noOverflow();
    await page.evaluate(()=>{window.mainClick();window.mainClick();});const sent=await page.evaluate(()=>window.sent);
    assert.equal(sent.length,1);assert.equal(sent[0].support_type,'5890');assert.equal(sent[0].support_program,'3118');assert.equal(sent[0].fom_id,'00081');assert.match(sent[0].request_description,/После пароля/);
    await open();await page.locator('[data-type-id="office:9ddd84d7-b5f2-45f8-86d2-da4c1552d6ec"]').click();await page.locator('form [type=submit]').click();assert.match(await page.locator('#error').innerText(),/выберите программу/);assert.equal(await page.evaluate(()=>window.sent.length),0);
    await open();await page.locator('#supportSearch').fill('обновлением цен');await page.locator('#supportTypeResults button').click();assert.match(await page.locator('#supportMappingHint').innerText(),/Точного типа в Б24 нет/);await page.locator('[name=request_description]').fill('Цены в приложении не обновились. Помогите проверить.');await page.locator('form [type=submit]').click();assert.equal((await page.evaluate(()=>window.sent))[0].support_type,'office:34c36e4e-61b6-4743-83e6-d1dbc6d39065');
    await open();await page.locator('#supportSearch').fill('чек сумма');assert.ok(await page.locator('#supportTypeResults button').count()>0);await page.screenshot({path:path.join(out,'mobile-search.png'),fullPage:true});await noOverflow();
    await page.locator('#supportSearch').fill('');await page.locator('#moreTypes').click();assert.equal(await page.locator('#supportTypeResults button').count(),32);
    await page.setViewportSize({width:1280,height:900});await page.screenshot({path:path.join(out,'desktop-search.png'),fullPage:true});await noOverflow();
    assert.deepEqual(errors,[]);
    fs.writeFileSync(path.join(out,'result.json'),JSON.stringify({passed:true,viewports:[390,1280],checks:['unknown first, explicit choice required','program filter and multiword case-insensitive search','Enter selects unique match','auto theme and edited theme preserved','RU/UZ switch preserves selection','program change clears incompatible type only','no match fallback and free description','unknown native ID sent once, FOM ID preserved','program required for product-specific office type','unmapped office type notice and original UUID submission','paged list, no overflow or browser errors']},null,2));
    console.log('Support picker UI: passed on mobile 390px and desktop 1280px');
  }finally{if(browser)await browser.close();await new Promise(resolve=>server.close(resolve));}
})().catch(error=>{console.error(error);process.exitCode=1;});
