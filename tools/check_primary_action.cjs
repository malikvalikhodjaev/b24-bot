/* Exercise native Telegram and browser action controls without CRM writes. */
const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const base=process.env.PRIMARY_QA_URL||'http://127.0.0.1:18773/';

(async()=>{
  const browser=await chromium.launch({headless:true});const scenarios=[];
  async function open({platform='android',mode='deal',available=true,broken=false,noTelegram=false}={}){
    const context=await browser.newContext({viewport:{width:390,height:844}});
    await context.addInitScript(options=>{
      window.sent=[];window.bindings=0;
      if(options.noTelegram)return;
      const main={isVisible:false,isActive:true,progress:false,text:'',
        onClick(callback){window.nativeAction=callback;window.bindings++;},
        setText(text){this.text=text;},show(){if(options.broken)throw new Error('Unavailable native control');this.isVisible=true;},
        hide(){this.isVisible=false;},disable(){this.isActive=false;},enable(){this.isActive=true;},
        showProgress(){this.progress=true;this.isActive=false;},hideProgress(){this.progress=false;}};
      if(!options.available)delete main.onClick;
      window.Telegram={WebApp:{platform:options.platform,MainButton:main,ready(){},expand(){},
        sendData(payload){if(window.failSend)throw new Error('Transport unavailable');window.sent.push(JSON.parse(payload));}}};
    },{platform,available,broken,noTelegram});
    const page=await context.newPage();const errors=[];page.on('pageerror',error=>errors.push(error.message));
    await page.route('https://core.telegram.org/js/telegram-web-app.js',route=>route.fulfill({body:'',contentType:'text/javascript'}));
    await page.goto(base+`?mode=${mode}&lang=ru&token=datfo-form-${'a'.repeat(32)}`);
    await page.waitForFunction(()=>document.querySelector('[name=business_region]').options.length>1);
    return {page,context,errors};
  }
  try{
    for(const [platform,mode,label] of [['android','deal','Создать сделку →'],['tdesktop','support','Отправить заявку →'],['ios','pharmacy','Добавить аптеку →']]){
      const {page,context,errors}=await open({platform,mode});
      assert.equal(await page.locator('[type=submit]').isVisible(),false);
      assert.deepEqual(await page.evaluate(()=>({visible:Telegram.WebApp.MainButton.isVisible,text:Telegram.WebApp.MainButton.text,bindings})),{visible:true,text:label,bindings:1});
      assert.deepEqual(errors,[]);await context.close();scenarios.push(platform+'-'+mode+'-one-native-action');
    }
    for(const options of [{platform:'unknown'},{noTelegram:true},{available:false},{broken:true}]){
      const {page,context,errors}=await open(options);
      assert.equal(await page.locator('[type=submit]').isVisible(),true);
      assert.equal(await page.evaluate(()=>window.Telegram?.WebApp.MainButton.isVisible||false),false);
      assert.deepEqual(errors,[]);await context.close();scenarios.push('inline-fallback-'+Object.keys(options)[0]);
    }
    {
      const {page,context,errors}=await open();
      await page.evaluate(()=>window.nativeAction());
      assert.equal(await page.locator('[name=deal_title]').evaluate(node=>node.validity.valid),false);
      assert.equal(await page.evaluate(()=>window.sent.length),0);
      await page.locator('[name=deal_title]').fill('Встреча с клиентом');
      await page.locator('[name=discussion_programs]').first().check();
      await page.evaluate(()=>{window.nativeAction();window.nativeAction();});
      assert.deepEqual(await page.evaluate(()=>({sent:window.sent.length,progress:Telegram.WebApp.MainButton.progress,active:Telegram.WebApp.MainButton.isActive,text:Telegram.WebApp.MainButton.text})),
        {sent:1,progress:true,active:false,text:'⏳ Подождите, готовим сделку…'});
      assert.equal(await page.locator('[type=submit]').isVisible(),false);
      assert.deepEqual(errors,[]);await context.close();scenarios.push('native-validation-and-single-send-with-progress');
    }
    {
      const {page,context,errors}=await open();
      await page.locator('[name=deal_title]').fill('Позвонить клиенту');
      await page.locator('[name=discussion_programs]').first().check();
      await page.evaluate(()=>{window.failSend=true;window.nativeAction();});
      assert.equal(await page.evaluate(()=>Telegram.WebApp.MainButton.isActive&&!Telegram.WebApp.MainButton.progress),true);
      assert.equal(await page.evaluate(()=>Telegram.WebApp.MainButton.text),'Создать сделку →');
      await page.evaluate(()=>{window.failSend=false;window.nativeAction();});
      assert.equal(await page.evaluate(()=>window.sent.length),1);
      assert.deepEqual(errors,[]);await context.close();scenarios.push('failed-send-restores-native-action-and-retry');
    }
    const result={ok:true,scenarios};
    fs.mkdirSync(path.resolve(__dirname,'../data/primary-action-qa'),{recursive:true});
    fs.writeFileSync(path.resolve(__dirname,'../data/primary-action-qa/result.json'),JSON.stringify(result,null,2));
    console.log(JSON.stringify(result));
  }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
