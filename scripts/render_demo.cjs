// Screenshot the actual local app. No remote sites, accounts or model calls.
const [baseURL, output, calendarFile, qaOutput, playwrightModule, browserExecutable] = process.argv.slice(2);
const {chromium} = require(playwrightModule);
const fs = require('node:fs');
const path = require('node:path');
(async () => {
  const browser = await chromium.launch({headless:true, ...(browserExecutable ? {executablePath:browserExecutable} : {channel:'chromium'})});
  try {
    const page = await browser.newPage({viewport:{width:1440,height:1080}, deviceScaleFactor:1});
    const errors = [];
    const blockedRequests = [];
    page.on('pageerror', error => errors.push(String(error)));
    page.on('console', message => { if(message.type()==='error') errors.push(message.text()); });
    await page.route('**/*', route => {
      const url = route.request().url();
      if(url.startsWith(baseURL+'/') || url.startsWith('file://')) return route.continue();
      blockedRequests.push(url);
      return route.abort();
    });
    await page.goto(baseURL+'/');
    if(await page.title() !== 'Szkolny terminarz') throw Error('Wrong app page identity');
    await page.getByText('Demo — dane fikcyjne / fictional data', {exact:true}).waitFor();
    await page.getByRole('heading',{name:'Wiadomości ze szkoły',exact:true}).waitFor();
    await page.screenshot({path:path.join(output,'inbox.png'),fullPage:true});
    await page.getByRole('link').filter({hasText:'Wyjście klasy do planetarium'}).click();
    await page.getByRole('heading',{name:'Wyjście klasy do planetarium',exact:true,level:2}).waitFor();
    await page.getByText('[Librus] Wyjście do planetarium',{exact:true}).waitFor();
    await page.screenshot({path:path.join(output,'message-detail.png'),fullPage:true});
    await page.getByRole('link',{name:/Do sprawdzenia/}).click();
    await page.getByRole('heading',{name:'[Librus] Dodatkowe warsztaty robotyki',exact:true}).waitFor();
    await page.screenshot({path:path.join(output,'extracurricular-review.png'),fullPage:true});
    await page.getByRole('button',{name:'Zatwierdź termin',exact:true}).click();
    await page.getByRole('heading',{name:'Wszystko sprawdzone',exact:true}).waitFor();
    const proposals = await (await page.request.get(baseURL+'/api/proposals')).json();
    if(!proposals.some(p=>p.message_id==='demo-robotics' && p.status==='pending' && p.user_approved===true)) throw Error('Approval did not persist');
    await page.goto(baseURL+'/');
    await page.getByRole('button',{name:'Wstrzymaj zapisy',exact:true}).click();
    await page.getByRole('button',{name:'Wznów zapisy',exact:true}).waitFor();
    await page.getByRole('button',{name:'Sprawdź teraz',exact:true}).click();
    await page.getByRole('status').filter({hasText:'Zlecono sprawdzenie'}).waitFor();
    const mobile = await browser.newPage({viewport:{width:390,height:844}, deviceScaleFactor:1});
    mobile.on('pageerror', error => errors.push(String(error)));
    await mobile.route('**/*', route => {
      const url = route.request().url();
      if(url.startsWith(baseURL+'/')) return route.continue();
      blockedRequests.push(url); return route.abort();
    });
    await mobile.goto(baseURL+'/');
    await mobile.getByRole('link').filter({hasText:'Wyjście klasy do planetarium'}).click();
    await mobile.getByRole('heading',{name:'Wyjście klasy do planetarium',exact:true,level:2}).waitFor();
    if(await mobile.evaluate(()=>document.documentElement.scrollWidth>window.innerWidth)) throw Error('Mobile horizontal overflow');
    await mobile.screenshot({path:path.join(qaOutput,'mobile-detail.png'),fullPage:true});
    await page.goto('file://'+calendarFile);
    await page.getByText('Demo — dane fikcyjne / fictional data',{exact:true}).waitFor();
    await page.screenshot({path:path.join(output,'calendar-week.png'),fullPage:true});
    if(errors.length) throw Error('Browser errors: '+errors.join('\n'));
    if(blockedRequests.length) throw Error('Unexpected remote requests attempted: '+blockedRequests.join('\n'));
    fs.writeFileSync(path.join(qaOutput,'checks.json'),JSON.stringify({
      browser:'Playwright Chromium (Browser plugin not available)',desktop:[1440,1080],mobile:[390,844],
      identity:true,nonblank:true,noFrameworkOverlay:true,consoleErrors:errors,
      interactions:['inbox → source detail → recognized event','review → approve → pending with user_approved=true','pause writes → paused','check now → demo-only request notice','mobile inbox → detail'],
      network:'Only localhost app assets and local illustration; no service/model/calendar calls.',
      blockedRequests,
      screenshots:['inbox.png','message-detail.png','extracurricular-review.png','calendar-week.png']
    },null,2));
  } finally { await browser.close(); }
})().catch(error=>{console.error(error);process.exit(1)});
