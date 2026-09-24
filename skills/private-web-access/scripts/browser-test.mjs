import { createRequire } from "node:module";
import { execFileSync, spawn } from "node:child_process";
import { mkdtempSync, readFileSync, existsSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
const require=createRequire(import.meta.url);
const {chromium}=require(process.env.PRIVATE_WEB_PLAYWRIGHT || "playwright");
const dir=mkdtempSync(join(tmpdir(),"private-web-browser-"));
let child,browser;
const assert=(b)=>{if(!b)throw Error("BROWSER_ASSERTION_FAILED")};
try {
 const bin=join(dir,"fixture"),ready=join(dir,"ready.json");
 execFileSync(process.env.GO || "go",["test","-c","-o",bin,"./internal/gateway"],{stdio:"pipe"});
 child=spawn(bin,["-test.run=^TestBrowserFixture$"],{env:{...process.env,PRIVATE_WEB_BROWSER_FIXTURE:ready},stdio:["pipe","pipe","pipe"]});
 for(let i=0;i<200&&!existsSync(ready);i++){if(child.exitCode!==null)throw Error("FIXTURE_FAILED");await new Promise(r=>setTimeout(r,50))}
 const c=JSON.parse(readFileSync(ready,"utf8"));
 browser=await chromium.launch({headless:true,executablePath:process.env.PRIVATE_WEB_CHROMIUM,args:["--no-proxy-server","--host-resolver-rules=MAP gateway.example.test 127.0.0.1"]});
 const ctx=await browser.newContext({ignoreHTTPSErrors:true});const page=await ctx.newPage();
 const dev=await ctx.newCDPSession(page);await dev.send("WebAuthn.enable");
 await dev.send("WebAuthn.addVirtualAuthenticator",{options:{protocol:"ctap2",transport:"internal",hasResidentKey:true,hasUserVerification:true,isUserVerified:true,automaticPresenceSimulation:true}});
 await page.goto(c.origin+"/login#enroll="+c.token);
 await page.locator("#auth-register").click();await page.waitForURL(c.origin+"/");
 assert((await page.evaluate(()=>fetch("/auth/status").then(r=>r.json()))).authenticated);
 await page.goto(c.origin+"/apps/console/");assert(await page.locator("#content").count()===1);
 assert(await page.evaluate(()=>fetch("/console/action",{method:"POST"}).then(r=>r.status))===200);
 await page.goto(c.origin+"/apps/library/");assert(page.url()===c.isolated+"/");
 assert(await page.evaluate(()=>fetch("/action",{method:"POST"}).then(r=>r.status))===405);
 assert(await page.evaluate(()=>fetch("/auth/status").then(r=>r.status))===404);
 // Cross-port scripts cannot use a same-site request to act on the login origin.
 const blocked=await page.evaluate(async origin=>{try{await fetch(origin+"/console/action",{method:"POST",credentials:"include"});return false}catch{return true}},c.origin);assert(blocked);
 await page.goto(c.origin+"/login");await page.locator("#auth-logout-all").click();
 await page.waitForFunction(async()=>!(await fetch("/auth/status").then(r=>r.json())).authenticated);
 await page.goto(c.isolated+"/");assert(page.url().startsWith(c.origin+"/login?next=library"));
 await page.locator("#auth-login").click();await page.waitForURL(c.isolated+"/");
 console.log("BROWSER_PASSKEY_AND_BOUNDARIES_PASS");
} catch { console.error("BROWSER_CHECK_FAILED"); process.exitCode=1; }
finally {if(browser)await browser.close();if(child){child.stdin.end();await new Promise(r=>{if(child.exitCode!==null)return r();child.once("exit",r);setTimeout(()=>child.kill(),5000).unref()})}rmSync(dir,{recursive:true,force:true})}
