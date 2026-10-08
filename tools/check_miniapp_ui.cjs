const fs = require('fs');
const path = require('path');
const http = require('http');
const assert = require('assert/strict');
const {chromium} = require('C:/Users/Lenovo/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const root = path.resolve(__dirname, '../webapp');
const tag=process.argv[2]||'miniapp-half-hour-qa-20261008';assert.match(tag,/^[a-z0-9-]+$/);
const out = path.resolve(__dirname, '../data/'+tag);
fs.mkdirSync(out, {recursive:true});
const assets = new Set(['index.html','app.js','style.css','reference.json']);
const server = http.createServer((req,res) => {
  const name = new URL(req.url,'http://localhost').pathname.split('/').pop() || 'index.html';
  if (!assets.has(name)) { res.writeHead(404);res.end();return; }
  res.writeHead(200,{'Content-Type': {'html':'text/html; charset=utf-8','js':'text/javascript; charset=utf-8','css':'text/css; charset=utf-8','json':'application/json; charset=utf-8'}[name.split('.').pop()]});
  res.end(fs.readFileSync(path.join(root,name)));
});
(async () => {
  await new Promise(resolve => server.listen(0,'127.0.0.1',resolve));
  let browser;
  try {
    browser = await chromium.launch({headless:true});
    const context = await browser.newContext({viewport:{width:390,height:844},deviceScaleFactor:1});
    await context.route('https://core.telegram.org/js/telegram-web-app.js',route=>route.fulfill({contentType:'text/javascript',body:''}));
    await context.addInitScript(()=>{
      window.sent=[];window.mainClick=undefined;
      window.Telegram={WebApp:{platform:'android',ready(){},expand(){},sendData(value){window.sent.push(JSON.parse(value));},MainButton:{setText(){},show(){},showProgress(){},hideProgress(){},onClick(fn){window.mainClick=fn;}}}};
    });
    const page = await context.newPage();
    const errors=[];page.on('pageerror',error=>errors.push(error.message));
    const base='http://127.0.0.1:'+server.address().port+'/?token=qa-only';
    async function open(mode){await page.goto(base+'&mode='+mode);await page.waitForFunction(()=>document.querySelector('[name=business_region]').options.length>2);}
    async function noOverflow(){assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);}
    await open('deal');await noOverflow();
    assert.equal(await page.locator('#pharmacyChoice h2').innerText(),'🏪 На какую аптеку создать сделку?');
    await page.screenshot({path:path.join(out,'mobile-choice.png')});
    assert.equal(await page.locator('#pharmacyData').isVisible(),false);
    await page.locator('[name=pharmacy_kind][value=existing]').check();
    await page.locator('[name=fom_id]').fill('00081');
    await page.locator('form [type=submit]').click();assert.equal((await page.evaluate(()=>window.sent)).length,0);assert.match(await page.locator('#error').innerText(),/Выберите хотя бы одну/);
    await page.locator('[name=discussion_programs]').nth(0).check();await page.locator('[name=discussion_programs]').nth(1).check();
    await page.locator('#language').click();assert.equal(await page.locator('[name=discussion_programs]:checked').count(),2);assert.match(await page.locator('#discussion h2').innerText(),/дастур/);await page.locator('#language').click();
    await page.screenshot({path:path.join(out,'mobile-programs.png'),fullPage:true});
    assert.equal(await page.locator('[name=inn]').isDisabled(),true);
    await page.locator('[name=reminder]').check();
    await page.locator('[name=reminder_action]').selectOption('other');assert.equal(await page.locator('#customAction').isVisible(),true);await page.locator('[name=next_step]').fill('Уточнить поставку');
    await page.locator('[name=reminder_action]').selectOption('call');assert.equal(await page.locator('#customAction').isVisible(),false);assert.equal(await page.locator('[name=next_step]').inputValue(),'Позвонить');
    await page.locator('#chooseDeadline').click();
    await page.locator('[data-day-offset="1"]').click();
    assert.equal(await page.locator('#hour option').count(),48);assert.equal(await page.locator('#hour option[value="00:30"]').count(),1);assert.equal(await page.locator('#hour option[value="23:30"]').count(),1);
    await page.locator('#hour').selectOption('10:30');
    await page.screenshot({path:path.join(out,'mobile-calendar.png')});await noOverflow();
    await page.locator('#applyDate').click();await page.locator('#language').click();assert.match(await page.locator('#chooseDeadline').innerText(),/10:30/);await page.locator('#language').click();await page.locator('#chooseDeadline').click();assert.equal(await page.locator('#hour').inputValue(),'10:30');await page.locator('#closeDate').click();
    assert.match(await page.locator('#chooseDeadline').innerText(),/10:30/);
    await page.screenshot({path:path.join(out,'mobile-existing-reminder.png')});
    await page.locator('form [type=submit]').click();
    await page.evaluate(()=>window.mainClick());
    let sent=await page.evaluate(()=>window.sent);
    assert.equal(sent.length,1);assert.equal(sent[0].discussion_programs.length,2);assert.equal(sent[0].reminder_action,'call');assert.equal(sent[0].next_step,'Позвонить');assert.equal(sent[0].fom_id,'00081');assert.equal(sent[0].inn,'');assert.equal(sent[0].title,'');assert.equal(sent[0].reminder,true);assert.match(sent[0].deadline,/T10:30$/);
    await open('deal');await page.locator('[name=pharmacy_kind][value=new]').check();
    const reference=JSON.parse(fs.readFileSync(path.join(root,'reference.json'),'utf8'));
    const region=reference.regions.find(row=>(reference.cities[row.id]||[]).length);
    await page.locator('[name=title]').fill('Аптека проверка');await page.locator('[name=inn]').fill('123456789');await page.locator('[name=company_name]').fill('Фирма проверка');
    await page.locator('[name=business_region]').selectOption(String(region.id));await page.locator('[name=city]').selectOption(String(reference.cities[region.id][0].id));await page.locator('[name=program]').selectOption(String(reference.programs[0].id));await page.locator('[name=address]').fill('Улица, 1');await page.locator('[name=discussion_programs]').nth(0).check();await page.locator('[name=phone]').fill('909876543');
    assert.equal(await page.locator('[name=contact_name]').isDisabled(),true);
    await page.screenshot({path:path.join(out,'mobile-new.png'),fullPage:true});await noOverflow();
    await page.locator('form [type=submit]').click();sent=await page.evaluate(()=>window.sent);
    assert.equal(sent.length,1);assert.equal(sent[0].contact_policy,'optional');assert.equal(sent[0].create_contact,false);assert.equal(sent[0].contact_name,'');assert.equal(sent[0].phone,'909876543');
    await open('support');assert.equal(await page.locator('#pharmacyChoice h2').innerText(),'🏪 На какую аптеку создать заявку?');await page.locator('[name=pharmacy_kind][value=existing]').check();await page.locator('[name=fom_id]').fill('00081');await page.locator('#unknownType').click();await page.locator('[name=request_title]').fill('Ошибка подключения');await page.locator('[name=request_description]').fill('Не открывается программа, помогите подключить.');
    assert.equal(await page.locator('#reminder').isVisible(),false);await page.screenshot({path:path.join(out,'mobile-support.png')});await page.locator('form [type=submit]').click();sent=await page.evaluate(()=>window.sent);assert.equal(sent[0].mode,'support');assert.equal(sent[0].reminder,false);
    await open('pharmacy');assert.equal(await page.locator('#pharmacyChoice').isVisible(),false);assert.equal(await page.locator('#pharmacyData').isVisible(),true);await page.locator('[name=create_contact]').check();assert.equal(await page.locator('#newContact').isVisible(),true);assert.equal(await page.locator('[name=phone]').getAttribute('required'),'');await page.locator('#language').click();assert.match(await page.locator('h1').innerText(),/Дорихона/);await noOverflow();
    await page.setViewportSize({width:1280,height:900});await open('deal');await page.locator('[name=pharmacy_kind][value=existing]').check();await page.screenshot({path:path.join(out,'desktop-existing.png')});await noOverflow();
    assert.deepEqual(errors,[]);
    fs.writeFileSync(path.join(out,'result.json'),JSON.stringify({passed:true,viewports:[390,1280],checks:['new/existing conditional fields','FOM ID keeps leading zeros','multiple discussion programs and language switch preserve choice','reminder presets and custom text','48 time choices and half-hour minutes survive submission','phone without contact','support form','standalone pharmacy','Uzbek switch','duplicate submission blocked','no overflow or browser errors'],screenshots:fs.readdirSync(out).filter(x=>x.endsWith('.png'))},null,2));
    console.log('Mini App UI: passed, mobile 390px and desktop 1280px; screenshots in data/miniapp-half-hour-qa-20261008');
  } finally {if(browser)await browser.close();await new Promise(resolve=>server.close(resolve));}
})().catch(error=>{console.error(error);process.exitCode=1;});
