"use strict";

const $ = (selector, root = document) => root.querySelector(selector);
const names = {qq: "QQ", bilibili: "B 站", douyin: "抖音"};
const emotionNames = {joy: "开心", teasing: "打趣", sleepy: "困倦", flustered: "害羞",
  sad: "难过", smug: "得意", neutral: "平静", annoyed: "烦躁"};
const stateNames = {received: "已接收", sent: "已确认", skipped: "已跳过", failed: "失败",
  uncertain: "待核查", pending: "待发送", sending: "发送中", evaluate: "待评估", evaluating: "评估中"};
const titles = {
  overview: ["总览", "查看本机服务、今日消息和最近运行记录。今日数据按北京时间统计。"],
  platforms: ["平台管理", "配置各平台的接收偏好和会话白名单。"],
  persona: ["AI 人设", "调整大肥鱼的表达、模型与接话节奏。"],
  memory: ["记忆管理", "浏览鲸小鱼的记忆、人物与经历。"],
  stickers: ["表情包", "管理 V2 图片、情绪和使用场景。"],
  messages: ["消息日志", "查看 SQLite 中的消息和发送结果。时间按当前浏览器时区显示。"],
  settings: ["系统设置", "管理发送上限、模型参数和日志级别。"],
  bilibili: ["B 站浏览", "按时段阅读真实字幕与摘要，形成经历和动态兴趣。"],
  "douyin-account": ["抖音账号", "查看应用配置、账号授权和权限。"],
  "douyin-drafts": ["内容工作台", "编写、生成和保存抖音短视频草稿。"],
};
const sessionKey = "fishbot-panel-session";
let signedIn = false;
let sequence = 0;
let previews = [];

function node(tag, text = "", className = "") {
  const el = document.createElement(tag);
  if (text) el.textContent = text;
  if (className) el.className = className;
  return el;
}

function block(...children) {
  const div = node("div");
  div.append(...children);
  return div;
}

function tag(text, kind = "") {
  return node("span", text, "tag " + kind);
}

function action(label, className = "") {
  const button = node("button", label, className);
  button.type = "button";
  return button;
}

function card(title) {
  const article = node("article", "", "card");
  if (title) article.append(node("h2", title));
  return article;
}

function empty(text = "暂无数据") {
  return node("div", text, "empty");
}

function timeLabel(timestamp) {
  return timestamp ? new Date(timestamp * 1000).toLocaleString("zh-CN", {hour12: false}) : "—";
}

async function request(path, {method = "GET", data, file, blob = false, token} = {}) {
  const headers = {};
  if (token) headers.Authorization = "Bearer " + token;
  if (file) headers["Content-Type"] = file.type;
  if (data) headers["Content-Type"] = "application/json";
  const response = await fetch("/api" + path, {
    method, headers, cache: "no-store",
    body: file || (data ? JSON.stringify(data) : undefined),
  });
  if (response.status === 401) showLogin();
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    const error = new Error(body.detail || "操作失败，请稍后重试。");
    error.status = response.status;
    throw error;
  }
  return blob ? response.blob() : response.json();
}

function showLogin() {
  signedIn = false;
  sequence += 1;
  sessionStorage.removeItem(sessionKey);
  $("#workspace").hidden = true;
  $("#login-view").hidden = false;
  $("#page").replaceChildren();
  previews.forEach(URL.revokeObjectURL);
  previews = [];
}

function toast(message, bad = false) {
  const notice = $("#notice");
  notice.textContent = message;
  notice.classList.toggle("bad", bad);
  notice.hidden = false;
}

async function perform(button, task, success, refresh = true) {
  button.disabled = true;
  try {
    const result = await task();
    toast(typeof success === "function" ? success(result) : success);
    if (refresh) await renderPage();
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
  }
}

function row(label, value) {
  const div = node("div", "", "detail-line");
  div.append(node("span", label), typeof value === "string" ? node("span", value, "muted") : value);
  return div;
}

function labeled(label, control) {
  const parent = node("label", label);
  parent.append(control);
  return parent;
}

function input(name, value, {type = "text", min, max, step, maxLength, required = false} = {}) {
  const control = node("input");
  control.name = name;
  control.type = type;
  control.value = value;
  control.required = required;
  if (min !== undefined) control.min = min;
  if (max !== undefined) control.max = max;
  if (step !== undefined) control.step = step;
  if (maxLength !== undefined) control.maxLength = maxLength;
  return control;
}

function area(name, value, maxLength, required = false) {
  const control = node("textarea");
  Object.assign(control, {name, value, maxLength, required});
  return control;
}

function check(name, selected, label, disabled = false) {
  const control = node("input");
  Object.assign(control, {type: "checkbox", name, checked: selected, disabled});
  const result = node("label");
  result.append(control, document.createTextNode(label));
  return result;
}

function emotionInputs(all, chosen) {
  const field = node("fieldset");
  const items = node("div", "", "checks");
  for (const emotion of all) items.append(check("emotions", chosen.includes(emotion), emotionNames[emotion]));
  field.append(node("legend", "情绪标签"), items);
  for (const [index, control] of Array.from(items.querySelectorAll("input")).entries()) control.value = all[index];
  return field;
}

function configActions(form, buttonLabel, handler, snapshot = null) {
  const actions = node("div", "", "actions");
  const submit = action(buttonLabel, "primary");
  submit.type = "submit";
  actions.append(submit);
  if (snapshot) {
    const restore = action("恢复上一次配置");
    restore.disabled = !snapshot.can_restore;
    restore.onclick = () => {
      if (window.confirm("恢复面板最近一次保存前的配置？")) {
        perform(restore, () => request("/settings/restore", {
          method: "POST", data: {revision: snapshot.revision},
        }), "已恢复上一次配置。");
      }
    };
    actions.append(restore);
  }
  form.append(actions);
  form.onsubmit = event => {
    event.preventDefault();
    perform(submit, () => handler(new FormData(form)), "已保存。");
  };
}

function numberField(label, name, value, min, max, step = 1) {
  return labeled(label, input(name, value, {type: "number", min, max, step, required: true}));
}

function fields(...children) {
  const container = node("div", "", "form-grid");
  container.append(...children);
  return container;
}

