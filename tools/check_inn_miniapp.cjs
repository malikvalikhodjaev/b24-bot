/* Real production assets; fake Telegram transport and read-only catalogue responses. */
const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const base=process.env.INN_QA_URL||'http://127.0.0.1:18773/';
const output=path.resolve(__dirname,'../data/inn-miniapp-qa');fs.mkdirSync(output,{recursive:true});
const companies=[{id:7,title:'ООО «Саломат Фарм»',pharmacy_count:2,pharmacies:[
  {id:81,title:'Мирабад — аптека',fom_id:'00081',address:'Мирабад, 12',status:'Работает',business_region:'Город Ташкент',city:'Мирабадский район',current_program:'1С'},
  {id:82,title:'Юнусабад — аптека',fom_id:'',address:'Юнусабад, 4',status:'Закрыта'}]}];
(async()=>{
  const browser=await chromium.launch({headless:true});const results=[];
  async function page(mode='deal',width=390){
    const context=await browser.newContext({viewport:{width,height:844},colorScheme:'dark'});
    const page=await context.newPage();const errors=[];page.on('pageerror',error=>errors.push(error.message));
    await context.addInitScript(()=>{window.sent=[];window.Telegram={WebApp:{platform:'android',sendData:data=>window.sent.push(JSON.parse(data)),ready(){},expand(){},MainButton:{setText(){},onClick(){},show(){},showProgress(){},hideProgress(){}}}};});
    await page.route('https://core.telegram.org/js/telegram-web-app.js',route=>route.fulfill({body:'',contentType:'text/javascript'}));
    await page.route('**/api/lookup',async route=>{
      const data=route.request().postDataJSON();assert.equal(data.token,'datfo-form-'+'a'.repeat(32));assert.equal(data.mode,mode);
      await new Promise(resolve=>setTimeout(resolve,150));
      if(data.inn==='300000199')return route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({error:'Б24 пока не ответил. Повторите поиск чуть позже.'})});
      let rows=data.inn==='300000102'?[]:data.inn==='300000103'?[{id:8,title:'Без аптек',pharmacy_count:0,pharmacies:[]}]:data.inn==='300000104'?[...companies,{id:9,title:'Вторая фирма',pharmacy_count:0,pharmacies:[]}]:companies;
      await route.fulfill({contentType:'application/json',body:JSON.stringify({companies:rows})});
    });
    await page.goto(base+`?mode=${mode}&lang=ru&token=datfo-form-${'a'.repeat(32)}`);
    await page.waitForFunction(()=>document.querySelector('[name=business_region]').options.length>1);

    return {page,context,errors};
  }
  async function find(p,inn='300000101'){await p.locator('[name=inn]').fill(inn);await p.locator('#findInn').click();assert.match(await p.locator('#lookupStatus').innerText(),/Подождите/);await p.waitForFunction(()=>!document.getElementById('findInn').disabled);}
  async function program(p){await p.locator('[name=discussion_programs]').first().check();}
  async function send(p){await p.locator('[type=submit]').click();await p.waitForFunction(()=>window.sent.length===1);return p.evaluate(()=>window.sent[0]);}
  {
    const {page:p,context,errors}=await page('deal',320);
    await p.locator('[name=deal_title]').fill('Обсудить внедрение F-Apteka');await program(p);
    assert.equal(await p.locator('#companyFields').isVisible(),true);
    assert.equal(await p.locator('[name=inn]').isDisabled(),false);assert.equal(await p.locator('[name=entry_kind]').count(),0);
    const value=await send(p);assert.equal(value.pharmacy_kind,'title');assert.equal(value.deal_title,'Обсудить внедрение F-Apteka');assert.equal(value.inn,'');
    assert.deepEqual(errors,[]);assert.equal(await p.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
    await p.screenshot({path:path.join(output,'title-only-320.png'),fullPage:true});results.push('title-only-without-inn');await context.close();
  }
  {
    const {page:p,context,errors}=await page('deal',390);await p.locator('[name=deal_title]').fill('Моя встреча с клиентом');await find(p);
    await p.locator('[name=bind_pharmacy]').check();await p.locator('[name=selected_pharmacy]').first().check();
    assert.equal(await p.locator('#pointPrefill').isVisible(),true);assert.equal(await p.locator('#pointFields').isVisible(),false);
    await p.locator('[name=prefill_pharmacy]').check();assert.equal(await p.locator('#pointRegion').inputValue(),'Город Ташкент');assert.equal(await p.locator('#pointCity').inputValue(),'Мирабадский район');
    assert.equal(await p.locator('[name=deal_title]').inputValue(),'Моя встреча с клиентом');
    await p.locator('[name=bind_pharmacy]').uncheck();assert.equal(await p.locator('#pointFields').isVisible(),false);assert.equal(await p.locator('#pointRegion').inputValue(),'');await program(p);
    const value=await send(p);assert.equal(value.pharmacy_kind,'company');assert.equal(value.pharmacy_id,'');assert.equal(value.deal_title,'Моя встреча с клиентом');
    assert.deepEqual(errors,[]);results.push('checkbox-unchecked-clears-point-custom-title-kept');await context.close();
  }
  for(const width of [320,390,1280]){
    const {page:p,context,errors}=await page('deal',width);await find(p);assert.match(await p.locator('#lookupStatus').innerText(),/Аптек в базе: 2/);
    await program(p);
    const value=await send(p);assert.equal(value.pharmacy_kind,'company');assert.equal(value.company_id,7);assert.equal(value.pharmacy_id,'');assert.equal(value.intake_version,2);
    assert.equal(await p.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);assert.deepEqual(errors,[]);
    await p.screenshot({path:path.join(output,`company-${width}.png`),fullPage:true});results.push(`company-${width}`);await context.close();
  }
  {
    const {page:p,context,errors}=await page();await find(p);await p.locator('[name=bind_pharmacy]').check();
    assert.equal(await p.locator('#pharmacyResults [name=selected_pharmacy]').count(),2);assert.match(await p.locator('#pharmacyResults').innerText(),/Город Ташкент/);assert.match(await p.locator('#pharmacyResults').innerText(),/Мирабадский район/);
    await p.locator('#pharmacySearch').fill('Юнусабад');assert.equal(await p.locator('#pharmacyResults [name=selected_pharmacy]').count(),1);
    await p.locator('[name=selected_pharmacy]').check();await program(p);const value=await send(p);assert.equal(value.pharmacy_id,'82');assert.equal(value.fom_id,'');assert.deepEqual(errors,[]);
    results.push('pharmacy-without-fom-id');await context.close();
  }
  {
    const {page:p,context}=await page();await find(p,'300000103');assert.equal(await p.locator('[name=bind_pharmacy]').isDisabled(),true);
    await program(p);assert.equal((await send(p)).company_id,8);
    results.push('company-with-no-pharmacies');await context.close();
  }
  {
    const {page:p,context}=await page();await find(p,'300000104');assert.match(await p.locator('#lookupStatus').innerText(),/Выберите фирму/);
    await p.locator('#companyResults button').nth(1).click();await program(p);assert.equal((await send(p)).company_id,9);
    results.push('multiple-companies');await context.close();
  }
  {
    const {page:p,context}=await page();await find(p,'300000102');assert.equal(await p.locator('#pharmacyChoice').isVisible(),false);
    await p.locator('[name=create_company]').check();await p.locator('[name=company_name]').fill('ООО Новая фирма');await program(p);
    const value=await send(p);assert.equal(value.create_company,true);assert.equal(value.company_name,'ООО Новая фирма');assert.equal(value.company_id,'');
    results.push('explicit-new-company-consent');await context.close();
  }
  {
    const {page:p,context}=await page();await find(p);await p.locator('[name=inn]').fill('300000102');
    assert.equal(await p.locator('#pharmacyChoice').isVisible(),false);await p.locator('[type=submit]').click();assert.match(await p.locator('#error').innerText(),/Сначала найдите/);assert.equal(await p.evaluate(()=>window.sent.length),0);
    results.push('changed-inn-invalidates-selection');await context.close();
  }
  {
    const {page:p,context}=await page();await find(p,'300000199');assert.match(await p.locator('#error').innerText(),/Б24 пока/);assert.equal(await p.locator('#companyConsent').isVisible(),false);assert.equal(await p.locator('#pharmacyChoice').isVisible(),false);
    results.push('lookup-error-does-not-offer-company-creation');await context.close();
  }
  {
    const {page:p,context,errors}=await page('support');await find(p);await p.locator('#unknownType').click();await p.locator('[name=request_description]').fill('Программа не открывается');
    const value=await send(p);assert.equal(value.pharmacy_kind,'company');assert.equal(value.company_id,7);assert.ok(value.support_type);assert.deepEqual(errors,[]);results.push('support-on-company');await context.close();
  }
  {
    const {page:p,context,errors}=await page('pharmacy');await find(p,'300000102');await p.locator('[name=create_company]').check();await p.locator('[name=company_name]').fill('ООО Новая фирма');
    assert.equal(await p.locator('#pharmacyData').isVisible(),true);assert.equal(await p.locator('#pointChoice').isVisible(),false);
    assert.equal(await p.locator('[name=pharmacy_kind]').inputValue(),'new');assert.deepEqual(errors,[]);results.push('standalone-new-pharmacy');await context.close();
  }
  {
    const {page:p,context,errors}=await page();await find(p);await p.locator('#language').click();assert.match(await p.locator('#lookupStatus').innerText(),/дорихоналар/);assert.deepEqual(errors,[]);results.push('uzbek-language');await context.close();
  }
  await browser.close();fs.writeFileSync(path.join(output,'result.json'),JSON.stringify({ok:true,scenarios:results},null,2));console.log(JSON.stringify({ok:true,scenarios:results}));
})().catch(error=>{console.error(error);process.exit(1);});
