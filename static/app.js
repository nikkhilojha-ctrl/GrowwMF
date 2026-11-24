const MAX_CHARS = 1000;

const state = {
  schemes: [],
  threads: [],
  activeThreadId: null,
  sending: false,
};

let openToken = 0;

const transcript = document.querySelector("#transcript");
const welcome = document.querySelector("#welcome");
const threadList = document.querySelector("#thread-list");
const growwLink = document.querySelector("#groww-link");
const question = document.querySelector("#question");
const composer = document.querySelector("#composer");
const sendButton = document.querySelector("#send");
const charCount = document.querySelector("#char-count");
const activeScheme = document.querySelector("#active-scheme");
const composerError = document.querySelector("#composer-error");
const newChat = document.querySelector("#new-chat");

newChat.addEventListener("click", () => {
  createThread().catch(showFailure);
});

composer.addEventListener("submit", (event) => {
  event.preventDefault();
  sendQuestion(question.value).catch(showFailure);
});

question.addEventListener("input", updateComposer);
question.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    composer.requestSubmit();
  }
});

for (const button of document.querySelectorAll(".example")) {
  button.addEventListener("click", () => {
    sendQuestion(button.dataset.question || "").catch(showFailure);
  });
}

updateComposer();
boot().catch(showFailure);

async function boot() {
  state.schemes = await request("/api/schemes");
  await refreshThreads();
  if (state.threads.length === 0) {
    await createThread();
    return;
  }
  await openThread(state.threads[0].thread_id);
}

async function createThread() {
  const created = await request("/api/threads", { method: "POST" });
  await refreshThreads();
  await openThread(created.thread_id);
  question.value = "";
  updateComposer();
  question.focus();
}

async function openThread(threadId) {
  const token = ++openToken;
  const detail = await request("/api/threads/" + threadId);
  if (token !== openToken) {
    return;
  }
  state.activeThreadId = detail.thread_id;
  renderThreadList();
  renderTranscript(detail.messages);
  renderScheme(detail.active_scheme_id);
}

async function refreshThreads() {
  state.threads = await request("/api/threads");
  renderThreadList();
}

async function sendQuestion(raw) {
  const text = raw.trim();
  if (!text || state.sending) {
    if (!text) {
      showError("Enter a question.");
    }
    return;
  }
  if (text.length > MAX_CHARS) {
    showError("Questions are limited to 1,000 characters.");
    return;
  }
  if (!state.activeThreadId) {
    await createThread();
  }
  const threadId = state.activeThreadId;
  state.sending = true;
  updateComposer();
  showError("");
  try {
    await request("/api/threads/" + threadId + "/messages", {
      method: "POST",
      body: JSON.stringify({ text }),
    });
    if (question.value.trim() === text) {
      question.value = "";
      updateComposer();
    }
    await refreshThreads();
    if (state.activeThreadId === threadId) {
      await openThread(threadId);
    }
  } finally {
    state.sending = false;
    updateComposer();
  }
}

function renderThreadList() {
  threadList.replaceChildren();
  for (const thread of state.threads) {
    const item = document.createElement("li");
    const button = document.createElement("button");
    button.type = "button";
    button.className = "thread";
    button.textContent = thread.title;
    if (thread.thread_id === state.activeThreadId) {
      button.classList.add("is-active");
      button.setAttribute("aria-current", "true");
    }
    button.addEventListener("click", () => {
      openThread(thread.thread_id).catch(showFailure);
    });
    item.append(button);
    threadList.append(item);
  }
}

function renderTranscript(messages) {
  transcript.querySelectorAll(".message").forEach((node) => node.remove());
  welcome.hidden = messages.length > 0;
  for (const message of messages) {
    transcript.append(renderMessage(message));
  }
  transcript.scrollTop = transcript.scrollHeight;
}

function renderMessage(message) {
  const article = document.createElement("article");
  article.className = "message";
  article.dataset.role = message.role;
  if (message.kind) {
    article.dataset.kind = message.kind;
  }
  const text = document.createElement("p");
  text.className = "message-text";
  text.textContent = message.text;
  article.append(text);
  if (message.role === "assistant" && message.citation && message.citation.url) {
    const source = document.createElement("p");
    source.className = "message-source";
    const link = document.createElement("a");
    link.className = "citation";
    link.href = message.citation.url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.textContent = message.citation.title || message.citation.url;
    source.append(link);
    article.append(source);
  }
  if (message.role === "assistant" && message.footer) {
    const footer = document.createElement("p");
    footer.className = "message-footer";
    footer.textContent = message.footer;
    article.append(footer);
  }
  return article;
}

function renderScheme(schemeId) {
  const scheme = state.schemes.find((item) => item.scheme_id === schemeId);
  if (!scheme) {
    activeScheme.hidden = true;
    activeScheme.textContent = "";
    growwLink.hidden = true;
    growwLink.removeAttribute("href");
    return;
  }
  activeScheme.hidden = false;
  activeScheme.textContent = "Active scheme: " + scheme.name;
  growwLink.href = scheme.groww_url;
  growwLink.hidden = false;
}

function updateComposer() {
  const length = question.value.length;
  charCount.textContent = length.toLocaleString("en-US") + " / 1,000";
  sendButton.disabled = state.sending || question.value.trim().length === 0;
  newChat.disabled = state.sending;
  for (const button of document.querySelectorAll(".example")) {
    button.disabled = state.sending;
  }
}

function showError(message) {
  composerError.textContent = message;
}

function showFailure(error) {
  state.sending = false;
  updateComposer();
  showError(error && error.message ? error.message : "The assistant could not be reached.");
}

async function request(path, options = {}) {
  const headers = { Accept: "application/json" };
  if (options.body) {
    headers["Content-Type"] = "application/json";
  }
  const response = await fetch(path, {
    credentials: "same-origin",
    ...options,
    headers,
  });
  if (!response.ok) {
    let detail = "The assistant could not complete that request.";
    try {
      const payload = await response.json();
      if (typeof payload.detail === "string") {
        detail = payload.detail;
      }
    } catch (error) {
      /* The status text above is enough when the body is not JSON. */
    }
    throw new Error(detail);
  }
  return response.json();
}