function setPage(root, id) {
  root.append($("#" + id).content.cloneNode(true));
}

async function overview(root) {
  const data = await request("/status");
  setPage(root, "overview-template");
  const status = $(".hero h2", root);
  status.textContent = data.database ? "大肥鱼在线" : "数据库待检查";
  const live = data.mode === "live";
  $(".hero-mode", root).textContent = live ? "真实 QQ · live" : (data.paused ? "模拟已暂停" : "本地模拟 · simulation");
  if (live) $(".hero p", root).textContent = "仅回复授权 QQ 群，发送结果可在消息日志查看。";
  const labels = {received: "今日收到消息", generated: "今日生成回复", sent: "今日实际发送",
    [live ? "proactive" : "simulated_sent"]: live ? "主动接话评估" : "模拟发送确认",
    skipped: "已跳过", failed: "失败", uncertain: "待核查"};
  const metrics = $("#metrics", root);
  for (const [key, label] of Object.entries(labels)) {
    const item = node("div", "", "card metric");
    item.append(node("span", label), node("strong", String(data.stats[key] ?? 0)));
    metrics.append(item);
  }
  const version = node("div", "", "card metric");
  const hash = node("strong", data.revision.slice(0, 8), "revision-value");
  hash.title = data.revision;
  version.append(node("span", "当前配置版本"), hash);
  metrics.append(version);
  const connections = $("#connections", root);
  connections.append(row("FastAPI 服务", tag("运行中", "good")),
    row("DeepSeek · " + data.deepseek.model,
      tag(data.deepseek.verified ? "真实 API 已验证" : (data.deepseek.configured ? "已配置 · 未验证" : "未配置 · 未验证"),
          data.deepseek.verified ? "good" : "warn")));
  for (const platform of data.platforms) {
    connections.append(row(names[platform.id], tag(connectionLabel(platform), platform.connected ? "good" : "")));
    if (platform.webui_url) {
      const link = node("a", "打开独立 QQ 扫码入口", "tag sim");
      link.href = platform.webui_url;
      link.target = "_blank";
      link.rel = "noopener";
      connections.append(row("NapCat V2", tag(platform.napcat ? "可访问" : "不可访问")), link);
    }
  }
  const logs = $("#recent-logs", root);
  for (const item of data.logs.slice(0, 6)) {
    const line = node("div", "", "log-row");
    const label = block(node("span", item.text));
    if (item.mode === "simulation") label.append(tag("simulation", "sim"));
    line.append(label, node("time", timeLabel(item.timestamp)));
    logs.append(line);
  }
  if (!data.logs.length) logs.append(empty("暂无运行日志"));
  const simulate = $("#simulate", root);
  simulate.hidden = live;
  simulate.disabled = data.paused || data.simulation_busy;
  simulate.onclick = () => perform(simulate, () => request("/runtime", {
    method: "POST", data: {action: "simulate"},
  }), result => "模拟完成 · " + (stateNames[result.state] || result.state) + (result.detail ? " · " + result.detail : ""));
  const toggle = $("#runtime-toggle", root);
  toggle.hidden = live;
  toggle.textContent = data.paused ? "恢复模拟" : "暂停模拟";
  toggle.onclick = () => perform(toggle, () => request("/runtime", {
    method: "POST", data: {action: data.paused ? "resume" : "pause"},
  }), data.paused ? "本地模拟已恢复。" : "本地模拟已暂停。");
}

function connectionLabel(platform) {
  if (platform.id === "douyin") return "消息适配未开放";
  if (platform.connected) return "OneBot 已连接";
  if (platform.webui_url) return platform.napcat ? "等待 QQ 登录" : "NapCat V2 不可访问";
  return "未连接 · 暂不可用";
}

async function platforms(root) {
  const snapshot = await request("/platforms");
  for (const platform of snapshot.platforms) {
    const section = card(names[platform.id]);
    section.append(row("连接状态", tag(connectionLabel(platform), platform.connected ? "good" : "")));
    section.append(row("真实发送授权", tag(platform.authorized_send ? "仅授权群" : "未启用", platform.authorized_send ? "good" : "warn")));
    if (platform.id === "douyin") {
      const link = node("a", "前往抖音账号管理", "tag sim");
      link.href = "#douyin-account";
      section.append(link);
      root.append(section);
      continue;
    }
    if (platform.webui_url) {
      section.append(row("QQ 登录", platform.logged_in ? "已登录" : "待扫码"),
        row("机器人账号", (platform.nickname || "鲸小鱼") + " · " + platform.account),
        row("授权群", platform.allowlist.join("、")),
        row("消息接收", platform.storage_error ? "写库异常，正在重试" : platform.receiving ? "接收中" : "未就绪"),
        row("群记忆", platform.record_memory ? (platform.receiving ? "允许记录" : "已允许，等待接收") : "已关闭"),
        row("自动回复", platform.authorized_send ? "已开启" : "已暂停或未就绪"),
        row("待核查 Outbox", String(platform.uncertain)),
        row("接收异常次数", String(platform.receive_errors)),
        row("最近入库", timeLabel(platform.last_received_at)),
        row("最近恢复", timeLabel(platform.last_recovered_at)),
        row("主动接话", platform.proactive ? "已启用" : "首次真实回复验证后启用"));
      if (platform.last_error) section.append(row("最近接收或连接异常", platform.last_error));
      if (platform.owner_dm) {
        section.append(row("主人私聊", platform.owner_dm),
          row("私聊记忆", platform.owner_record_memory ? "独立记录" : "已关闭"),
          row("主动问候", platform.owner_checkins ? "已启用 · 北京时间 10:00–21:00" : "已关闭"));
        if (platform.owner_checkins && platform.owner_checkin_at) {
          section.append(row("下一次可问候时间", timeLabel(platform.owner_checkin_at)));
        }
      }
    }
    section.append(row("发送限额", "会话 " + platform.limits.per_channel_daily + " 条/日 · 全局 "
      + platform.limits.global_daily + " 条/日"));
    section.append(row("最近异常", platform.recent_error ?
      platform.recent_error.text + (platform.recent_error.mode === "simulation" ? "（simulation）" : "") : "暂无"));
    if (platform.webui_url) {
      const link = node("a", "打开 V2 独立 NapCat", "tag sim");
      link.href = platform.webui_url;
      link.target = "_blank";
      link.rel = "noopener";
      const actions = node("div", "", "actions");
      actions.append(link);
      if (platform.enabled) {
        const memoryToggle = action(platform.record_memory ? "关闭群记忆" : "开启群记忆");
        memoryToggle.onclick = () => perform(memoryToggle, () => request("/platforms/qq/memory", {
          method: "PUT", data: {revision: snapshot.revision, enabled: !platform.record_memory},
        }), "群记忆权限已更新。");
        const replyToggle = action(platform.authorized_send ? "暂停 QQ 自动回复" : "恢复 QQ 自动回复");
        replyToggle.disabled = !platform.authorized_send && (!platform.logged_in || !platform.receiving);
        replyToggle.onclick = () => {
          const enabled = !platform.authorized_send;
          if (enabled && !confirm("恢复对新消息的自动回复？已有的待核查任务会保留，也不会补发暂停期间的消息。")) return;
          perform(replyToggle, () => request("/platforms/qq/replies", {
            method: "PUT", data: {revision: snapshot.revision, enabled},
          }), enabled ? "已恢复对新消息的自动回复。" : "已暂停自动回复，消息仍可接收。");
        };
        actions.append(memoryToggle, replyToggle);
      }
      section.append(actions);
      root.append(section);
      continue;
    }
    const form = node("form");
    const checks = node("div", "", "checks");
    checks.append(check("enabled", platform.enabled, "预设启用"), check("receive", platform.receive, "预设接收"),
      check("send", false, "真实发送（未开放）", true));
    const box = area("allowlist", platform.allowlist.join("\n"), 18000);
    form.append(checks, labeled("会话白名单（每行一个编号）", box));
    const help = node("small", "启用和接收是保存到配置的偏好，当前阶段不启动平台连接。");
    form.append(help);
    configActions(form, "保存平台配置", values => request("/platforms/" + platform.id, {
      method: "PUT", data: {revision: snapshot.revision, enabled: values.has("enabled"),
        receive: values.has("receive"), send: false,
        allowlist: String(values.get("allowlist")).split(/\s+/).filter(Boolean)},
    }));
    section.append(form);
    root.append(section);
  }
}

