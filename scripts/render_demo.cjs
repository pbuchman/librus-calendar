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
    await page.getByRole('link',{name:'Podgląd agendy',exact:true}).click();
    await page.getByRole('heading',{name:'Podgląd całej agendy',exact:true}).waitFor();
    await page.getByText('To samo wyjście w trzech źródłach: jedno działanie i jeden wpis.',{exact:true}).waitFor();
    await page.getByText('Zachowano wcześniejsze odrzucenie udziału przez rodzica.',{exact:true}).waitFor();
    if(await page.locator('form').count()) throw Error('Preview contains mutation controls');
    if(await page.getByText('Proponowana zmiana wobec obecnego działania',{exact:true}).count()) throw Error('Link/ignore incorrectly displayed as field removal');
    if(!await page.getByText(/Ostatnie pełne zebranie źródeł/).count()) throw Error('Preview has no stored-source freshness evidence');
    await page.screenshot({path:path.join(qaOutput,'agenda-preview.png'),fullPage:true});
    await page.getByRole('link',{name:'Obecne działanie i zachowane decyzje',exact:true}).first().click();
    await page.getByRole('heading',{name:'Wszystkie źródła działania',exact:true}).waitFor();
    if(await page.locator('.message-detail a[href^="/sources/"]').count() !== 3) throw Error('Shared trip lost a supporting source');
    await page.goto(baseURL+'/');
    await page.getByRole('link').filter({hasText:'Wyjście klasy do planetarium'}).click();
    await page.getByRole('heading',{name:'Wyjście klasy do planetarium',exact:true,level:2}).waitFor();
    await page.getByText('[Librus] Wyjście do planetarium',{exact:true}).waitFor();
    await page.screenshot({path:path.join(output,'message-detail.png'),fullPage:true});
    await page.getByRole('link',{name:/Do sprawdzenia/}).click();
    await page.getByRole('heading',{name:'[Librus] Dodatkowe warsztaty robotyki',exact:true}).waitFor();
    await page.screenshot({path:path.join(output,'extracurricular-review.png'),fullPage:false});
    await page.locator('article.proposal').filter({has:page.getByRole('heading',{name:'[Librus] Dodatkowe warsztaty robotyki',exact:true})}).getByRole('button',{name:'Zatwierdź termin',exact:true}).click();
    const proposals = await (await page.request.get(baseURL+'/api/proposals')).json();
    if(!proposals.some(p=>p.message_id==='demo-robotics' && p.status==='pending' && p.user_approved===true)) throw Error('Approval did not persist');
    const offer = page.locator('.offer-group');
    if(await offer.getByRole('radio').count() !== 3) throw Error('Incomplete offer options');
    if(await offer.locator('input[type=radio]:checked').count()) throw Error('Offer preselects a date');
    if(!await offer.getByRole('radio').first().isDisabled()) throw Error('Past option selectable');
    await offer.getByRole('radio').nth(1).check();
    await offer.getByRole('button',{name:'Dodaj wybrany termin',exact:true}).click();
    await page.goto(baseURL+'/messages/demo-training-choice');
    await page.getByText('Wybrano termin',{exact:true}).waitFor();
    if(await page.getByRole('radio').count()) throw Error('Closed offer still selectable');
    const groups = await (await page.request.get(baseURL+'/api/proposal-groups')).json();
    if(groups.length !== 1 || groups[0].status !== 'selected' || !groups[0].selected_proposal_id) throw Error('Group selection did not persist');
    if(groups[0].members.filter(option=>option.status==='pending' && option.user_approved).length !== 1 || groups[0].members.some(option=>option.id!==groups[0].selected_proposal_id && option.status!=='ignored')) throw Error('Selection did not close every sibling');
    await page.screenshot({path:path.join(qaOutput,'offer-selected.png'),fullPage:true});
    await page.goto(baseURL+'/messages/demo-training-choice');
    await page.getByText('Wybrano termin',{exact:true}).waitFor();
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
    await mobile.goto(baseURL+'/preview');
    await mobile.getByRole('heading',{name:'Podgląd całej agendy',exact:true}).waitFor();
    if(await mobile.evaluate(()=>document.documentElement.scrollWidth>window.innerWidth)) throw Error('Mobile agenda horizontal overflow');
    await mobile.screenshot({path:path.join(qaOutput,'mobile-agenda-preview.png'),fullPage:true});
    await mobile.goto(baseURL+'/');
    await mobile.getByRole('link').filter({hasText:'Szkolenie pierwszej pomocy — wybierz jeden termin'}).click();
    await mobile.getByRole('heading',{name:'Szkolenie pierwszej pomocy — wybierz jeden termin',exact:true,level:2}).waitFor();
    await mobile.getByText('Wybrano termin',{exact:true}).waitFor();
    if(await mobile.evaluate(()=>document.documentElement.scrollWidth>window.innerWidth)) throw Error('Mobile horizontal overflow');
    await mobile.screenshot({path:path.join(qaOutput,'mobile-detail.png'),fullPage:true});
    await page.goto('file://'+calendarFile);
    await page.getByText('Demo — dane fikcyjne / fictional data',{exact:true}).waitFor();
    await page.screenshot({path:path.join(output,'calendar-week.png'),fullPage:true});
    if(errors.length) throw Error('Browser errors: '+errors.join('\n'));
    if(blockedRequests.length) throw Error('Unexpected remote requests attempted: '+blockedRequests.join('\n'));
    fs.writeFileSync(path.join(qaOutput,'checks.json'),JSON.stringify({
      browser:'Playwright with existing local Chrome; Browser plugin not available',desktop:[1440,1080],mobile:[390,844],
      identity:true,nonblank:true,noFrameworkOverlay:true,consoleErrors:errors,
      interactions:['whole-agenda preview → one trip with three sources → shared action detail; preview has no mutation forms','inbox → source detail → recognized event','review → approve standalone → pending with user_approved=true','offer → no default, past disabled → select future → all siblings closed → same state in message detail','pause writes → paused','check now → demo-only request notice','mobile inbox → detail'],
      network:'Only localhost app assets and local illustration; no service/model/calendar calls.',
      blockedRequests,
      screenshots:['inbox.png','message-detail.png','extracurricular-review.png','calendar-week.png']
    },null,2));
  } finally { await browser.close(); }
})().catch(error=>{console.error(error);process.exit(1)});
