"use strict";
(() => {
  const $ = id => document.getElementById(id);
  const API = location.hostname === "kshao0315.github.io" ? "https://terminal-203-160-55-78.sslip.io" : location.origin;
  const fragment = new URLSearchParams(location.hash.slice(1));
  const token = fragment.get("session") || sessionStorage.getItem("terminalfaker-session") || "";
  if (token) sessionStorage.setItem("terminalfaker-session", token);
  let language = fragment.get("lang") === "en" ? "en" : "zh";
  history.replaceState(null, "", location.pathname + location.search);
  let busy = false, commandHistory = [], historyIndex = 0, joinCommand = "";
  const t = (zh, en) => language === "en" ? en : zh;
  function labels() {
    document.documentElement.lang = language === "en" ? "en" : "zh-CN";
    $("lang").textContent = language === "en" ? "简体中文" : "English";
    $("banner").textContent = t("Android 13 · A/B · 动态分区\n设备已进入系统。输入 help 查看支持的命令。", "Android 13 · A/B · Dynamic Partitions\nAndroid is running. Type help to see the supported commands.");
    $("notice").textContent = t("模拟终端 · 进度自动保存 · 每次输入一条命令", "Simulated terminal · Progress saved automatically · One command at a time");
    $("copy").textContent = t("复制 /join 指令", "Copy /join command");
  }
  function render(data) {
    $("mode").textContent = data.mode;
    $("prompt").textContent = data.prompt;
    $("output").replaceChildren();
    for (const entry of data.transcript || []) {
      const wrap = document.createElement("div"); wrap.className = "entry";
      const input = document.createElement("pre"); input.className = "input-line";
      input.textContent = entry.prompt + entry.command;
      const output = document.createElement("pre"); output.className = "result"; output.textContent = entry.output;
      wrap.append(input, output); $("output").append(wrap);
    }
    if (data.join_command) joinCommand = data.join_command;
    $("copy").hidden = !joinCommand;
    $("screen").scrollTop = $("screen").scrollHeight;
  }
  function error(message) {
    const line = document.createElement("pre"); line.className = "error"; line.textContent = message;
    $("output").append(line); $("screen").scrollTop = $("screen").scrollHeight;
  }
  async function request(route, payload) {
    const ctrl = new AbortController(), timer = setTimeout(() => ctrl.abort(), 15000);
    try {
      const response = await fetch(API + route, {method: payload ? "POST" : "GET", cache: "no-store", signal: ctrl.signal,
        headers: {Authorization: "Bearer " + token, ...(payload ? {"Content-Type":"application/json"} : {})},
        ...(payload ? {body: JSON.stringify(payload)} : {})});
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "Request failed");
      return data;
    } finally { clearTimeout(timer); }
  }
  $("lang").onclick = () => {language = language === "en" ? "zh" : "en"; labels();};
  $("copy").onclick = async () => {
    try {await navigator.clipboard.writeText(joinCommand); $("copy").textContent = t("已复制", "Copied");}
    catch {error(joinCommand);}
  };
  $("command").addEventListener("keydown", e => {
    if (e.key === "ArrowUp") {e.preventDefault(); historyIndex = Math.max(0, historyIndex - 1); e.target.value = commandHistory[historyIndex] || "";}
    if (e.key === "ArrowDown") {e.preventDefault(); historyIndex = Math.min(commandHistory.length, historyIndex + 1); e.target.value = commandHistory[historyIndex] || "";}
  });
  $("terminal").onsubmit = async event => {
    event.preventDefault(); const command = $("command").value.trim(); if (!command || busy) return;
    busy = true; $("send").disabled = true; $("command").disabled = true;
    const payload = {command, op_id: crypto.randomUUID()};
    try {
      let data;
      try {data = await request("/api/command", payload);}
      catch (first) {if (first.name === "AbortError" || first instanceof TypeError) data = await request("/api/command", payload); else throw first;}
      render(data); commandHistory.push(command); historyIndex = commandHistory.length; $("command").value = "";
    } catch (err) {error(t("请求失败：", "Request failed: ") + (err.name === "AbortError" ? t("连接超时，请重试。", "Connection timed out. Please try again.") : err.message));}
    finally {busy = false; $("send").disabled = false; $("command").disabled = false; $("command").focus();}
  };
  labels();
  (async () => {
    if (!token) {error(t("请从 Bot 私聊题目下方的按钮打开终端。", "Open this terminal using the button below the challenge in your private bot chat.")); return;}
    try {
      const data = await request("/api/session"); language = data.language === "en" ? "en" : "zh"; labels(); render(data);
      commandHistory = (data.transcript || []).map(e => e.command); historyIndex = commandHistory.length;
      $("command").disabled = false; $("send").disabled = false; $("command").focus();
    } catch (err) {error(err.message);}
  })();
})();