function basicSettingsData(snapshot, form) {
  return {...snapshot,
    model: form.get("model"), participation_every: Number(form.get("participation_every")),
  };
}

function payloadSettings(snapshot, values) {
  return {revision: snapshot.revision, model: values.model, participation_every: values.participation_every,
    log_level: values.log_level, limits: values.limits, stickers: values.stickers};
}

function modelFields(snapshot) {
  return fields(labeled("DeepSeek 模型名称", input("model", snapshot.model, {required: true, maxLength: 100})),
    numberField("主动接话间隔（条）", "participation_every", snapshot.participation_every, 1, 1000));
}

async function persona(root) {
  const [snapshot, settings] = await Promise.all([request("/persona"), request("/settings")]);
  const item = snapshot.persona;
  const section = card(item.name + "的人设");
  section.append(row("人格配置版本", snapshot.revision.slice(0, 8)),
    node("p", "常用昵称 · " + item.aliases.join("、") + "。人设调整仅影响表达，账号权限和审核规则独立生效。"));
  const form = node("form");
  form.append(fields(labeled("机器人名称", input("name", item.name, {required: true, maxLength: 50})),
    labeled('昵称（每行一个）',area('aliases',item.aliases.join('\n'),1200)),
    numberField("回复字数上限（0 为不限）", "max_chars", item.max_chars, 0, 1000),
    numberField("温度", "temperature", item.temperature, 0, 2, "any")),
  labeled("完整人设提示词", area("system_prompt", item.system_prompt, 10000, true)),
  labeled("回复风格", area("style", item.style, 1000)), emotionInputs(snapshot.emotions, item.emotions));
  configActions(form, "保存人设", values => request("/persona", {method: "PUT", data: {
    revision: snapshot.revision, name: values.get("name"), system_prompt: values.get("system_prompt"),
    style: values.get("style"), max_chars: Number(values.get("max_chars")),
    temperature: Number(values.get("temperature")), emotions: values.getAll("emotions"),
    aliases:String(values.get('aliases')||'').split('\n').map(x=>x.trim()).filter(Boolean),
  }}), snapshot);
  section.append(form);
  const tuning = card("模型与接话节奏");
  const tuningForm = node("form");
  tuningForm.append(modelFields(settings));
  configActions(tuningForm, "保存模型与接话配置", values => request("/settings", {
    method: "PUT", data: payloadSettings(settings, basicSettingsData(settings, values)),
  }));
  tuning.append(tuningForm);
  root.append(section, tuning);
}

async function conversationMemory(root) {
  const {channels} = await request("/memory");
  const section = card("会话记忆");
  root.append(section);
  if (!channels.length) {
    section.append(empty("暂无会话。可在总览运行一次本地模拟。"));
    return;
  }
  const select = node("select");
  for (const [index, channel] of channels.entries()) {
    const option = node("option", (names[channel.platform] || channel.platform) + " · " + channel.channel_id);
    option.value = index;
    select.append(option);
  }
  const details = node("div");
  section.append(labeled("选择会话", select), details);
  let selectedRequest = 0;
  const load = async () => {
    const id = ++selectedRequest;
    const item = channels[Number(select.value)];
    const params = {platform: item.platform, kind: item.channel_kind, channel: item.channel_id};
    const {entries, summary} = await request("/memory?" + new URLSearchParams(params));
    if (id !== selectedRequest) return;
    details.replaceChildren(
      row("摘要状态", {pending: "有内容待摘要", ready: "已生成", empty: "暂无摘要"}[item.summary_status]),
      row("摘要更新时间", timeLabel(item.summary_at)), node("h3", "长期摘要"),
      summary ? node("p", summary, "prewrap") : empty("暂无长期摘要"), node("h3", "短期消息历史"),
    );
    for (const entry of entries) {
      details.append(row(entry.speaker + " · " + entry.name + " · " + timeLabel(entry.created_at), entry.text));
    }
    if (!entries.length) details.append(empty("暂无短期消息"));
    const clear = action("清理此会话记忆", "danger");
    clear.disabled = !entries.length && !summary;
    clear.onclick = () => {
      const confirmation = window.prompt("此操作将清理该会话的短期与长期记忆。请输入会话编号 " + item.channel_id + " 确认。");
      if (confirmation === null) return;
      perform(clear, () => request("/memory/clear", {method: "POST", data: {...params, confirmation}}), "已清理此会话记忆。");
    };
    details.append(clear);
  };
  select.onchange = () => load().catch(error => toast(error.message, true));
  await load();
}

