"use strict";
globalThis.XIXI_ADMIN_BASE=new URL(document.currentScript.src).pathname.split('/static/')[0];
const form = document.getElementById("login-form");
const message = document.getElementById("message");
const button = document.getElementById("login-button");
let csrf = "";
let bootstrapping;

function loginDestination() {
  return form.dataset.destination || (globalThis.XIXI_ADMIN_BASE || "") + "/";
}
async function request(path, options = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 15000);
  try {
    const response = await fetch(path.startsWith((globalThis.XIXI_ADMIN_BASE||'')+'/')?path:(globalThis.XIXI_ADMIN_BASE||'')+path, {...options, cache: "no-store", credentials: "same-origin", signal: controller.signal});
    const data = await response.json();
    return {response, data};
  } catch (error) {
    throw new Error(error.name === "AbortError"
      ? "连接超时，请检查服务器联网后重试。"
      : "连接中断，请稍后重试；若仍有问题，请刷新页面。");
  } finally { clearTimeout(timer); }
}
function bootstrap() {
  if (bootstrapping) return bootstrapping;
  bootstrapping = (async () => {
    const {response, data} = await request(((globalThis.XIXI_ADMIN_BASE||'')+"/v1/session"));
    if (!response.ok) throw new Error("此入口不可访问，请检查内网与 HTTPS 配置。");
    csrf = data.csrf;
    if (data.authenticated) window.location.replace(loginDestination());
  })().finally(() => { bootstrapping = undefined; });
  return bootstrapping;
}
form.addEventListener("submit", async event => {
  event.preventDefault(); button.disabled = true; button.textContent = "正在登录…"; message.textContent = "";
  const input = document.getElementById("password");
  try {
    if (!csrf) await bootstrap();
    const {response, data} = await request(((globalThis.XIXI_ADMIN_BASE||'')+"/v1/login"), {method: "POST", headers: {"Content-Type": "application/json", "X-CSRF-Token": csrf}, body: JSON.stringify({password: input.value})});
    if (response.status === 429) throw new Error("登录尝试过多，请在 15 分钟后重试。");
    if (!response.ok || !data.authenticated) throw new Error("登录失败，请检查密码或刷新页面。");
    window.location.replace(loginDestination());
  } catch (error) { message.textContent = error.message; }
  finally { input.value = ""; button.disabled = false; button.textContent = "登录管理台"; }
});
bootstrap().catch(error => { message.textContent = error.message; });
