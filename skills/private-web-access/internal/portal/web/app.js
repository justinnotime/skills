"use strict";
const $=id=>document.getElementById(id);
async function refreshAccess(){
 try{
  const s=await AccessAuth.status();
  if(s.enabled&&!s.authenticated){location.replace("/login");return;}
  $("auth-manage").hidden=!s.enabled;$("auth-logout").hidden=!s.enabled;
  $("access-state").textContent=s.enabled?"已通过通行密钥登录":"本机访问";
 }catch{$("access-state").textContent="登录状态暂时不可用";}
}
$("auth-logout").onclick=async()=>{
 $("auth-logout").disabled=true;
 try{await AccessAuth.logout();location.replace("/login");}
 catch{$("portal-message").textContent="退出未确认，请重试。";$("auth-logout").disabled=false;}
};
document.addEventListener("visibilitychange",()=>{if(!document.hidden)refreshAccess();});
refreshAccess();