function tokens(value) {
  return String(value).split(/[\n,，]/).map(item => item.trim()).filter(Boolean);
}

async function stickers(root) {
  const [catalog, settings] = await Promise.all([request("/stickers"), request("/settings")]);
  const controls = card("配图设置");
  const policy = node("form");
  policy.append(check("enabled", settings.stickers.enabled, "启用模拟配图"),
    fields(numberField("最小配图间隔（秒）", "min_interval", settings.stickers.min_interval, 0),
      numberField("同图防重复（秒）", "repeat_interval", settings.stickers.repeat_interval, 0)));
  configActions(policy, "保存配图设置", values => request("/settings", {method: "PUT", data: payloadSettings(settings, {
    ...settings, stickers: {enabled: values.has("enabled"),
      min_interval: Number(values.get("min_interval")), repeat_interval: Number(values.get("repeat_interval"))},
  })}));
  controls.append(policy);
  const upload = card("导入图片");
  const uploadForm = node("form");
  const file = node("input");
  Object.assign(file, {type: "file", accept: "image/png,image/jpeg,image/webp", required: true});
  uploadForm.append(labeled("选择静态图片", file), node("small", "支持 PNG、JPEG、WebP，最大 4 MB。"));
  const uploadButton = action("导入", "primary");
  uploadButton.type = "submit";
  uploadForm.append(uploadButton);
  uploadForm.onsubmit = event => {
    event.preventDefault();
    if (file.files[0]) perform(uploadButton, () => request("/stickers", {
      method: "POST", file: file.files[0],
    }), "已导入。填写标签后可启用。");
  };
  upload.append(uploadForm);
  const top = node("div", "", "split");
  top.append(controls, upload);
  root.append(top);
  const list = node("div", "", "sticker-list");
  root.append(list);
  if (!catalog.items.length) list.append(empty("暂无表情包。"));
  for (const item of catalog.items) {
    const section = card(item.name || "未命名表情");
    const layout = node("div", "", "sticker-layout");
    const picture = node("img", "", "sticker-preview");
    picture.alt = item.name || "表情预览";
    const form = node("form");
    form.append(labeled("名称", input("name", item.name, {maxLength: 60, required: true})),
      check("enabled", item.enabled, "启用这张图片"),
      emotionInputs(catalog.emotions, item.emotions),
      labeled("关键词（逗号分隔）", input("keywords", item.keywords.join("，"), {maxLength: 1250})),
      labeled("适用语境（逗号分隔）", input("contexts", item.contexts.join("，"), {maxLength: 1250})));
    configActions(form, "保存表情", values => request("/stickers/" + item.id, {method: "PUT", data: {
      name: values.get("name"), enabled: values.has("enabled"), emotions: values.getAll("emotions"),
      keywords: tokens(values.get("keywords")), contexts: tokens(values.get("contexts")),
    }}));
    layout.append(picture, form);
    section.append(layout);
    list.append(section);
    const previewPage = sequence;
    request("/stickers/" + item.id + "/image", {blob: true}).then(blob => {
      if (previewPage !== sequence) return;
      picture.src = URL.createObjectURL(blob);
      previews.push(picture.src);
    }).catch(() => { picture.alt = "图片暂不可用"; });
  }
}

async function messages(root) {
  setPage(root, "messages-template");
  const form = $("#message-filter", root);
  const result = $("#message-results", root);
  let filterSequence = 0;
  const filter = async () => {
    const serial = ++filterSequence;
    const parameters = new URLSearchParams();
    for (const [key, value] of new FormData(form)) {
      if (!value) continue;
      parameters.set(key, ["since", "until"].includes(key) ? new Date(value).getTime() / 1000 : value);
    }
    const {items} = await request("/messages?" + parameters);
    if (serial !== filterSequence) return;
    result.replaceChildren(node("h2", "消息记录"));
    if (!items.length) {
      result.append(empty("没有符合条件的消息"));
      return;
    }
    const wrap = node("div", "", "table-wrap");
    const table = node("table");
    const header = table.createTHead().insertRow();
    for (const text of ["时间", "平台和会话", "消息内容", "触发方式", "生成回复", "发送状态", "详情"]) {
      header.append(node("th", text));
    }
    const body = table.createTBody();
    for (const item of items) {
      const tr = body.insertRow();
      for (const value of [timeLabel(item.timestamp),
        (names[item.platform] || item.platform) + " · " + item.channel_id,
        item.sender + "：" + item.text, item.trigger || "—", item.reply || "—"]) {
        tr.append(node("td", value, "prewrap"));
      }
      const state = tr.insertCell();
      state.append(tag(stateNames[item.state] || item.state, item.state === "sent" ? "good" : ""));
      if (item.mode === "simulation") state.append(tag("simulation", "sim"));
      if (item.mode === "live") state.append(tag("live", "good"));
      if (item.mode === "unknown") state.append(tag("模式未记录"));
      tr.append(node("td", item.audit_skipped ? "审核跳过 · " + item.detail : item.detail || "—"));
    }
    wrap.append(table);
    result.append(wrap);
  };
  form.onsubmit = event => {
    event.preventDefault();
    filter().catch(error => toast(error.message, true));
  };
  await filter();
}

