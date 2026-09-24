"use strict";
window.AccessAuth=(()=>{
 const decode=s=>Uint8Array.from(atob(s.replace(/-/g,"+").replace(/_/g,"/")),c=>c.charCodeAt(0));
 const encode=b=>b==null?null:btoa(String.fromCharCode(...new Uint8Array(b))).replace(/\+/g,"-").replace(/\//g,"_").replace(/=+$/,"");
 async function call(path,body){
  const r=await fetch(path,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body),cache:"no-store"});
  if(!r.ok)throw new Error("AUTH_FAILED");return r.json();
 }
 async function ceremony(kind,token=""){
  if(!window.isSecureContext||!navigator.credentials)throw new Error("SECURE_CONTEXT_REQUIRED");
  const options=(await call(`/auth/${kind}/begin`,{token})).publicKey;
  options.challenge=decode(options.challenge);
  if(options.user)options.user.id=decode(options.user.id);
  for(const key of ["allowCredentials","excludeCredentials"])for(const c of options[key]||[])c.id=decode(c.id);
  const credential=kind==="register"?await navigator.credentials.create({publicKey:options}):await navigator.credentials.get({publicKey:options});
  if(!credential)throw new Error("AUTH_CANCELLED");
  const response={clientDataJSON:encode(credential.response.clientDataJSON)};
  if(kind==="register"){
   response.attestationObject=encode(credential.response.attestationObject);
   response.transports=credential.response.getTransports?.()||[];
  }else{
   response.authenticatorData=encode(credential.response.authenticatorData);response.signature=encode(credential.response.signature);response.userHandle=encode(credential.response.userHandle);
  }
  await call(`/auth/${kind}/finish`,{id:credential.id,rawId:encode(credential.rawId),type:credential.type,authenticatorAttachment:credential.authenticatorAttachment,response,clientExtensionResults:credential.getClientExtensionResults()});
 }
 async function status(){const r=await fetch("/auth/status",{cache:"no-store"});if(!r.ok)throw new Error("AUTH_FAILED");return r.json();}
 async function logout(all=false){const r=await fetch(all?"/auth/logout-all":"/auth/logout",{method:"POST",headers:{"X-Access-Action":"logout"}});if(!r.ok)throw new Error("AUTH_FAILED");}
 return {ceremony,status,logout,call};
})();
if(document.getElementById("auth-login")){
 const $=id=>document.getElementById(id);
 const next=new URLSearchParams(location.search).get("next");
 const destination=/^[a-z][a-z0-9-]{0,31}$/.test(next||"")?"/apps/"+next+"/":"/";
 const loginPath=destination!=="/"?"/login?next="+next:"/login";
 $("auth-back").href=destination;$("auth-back").textContent="返回工作台";
 let invitation=new URLSearchParams(location.hash.slice(1)).get("enroll")||"";
 history.replaceState(null,"",loginPath);
 window.addEventListener("hashchange",()=>{invitation=new URLSearchParams(location.hash.slice(1)).get("enroll")||"";history.replaceState(null,"",loginPath);refresh().catch(()=>{});});
 async function refresh(){
  const s=await AccessAuth.status();
  $("auth-login").hidden=s.authenticated;$("auth-register").hidden=!invitation&&!s.authenticated;$("auth-back").hidden=!s.authenticated;$("auth-logout-all").hidden=!s.authenticated;
  $("auth-register").textContent=s.authenticated?"添加备用通行密钥":"在此设备创建通行密钥";
  $("auth-description").textContent=s.authenticated?"登录成功。可为电脑、手机或安全钥匙添加备用凭据，也可移除丢失设备的登录权限。":invitation?"这是十分钟内有效的一次性绑定入口。确认后，登录私钥由你的设备或密钥管理器保管。":"使用你已绑定的通行密钥登录。首次绑定或恢复访问，请在主机生成新的绑定邀请。";
  $("auth-credentials").replaceChildren();
  if(s.authenticated){
   const r=await fetch("/auth/credentials",{cache:"no-store"});if(!r.ok)throw new Error("AUTH_FAILED");
   for(const c of await r.json()){
    const row=document.createElement("p"),button=document.createElement("button");row.textContent=`凭据 ${c.slot} · ${c.synced?"支持同步":"设备绑定"} `;
    button.className="secondary danger";button.textContent="移除";button.disabled=s.credentials<=1;
    button.onclick=()=>run(async()=>{
     if(!confirm("移除此凭据，并立即终止它的现有登录会话？"))return;
     await AccessAuth.ceremony("login");await AccessAuth.call("/auth/credentials/remove",{handle:c.handle});await refresh();
    });row.append(button);$("auth-credentials").append(row);
   }
  }
 }
 async function run(fn){
  for(const b of document.querySelectorAll("button"))b.disabled=true;$("auth-message").textContent="请按浏览器提示，在手机或安全钥匙上确认…";
  try{await fn();$("auth-message").textContent="操作已完成。";}catch{$("auth-message").textContent="操作未完成。可重试；绑定链接失效时，在主机生成新的绑定邀请。";}
  finally{for(const b of document.querySelectorAll("button"))b.disabled=false;await refresh().catch(()=>{});}
 }
 $("auth-login").onclick=()=>run(async()=>{await AccessAuth.ceremony("login");location.replace(destination);});
 $("auth-register").onclick=()=>run(async()=>{
  if(!invitation)await AccessAuth.ceremony("login");await AccessAuth.ceremony("register",invitation);invitation="";location.replace(destination);
 });
 $("auth-logout-all").onclick=()=>run(async()=>{await AccessAuth.logout(true);await refresh();});
 refresh().catch(()=>{$("auth-message").textContent="认证服务暂时不可用。";});
}
