import assert from 'node:assert/strict';
import {readFile,writeFile} from 'node:fs/promises';
import {chromium} from 'playwright';
const fixture=JSON.parse(await readFile(process.argv[2],'utf8'));
const browser=await chromium.launch({headless:true,args:['--no-proxy-server','--host-resolver-rules=MAP gateway.example.test 127.0.0.1']});
// This exception applies only to the generated synthetic TLS certificate.
const context=await browser.newContext({ignoreHTTPSErrors:true});
const page=await context.newPage();
const external=[],errors=[],failed=[];
try {
 page.on('pageerror',e=>errors.push(e.message));
 page.on('requestfailed',r=>failed.push({url:r.url(),error:r.failure()?.errorText}));
 // Observe actual requests; no mock of gateway headers, authentication or CSP.
 page.on('request',r=>{
  if(![fixture.origin,fixture.loginOrigin].includes(new URL(r.url()).origin))external.push(r.url());
 });
 await page.goto(fixture.origin+'/files/ops/');
 assert.ok(page.url().startsWith(fixture.loginOrigin+'/login'));
 const dev=await context.newCDPSession(page);
 await dev.send('WebAuthn.enable');
 await dev.send('WebAuthn.addVirtualAuthenticator',{options:{protocol:'ctap2',transport:'internal',hasResidentKey:true,hasUserVerification:true,isUserVerified:true,automaticPresenceSimulation:true}});
 await page.goto(fixture.enrollment);
 await page.locator('#auth-register').click();
 await page.waitForURL(fixture.loginOrigin+'/');
 await page.goto(fixture.origin+'/files/ops/');
 await page.getByText('root-report.md',{exact:true}).first().waitFor({timeout:15000});
 await page.screenshot({path:fixture.artifacts+'/quantum-desktop.png',fullPage:true});
 await page.goto(fixture.origin+'/tasks/demo/report.md?v');
 await page.getByRole('heading',{name:'Live report',exact:true}).waitFor({timeout:15000});
 assert.equal(await page.locator('.markdown-content strong').textContent(),'Bold');
 assert.ok(await page.locator('.markdown-content table tr').count()>=2);
 assert.equal(await page.evaluate(()=>window.injected),undefined);
 assert.equal(await page.locator('a[href^="javascript:"]').count(),0);
 await page.goto(fixture.origin+'/files/ops/tasks/demo/');
 await page.getByText('report.md',{exact:true}).first().waitFor();
 await writeFile(fixture.tasks+'/demo/created-after-start.md','# New file\n');
 await page.reload();
 await page.getByText('created-after-start.md',{exact:true}).first().waitFor();
 await writeFile(fixture.tasks+'/demo/report.md','# Updated report\n');
 await page.goto(fixture.origin+'/files/ops/tasks/demo/report.md');
 await page.getByRole('heading',{name:'Updated report',exact:true}).waitFor();
 await page.setViewportSize({width:390,height:844});
 await page.screenshot({path:fixture.artifacts+'/quantum-mobile.png',fullPage:true});
 await writeFile(fixture.artifacts+'/browser-network.json',JSON.stringify({external,failed,errors},null,2));
 assert.deepEqual(external.filter(u=>!u.startsWith('https://example.invalid/')),[]);
 // The malicious Markdown image is refused by the real response CSP.
 assert.ok(failed.some(r=>r.url.startsWith('https://example.invalid/')&&r.error==='csp'));
 assert.equal(await page.evaluate(()=>fetch('/api/resources',{method:'POST'}).then(r=>r.status)),405);
 assert.equal(await page.evaluate(()=>fetch('/api/auth/token').then(r=>r.status)),403);
 await page.goto(fixture.loginOrigin+'/login');
 await page.locator('#auth-logout-all').click();
 await page.waitForFunction(async()=>!(await fetch('/auth/status').then(r=>r.json())).authenticated);
 await page.goto(fixture.origin+'/files/ops/');
 assert.ok(page.url().startsWith(fixture.loginOrigin+'/login'));
 assert.deepEqual(errors,[]);
 console.log('OK browser: real gateway + virtual Passkey, logout revocation, fresh local files, Markdown, mobile, script sanitization and CSP');
} catch(e) {
 await writeFile(fixture.artifacts+'/browser-failure.json',JSON.stringify({url:page.url(),text:(await page.locator('body').innerText()).slice(0,5000),external,errors,failed},null,2));
 await page.screenshot({path:fixture.artifacts+'/browser-failure.png',fullPage:true});
 throw e;
} finally {await browser.close();}