async function settings(root) {
  const snapshot = await request("/settings");
  const section = card("运行配置");
  const form = node("form");
  const level = node("select");
  level.name = "log_level";
  for (const [value, label] of Object.entries({DEBUG: "调试", INFO: "信息", WARNING: "警告", ERROR: "错误"})) {
    const option = node("option", label + " · " + value);
    Object.assign(option, {value, selected: value === snapshot.log_level});
    level.append(option);
  }
  form.append(modelFields(snapshot), fields(
    numberField("每会话每日发送上限", "per_channel_daily", snapshot.limits.per_channel_daily, 0),
    numberField("全局每日发送上限", "global_daily", snapshot.limits.global_daily, 0),
    numberField("最小发送间隔（秒）", "minimum_interval", snapshot.limits.minimum_interval, 0)),
  labeled("日志级别", level));
  configActions(form, "保存系统设置", values => request("/settings", {
    method: "PUT", data: payloadSettings(snapshot, {...basicSettingsData(snapshot, values), log_level: values.get("log_level"),
      limits: Object.fromEntries(Object.keys(snapshot.limits).map(key => [key, Number(values.get(key))]))}),
  }), snapshot);
  section.append(form);
  const other = card("平台与 DeepSeek 配置");
  const link = node("a", "前往平台管理 →", "tag sim");
  link.href = "#platforms";
  other.append(node("p", "平台开关与会话白名单在平台管理页编辑。DeepSeek 密钥由服务进程的环境变量管理。"),
    link, row("当前配置版本", snapshot.revision.slice(0, 8)));
  root.append(section, other);
  await replyDecisionControls(root);
}

async function replyDecisionControls(root) {
  const data=await request('/reply-decision');const cfg=data.config;
  const box=card('动态回复决策');const form=node('form');
  box.append(node('p','直接 @、引用和主人私聊不经过 Planner。节能模式只调用回复模型；均衡模式先做一次轻量决策。'),
    row('基础间隔',data.base_interval+' 条（沿用上方主动接话间隔）'),
    row('当前名称与昵称',[data.name,...data.aliases].join('、')),
    node('p','名称和昵称在 AI 人设页修改。基础间隔、人设变更沿用原设置生效方式，运行中的服务需重启；以下决策参数保存后用于新批次。'));
  const labels={mode:'回复模式',backoff:'启用动态退避',multiplier:'退避倍率',max_interval:'最大消息间隔',max_wait:'最大决策等待（秒）',
    conversation_ttl:'连续对话有效期（秒）',early_name:'提到名称或昵称时提前检查',early_continuation:'延续对话时提前检查',early_topic:'明显换题时提前检查',early_interest:'兴趣相关时提前检查',
    aggregate_seconds:'短消息聚合等待（秒，0 为不等待）',semantic:'启用语义兴趣与话题匹配',semantic_interval:'语义检查最小间隔（秒）',semantic_timeout:'语义请求超时（秒）',
    interest_threshold:'兴趣最低余弦相似度',topic_threshold:'话题变化余弦阈值',continuation_threshold:'延续话题最低余弦相似度',
    planner_same_model:'Planner 使用当前回复模型和凭据',planner_base_url:'独立 Planner API 地址',planner_model:'独立 Planner 模型',planner_key:'独立 Planner Key（留空保留）',
    planner_disable_thinking:'Planner 关闭思考模式',planner_timeout:'Planner 超时（秒）',planner_input_tokens:'Planner 输入预算（UTF-8 字节保守上界）',
    planner_output_tokens:'Planner 最大输出 Token',planner_messages:'Planner 最多候选消息数',planner_failure:'Planner 异常处理'};
  const controls={};
  for(const [key,label] of Object.entries(labels)) {
    let field;
    if(key==='mode'||key==='planner_failure')field=memoryPick(key==='mode'?{eco:'节能模式',balanced:'均衡模式'}:{eco:'降级节能模式',skip:'结束本轮接话'},cfg[key]);
    else {field=node('input');field.type=typeof cfg[key]==='boolean'?'checkbox':typeof cfg[key]==='number'?'number':key==='planner_key'?'password':'text';
      if(field.type==='checkbox')field.checked=cfg[key];else field.value=cfg[key];if(field.type==='number')field.step='any';field.autocomplete='off';}
    controls[key]=field;form.append(labeled(label,field));
  }
  const clear=node('input');clear.type='checkbox';form.append(row('独立 Planner 密钥',data.key_configured?'已配置':'未配置'),labeled('清除独立 Planner Key',clear));
  const save=node('button','保存决策设置','primary');save.type='submit';form.append(save);
  form.onsubmit=e=>{e.preventDefault();const config={...cfg};for(const [key,field] of Object.entries(controls))config[key]=field.type==='checkbox'?field.checked:field.type==='number'?Number(field.value):field.value;
    perform(save,()=>request('/reply-decision',{method:'PUT',data:{revision:data.revision,config,clear_key:clear.checked}}),'回复决策设置已保存');};
  box.append(form);root.append(box);
  const usage=card('决策与模型消耗');const range=node('form');const scope=memoryPick({'':'当前角色全部会话'},'');
  for(const parts of data.scopes){const option=node('option',parts.join(' / '));option.value=JSON.stringify(parts);scope.append(option);}
  const since=input('since','',{type:'datetime-local'}),until=input('until','',{type:'datetime-local'});const show=node('button','查看统计');show.type='submit';
  range.append(labeled('会话',scope),labeled('开始时间',since),labeled('结束时间',until),show);const output=node('div');usage.append(range,output);root.append(usage);
  async function load(){const params=new URLSearchParams({scope:scope.value,since:since.value?String(new Date(since.value).getTime()/1000):'0'});if(until.value)params.set('until',String(new Date(until.value).getTime()/1000));
    const report=await request('/reply-decision/stats?'+params);output.replaceChildren(node('p','优先记录 API usage；缺少 usage 时标记估算，失败且无 usage 时记为未知，不能当作零消耗。记忆后台任务单独列出。'));
    for(const [mode,value] of Object.entries(report.totals)){const item=card(mode==='eco'?'节能模式':'均衡模式');
      for(const [key,label] of Object.entries({tokens:'已记录 Token',calls:'模型请求次数',sent:'成功回复数',planner_average_tokens:'平均 Planner Token',silent_average_tokens:'平均沉默决策 Token',tokens_per_sent:'每条成功回复分摊 Token',estimated_requests:'估算请求数',unknown_requests:'用量未知请求数'}))item.append(row(label,value[key]===null?'暂无数据':String(Math.round(value[key]*100)/100)));
      output.append(item);}
    const stages={rules:'规则与结果',planner:'轻量 Planner',replyer:'回复生成',review:'回复审核',embedding:'语义检索',embedding_trigger:'兴趣匹配',memory_summary:'记忆摘要',memory_extract:'记忆提取',memory_evaluate:'记忆评价',memory_reflection:'记忆反思',memory_index:'记忆索引'};
    const events={received:'接收消息',rule_skipped:'规则跳过或批次合并',triggered:'动态触发',completed:'请求完成',reply:'决定回复',silent:'决定沉默',sent:'发送成功',error:'请求异常',error_or_skip:'异常或失效',planner_fallback:'Planner 降级',embedding_fallback:'语义降级'};
    for(const group of report.groups)output.append(row((stages[group.stage]||group.stage)+' · '+(events[group.event]||group.event)+' · '+group.decision_mode,
      group.records+' 次记录 / '+group.calls+' 次调用 / '+(group.prompt+group.completion)+' Token（输入 '+group.prompt+'，输出 '+group.completion+'）'));
    output.append(node('h3','按小时统计（浏览器本地时间）'));for(const hour of report.hourly)output.append(row(timeLabel(hour.hour)+' · '+hour.decision_mode,String(hour.tokens)+' Token'));
    output.append(node('h3','当前群聊退避状态'));for(const state of data.states)output.append(row(JSON.parse(state.scope).join(' / '),'连续沉默 '+state.silence+' 次，待处理 '+state.pending+' 条'));
  }
  range.onsubmit=e=>{e.preventDefault();perform(show,load,'统计已更新',false);};await load();
}

async function douyinAccount(root) {
  const data = await request("/douyin/account");
  const summary = card("账号连接");
  const state = {verified: "账号已核验", expired: "访问令牌已过期", disconnected: "尚未授权"};
  const identity = node("span", "", "actions");
  if (data.profile?.avatar) {
    const avatar = node("img", "", "douyin-avatar");
    avatar.src = data.profile.avatar;
    avatar.alt = "";
    avatar.referrerPolicy = "no-referrer";
    identity.append(avatar);
  }
  identity.append(node("span", data.profile?.nickname || "尚未连接账号"));
  summary.append(row("连接状态", tag(state[data.connection], data.connection === "verified" ? "good" : "")),
    row("账号", identity), row("运营模式", "草稿"),
    row("访问有效期", timeLabel(data.expires_at)), row("刷新有效期", timeLabel(data.refresh_expires_at)),
    row("最近核验", timeLabel(data.profile?.verified_at)));
  if (data.error) summary.append(row("最近错误", data.error));
  const actions = node("div", "", "actions");
  if (data.app_configured) {
    const authorize = action(data.profile ? "重新扫码授权" : "到抖音扫码授权", "primary");
    authorize.onclick = () => perform(authorize, async () => {
      const result = await request("/douyin/authorize", {method: "POST"});
      location.assign(result.authorization_url);
    }, "正在打开官方扫码页面…", false);
    actions.append(authorize);
  }
  if (data.can_refresh) {
    const refresh = action("刷新授权并核验账号");
    refresh.onclick = () => perform(refresh, () => request("/douyin/refresh", {method: "POST"}), "已刷新并核验账号。");
    actions.append(refresh);
  }
  if (data.profile) {
    const disconnect = action("断开本机连接");
    disconnect.onclick = () => {
      if (confirm("清除本机保存的授权？如需撤销应用授权，请到抖音 App 的授权管理操作。")) {
        perform(disconnect, () => request("/douyin/account", {method: "DELETE"}), "已清除本机授权凭据。");
      }
    };
    actions.append(disconnect);
  }
  summary.append(actions);
  const config = card("开放平台应用");
  const link = node("a", "打开抖音开放平台申请页", "tag sim");
  link.href = "https://open.douyin.com/platform/apply";
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  const eligibility = node("a", "查看网站应用的主体要求", "tag sim");
  eligibility.href = "https://developer.open-douyin.com/docs/resource/zh-CN/developer/join/issues";
  eligibility.target = "_blank";
  eligibility.rel = "noopener noreferrer";
  config.append(row("本机应用配置", data.app_configured ? "已配置" : "待配置"),
    row("开放平台审核", "请在官方控制台核对"),
    node("p", "网站应用需以企业、个体工商户等合规主体申请。个人开发者认证可用于小程序、小游戏和直播小玩法。"),
    block(link, " ", eligibility));
  if (!data.profile) {
    const form = node("form", "", "stack");
    const secret = input("client_secret", "", {type: "password", maxLength: 250, required: !data.app_configured});
    secret.autocomplete = "new-password";
    form.append(labeled("Client Key", input("client_key", "", {required: true, maxLength: 150})),
      labeled("Client Secret" + (data.app_configured ? "（留空沿用当前密钥）" : ""), secret),
      labeled("控制台登记的回调 URL", input("redirect_uri", data.redirect_uri, {type: "url", required: true, maxLength: 500})));
    const save = node("button", "保存本机配置", "primary");
    save.type = "submit";
    form.append(save);
    form.onsubmit = event => {
      event.preventDefault();
      const values = Object.fromEntries(new FormData(form));
      perform(save, () => request("/douyin/app", {method: "PUT", data: values}), "应用配置已保存。");
    };
    config.append(form);
  }
  const scopes = card("权限与安全");
  scopes.append(row("申请授权范围", data.requested_scopes.join("、")),
    row("账号实际授予", data.granted_scopes.join("、") || "暂无"),
    row("发布视频 · video.create", "待申请和联调"),
    row("普通作品评论 · item.comment", "待申请和联调"),
    row("普通账号私信", "暂不可用"), row("直播互动", "待评估"));
  root.append(summary, config, scopes);
  const outcome = new URLSearchParams(location.search).get("douyin");
  if (outcome) {
    toast(outcome === "ok" ? "抖音账号已核验。" : data.error || "授权未完成，请重新从面板发起。", outcome !== "ok");
    history.replaceState(null, "", "/#douyin-account");
  }
}

const draftFields = [
  ["title", "标题", 100], ["idea", "选题", 500], ["script", "脚本", 6000],
  ["description", "简介", 1000], ["topics", "话题（每行一个）", 400],
  ["pinned_comment", "置顶评论草稿", 600], ["plan", "发布计划", 1000],
];
const draftSources = {manual: "手动", simulation: "模拟", deepseek: "DeepSeek"};

function downloadDraft(draft) {
  const sections = draftFields.map(([key, label]) =>
    "## " + label + "\n\n" + (key === "topics" ? draft.content.topics.map(item => "#" + item).join(" ") : draft.content[key]));
  const file = new Blob(["> 本地草稿 · " + draftSources[draft.source] + "\n\n" + sections.join("\n\n")],
    {type: "text/markdown;charset=utf-8"});
  const href = URL.createObjectURL(file);
  const link = node("a");
  link.href = href;
  link.download = "大肥鱼-抖音草稿-" + draft.id + ".md";
  link.click();
  setTimeout(() => URL.revokeObjectURL(href), 1000);
}

async function douyinDrafts(root) {
  const data = await request("/douyin/drafts");
  const work = card("新选题");
  const brief = area("brief", "", 1000, true);
  brief.placeholder = "例如：大肥鱼用 30 秒介绍自己的创作日常";
  const generate = action("用 DeepSeek 生成并保存", "primary");
  generate.disabled = !data.deepseek_configured;
  const demo = action("保存模拟示例");
  const create = action("手动新建");
  const actions = node("div", "", "actions");
  actions.append(generate, demo, create);
  work.append(labeled("创作需求", brief), actions);
  if (!data.deepseek_configured) work.append(node("small", "V2 的 DeepSeek 密钥尚未配置，可先手动创建或查看模拟示例。"));
  generate.onclick = () => {
    if (brief.reportValidity()) perform(generate, () => request("/douyin/drafts/generate",
      {method: "POST", data: {brief: brief.value}}), "DeepSeek 草稿已保存。");
  };
  demo.onclick = () => perform(demo, () => request("/douyin/drafts/demo", {
    method: "POST", data: {brief: brief.value.trim() || "拍一支大肥鱼自我介绍短片"},
  }), "已保存模拟示例。");
  const editor = card("草稿编辑");
  editor.hidden = true;
  function edit(draft) {
    const form = node("form", "", "stack");
    form.append(labeled("创作需求", area("brief", draft?.brief ?? brief.value, 1000)));
    for (const [key, label, limit] of draftFields) {
      const value = draft?.content[key] ?? "";
      form.append(labeled(label, key === "title" ? input(key, value, {maxLength: limit, required: true}) :
        area(key, key === "topics" && value ? value.join("\n") : value, limit)));
    }
    const save = node("button", "保存草稿", "primary");
    save.type = "submit";
    form.append(save);
    editor.replaceChildren(node("h2", draft ? "编辑草稿" : "新建草稿"), form);
    editor.hidden = false;
    editor.scrollIntoView({block: "nearest"});
    form.elements.title.focus();
    form.onsubmit = event => {
      event.preventDefault();
      const values = Object.fromEntries(new FormData(form));
      const content = Object.fromEntries(draftFields.map(([key]) => [key, values[key]]));
      content.topics = values.topics.split(/\n/).map(value => value.trim().replace(/^#/, "")).filter(Boolean);
      perform(save, () => request("/douyin/drafts" + (draft ? "/" + draft.id : ""), {
        method: draft ? "PUT" : "POST",
        data: {brief: values.brief, content, ...(draft ? {version: draft.version} : {})},
      }), "草稿已保存。");
    };
  }
  create.onclick = () => edit();
  const library = card("草稿库");
  const list = node("div", "", "stack");
  const more = action("加载更早草稿");
  function append(page) {
    for (const draft of page.drafts) {
      const entry = block(node("h3", draft.content.title), row("来源", draftSources[draft.source]),
        row("最后编辑", timeLabel(draft.updated_at)));
      const buttons = node("div", "", "actions");
      const open = action("查看和编辑");
      open.onclick = () => edit(draft);
      const download = action("导出文案包");
      download.onclick = () => downloadDraft(draft);
      const remove = action("删除");
      remove.onclick = () => {
        if (confirm("删除这份本机草稿？")) perform(remove, () => request(
          "/douyin/drafts/" + draft.id + "?version=" + draft.version, {method: "DELETE"}), "草稿已删除。");
      };
      buttons.append(open, download, remove);
      entry.append(buttons);
      list.append(entry);
    }
    more.hidden = !page.next_before;
    more.onclick = async () => {
      more.disabled = true;
      try { append(await request("/douyin/drafts?before=" + page.next_before)); }
      catch (error) { toast(error.message, true); }
      finally { more.disabled = false; }
    };
  }
  if (!data.drafts.length) list.append(empty("暂无草稿"));
  append(data);
  library.append(list, more);
  const usage = card("抖音草稿模型用量");
  usage.append(row("DeepSeek 请求", String(data.usage.requests)), row("失败", String(data.usage.failures)),
    row("输入 Token", String(data.usage.prompt_tokens)), row("输出 Token", String(data.usage.completion_tokens)));
  root.append(work, editor, library, usage);
}

async function bilibili(root) {
  const data = await request('/bilibili');
  const status = card('浏览状态');
  status.append(row('账号凭据', data.credential_saved ? '本机已保存（有效性以实际读取为准）' : '未登录'),
    row('今日轮次', String(data.today_rounds) + '/' + data.config.rounds_per_day),
    row('模型预算', `${data.usage.requests} 次请求 / ${data.usage.tokens} 预留 Token / ${data.usage.visual} 次视觉`),
    row('运行状态', data.running ? '正在浏览' : data.worker_alive ? '后台每 30 秒检查' : '后台异常，请重启并检查'),
    row('视觉模型', data.vision_ready ? '已配置，实际调用时验证' : '未配置，文字浏览仍可使用'));
  const login = action('生成登录二维码');
  const check = action('我已扫码，检查登录');
  const once = action('检查并浏览一轮');
  const qr = node('div');
  login.onclick = () => perform(login, async () => {
    await request('/bilibili/login', {method:'POST'});
    const blob = await request('/bilibili/login/image', {blob:true});
    const url = URL.createObjectURL(blob); previews.push(url);
    const image = node('img'); image.src = url; image.alt = '请使用哔哩哔哩客户端扫码登录'; image.width = 220;
    qr.replaceChildren(image);
  }, '请用 B 站客户端扫码，然后点击检查登录。', false);
  check.onclick = () => perform(check, () => request('/bilibili/login/check', {method:'POST'}),
    r => ({connected:'登录成功',waiting:'等待扫码',confirming:'请在手机上确认',expired:'二维码已过期，请重新生成'})[r.state] || '登录暂不可用', false);
  once.onclick = () => perform(once, () => request('/bilibili/run', {method:'POST'}), '任务已受理，稍后点击刷新查看结果。');
  status.append(block(login, check, once), qr);
  root.append(status);
  const form = node('form', '', 'card');
  form.append(node('h2', '浏览设置'), node('p', '手动与后台共用时段和每日额度；失败、中断不退还额度。保存后 30 秒内生效。关闭浏览不打断已开始的一轮。'));
  const fields = {};
  const labels = {enabled:'开启自动浏览',rounds_per_day:'每日轮数',recommended:'每轮推荐（最多3）',searched:'每轮搜索（最多3）',
    visual:'每轮视觉（0或1）',max_duration:'最长视频（秒）',windows:'北京时间段（逗号分隔）',weekend_all_day:'周末全天',
    exploration:'探索方向',requests_per_day:'每日模型请求上限',tokens_per_day:'每日预留Token上限',
    visual_per_day:'每日视觉上限',share_preview:'生成分享预览（不发送 QQ）'};
  for (const [key, label] of Object.entries(labels)) {
    const input = node('input'); const value = data.config[key];
    input.type = typeof value === 'boolean' ? 'checkbox' : typeof value === 'number' ? 'number' : 'text';
    if (input.type === 'checkbox') input.checked = value;
    else input.value = Array.isArray(value) ? value.join(', ') : value;
    fields[key] = input; const wrap = node('label', label); wrap.append(input); form.append(wrap);
  }
  const save = node('button', '保存设置', 'primary'); save.type = 'submit'; form.append(save);
  form.onsubmit = e => { e.preventDefault(); const config = {...data.config};
    for (const [key,input] of Object.entries(fields)) config[key] = input.type === 'checkbox' ? input.checked :
      input.type === 'number' ? Number(input.value) : key === 'windows' ? input.value.split(',').map(x=>x.trim()).filter(Boolean) : input.value;
    perform(save, () => request('/bilibili/config', {method:'PUT', data:{revision:data.revision,config}}), '浏览设置已保存。');
  };
  root.append(form);
  const rounds = card('最近轮次与兴趣');
  rounds.append(node('p', data.interests.map(x=>`${x.topic} (${x.weight})`).join('、') || '尚无动态兴趣'));
  for (const round of data.rounds) rounds.append(node('p', `${timeLabel(round.at)} · ${round.state} · 阅读 ${(round.details.read || []).length} 个 · ${(round.details.errors || []).join(' / ')}`));
  root.append(rounds);
  const history = await request('/bilibili/history');
  const entries = card('真实阅读经历（最近50条）');
  for (const item of history.items) {
    const section = node('details'); section.append(node('summary', item.material.title + ' · ' + item.material.source));
    const link = node('a', '在 B 站打开'); link.href = item.material.url; link.target = '_blank'; link.rel = 'noopener noreferrer';
    section.append(link, node('p',item.material.coverage),node('p',item.material.text));
    if (item.assessment.summary) section.append(node('p','模型理解：'+item.assessment.summary));
    if (item.visual.description) section.append(node('p','抽样帧分析：'+item.visual.description));
    entries.append(section);
  }
  if (!history.items.length) entries.append(empty('尚无阅读经历'));
  root.append(entries);
  if (data.previews.length) { const preview = card('分享预览 · 未发送');
    for (const item of data.previews) preview.append(node('p',item.text)); root.append(preview); }
}

const pages = {overview, platforms, persona, bilibili,
  memory: typeof cognitionPage === "function" ? cognitionPage : conversationMemory, stickers, messages, settings,
  "douyin-account": douyinAccount, "douyin-drafts": douyinDrafts};

async function renderPage() {
  if (!signedIn) return;
  const serial = ++sequence;
  const requested = location.hash.slice(1);
  const route = Object.hasOwn(pages, requested) ? requested : "overview";
  for (const anchor of $("#nav").querySelectorAll("a")) {
    if (anchor.hash === "#" + route) anchor.setAttribute("aria-current", "page");
    else anchor.removeAttribute("aria-current");
  }
  $("#page-title").textContent = titles[route][0];
  $("#page-description").textContent = titles[route][1];
  const view = node("div", "", "stack");
  $("#page").replaceChildren(empty("正在加载…"));
  previews.forEach(URL.revokeObjectURL);
  previews = [];
  try {
    await pages[route](view);
    if (serial === sequence) $("#page").replaceChildren(view);
  } catch (error) {
    if (serial === sequence) $("#page").replaceChildren(empty(error.message));
  }
}

async function login() {
  await request("/status");
  signedIn = true;
  $("#login-error").textContent = "";
  $("#login-view").hidden = true;
  $("#workspace").hidden = false;
  await renderPage();
}

async function remember(token) {
  await request("/session", {method: "POST", token});
  sessionStorage.removeItem(sessionKey);
  await login();
}

$("#login-form").onsubmit = async event => {
  event.preventDefault();
  const control = $("#token");
  const token = control.value.trim();
  control.value = "";
  try {
    await remember(token);
  } catch (error) {
    showLogin();
    $("#login-error").textContent = error.message;
  }
};
$("#refresh").onclick = () => { $("#notice").hidden = true; renderPage(); };
$("#logout").onclick = async () => {
  try {
    await request("/session", {method: "DELETE"});
    showLogin();
  } catch (error) {
    if (signedIn) toast(error.message, true);
  }
};
window.addEventListener("hashchange", () => { $("#notice").hidden = true; renderPage(); });
const previousToken = sessionStorage.getItem(sessionKey);
(previousToken ? remember(previousToken) : login()).catch(error => {
  showLogin();
  if (previousToken || error.status !== 401) $("#login-error").textContent = error.message;
});
