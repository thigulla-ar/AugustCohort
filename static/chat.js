let user = null, ticketId = null, phase = "describe", sse = null, lastMsgId = 0;
// async function getUser() {
//   let r = await fetch("/api/me");
//   user = await r.json();
//   if (!user.name || user.name === "Guest") {
//     let name = prompt("What's your name?");
//     if (name) await fetch("/api/me", {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({name})});
//     r = await fetch("/api/me");
//     user = await r.json();
//   }
// }
async function getUser() {
  try {
    console.log("Calling /api/me...");

    let r = await fetch("/api/me", {
      method: "GET",
      cache: "no-store"
    });

    console.log("/api/me:", r.status, r.statusText);

    if (!r.ok) {
      throw new Error(`/api/me returned ${r.status}`);
    }

    user = await r.json();

    console.log("User:", user);

    if (!user.name || user.name === "Guest") {
      const name = prompt("What's your name?");

      if (name && name.trim()) {
        const saveResponse = await fetch("/api/me", {
          method: "POST",
          headers: {
            "Content-Type": "application/json"
          },
          body: JSON.stringify({
            name: name.trim()
          })
        });

        if (!saveResponse.ok) {
          throw new Error(`Failed to save user: ${saveResponse.status}`);
        }

        r = await fetch("/api/me", {
          cache: "no-store"
        });

        if (!r.ok) {
          throw new Error(`Failed to refresh user: ${r.status}`);
        }

        user = await r.json();
      }
    }
  } catch (err) {
    console.error("getUser() failed:", err);

    user = {
      id: null,
      name: "Guest"
    };
  }
}

function showTyping(show) {
  document.getElementById("typing-indicator").style.display = show ? "" : "none";
}
window.fetch = ((orig) => function(...args) {
  if (args[0].startsWith("/api/tickets")) showTyping(true);
  return orig(...args).finally(() => showTyping(false));
})(window.fetch);

function addMsg(msg) {
  const emptyState = document.getElementById("chat-empty-state");
  if (emptyState) emptyState.remove();
  if (msg.id != null && Number(msg.id) <= Number(lastMsgId)) return;
  let div = document.createElement("div");
  div.className = "chat-bubble " + (msg.sender === "user" ? "user" : msg.sender === "associate" ? "associate" : "bot");
  let name = msg.sender === "user" ? user.name : msg.sender === "associate" ? msg.author_name : "Assistant";
  let author = document.createElement("span");
  author.className = "bubble-author";
  author.textContent = name || "User";
  let body = document.createElement("span");
  body.className = "bubble-body";
  body.textContent = msg.body || "";
  div.append(author, body);
  document.getElementById("chat-messages").appendChild(div);
  if (msg.id != null) {
    lastMsgId = Math.max(Number(lastMsgId), Number(msg.id));
}
  div.scrollIntoView({behavior:"smooth",block:"end"});
}
function feedbackButtons(articleId) {
  let div = document.createElement("div");
  div.className = "feedback-buttons";
  div.innerHTML = `<button id="fb-yes">Yes</button><button id="fb-no">No</button>`;
  div.querySelector("#fb-yes").onclick = async () => {
    await fetch(`/api/tickets/${ticketId}/feedback`, {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({helped:true,article_id:articleId})});
    phase = "resolved";
    addMsg({sender:"bot",body:"Glad I could help! Your ticket is resolved.",author_name:"Assistant"});
  };
  div.querySelector("#fb-no").onclick = async () => {
    await fetch(`/api/tickets/${ticketId}/feedback`, {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({helped:false,article_id:articleId})});
    phase = "escalated";
    addMsg({sender:"bot",body:"Connecting you to a support associate...",author_name:"Assistant"});
  };
  document.getElementById("chat-messages").appendChild(div);
}
async function startSSE() {
  if (sse) {
    sse.close();
  }

  if (!ticketId) {
    console.error("Cannot start SSE: ticketId is missing");
    return;
  }

  if (!Number.isFinite(Number(lastMsgId))) {
    lastMsgId = 0;
  }

  sse = new EventSource(
    `/api/tickets/${ticketId}/stream?after=${Number(lastMsgId)}`
  );

  sse.onmessage = e => {};

  sse.addEventListener("message", e => {
    try {
      let msg = JSON.parse(e.data);
      addMsg(msg);
    } catch (err) {
      console.error("Invalid SSE message:", e.data, err);
    }
  });

  sse.addEventListener("status", e => {
    let s = JSON.parse(e.data).status;

    phase =
      s === "resolved"
        ? "resolved"
        : s === "escalated"
          ? "escalated"
          : phase;
  });

  sse.addEventListener("done", e => {
    phase = "resolved";

    addMsg({
      sender: "bot",
      body: "Ticket resolved. Thank you!",
      author_name: "Assistant"
    });

    sse.close();
  });

  sse.onerror = err => {
    console.error("SSE connection error:", err);
  };
}
window.addEventListener("DOMContentLoaded", async function() {
  await getUser();
  let url = new URL(window.location);
  ticketId = url.searchParams.get("ticket");
  if (ticketId) {
    let r = await fetch(`/api/tickets/${ticketId}/messages`);
    let {ticket, messages} = await r.json();
    for (let m of messages) addMsg(m);
    phase = ticket.status === "resolved" ? "resolved" : ticket.status === "escalated" ? "escalated" : "describe";
    lastMsgId = messages.length ? messages[messages.length-1].id : 0;
    startSSE();
  }
  document.getElementById("chat-form").onsubmit = async function(e) {
    e.preventDefault();
    let input = document.getElementById("chat-input");
    let text = input.value.trim();
    if (!text) return;
    input.value = "";
    if (!ticketId) {
      addMsg({sender:"user",body:text,author_name:user.name});
      let r = await fetch("/api/tickets", {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({summary:text})});
      let resp = await r.json();
      ticketId = resp.ticket_id;
      phase = resp.phase;
      lastMsgId = Number(resp.initial_message_id || 0);
      if (resp.auto_answer) feedbackButtons(resp.article_id);
      else if (resp.article_ids) {
        for (let aid of resp.article_ids) {
          let a = await (await fetch(`/api/articles/${aid}`)).json();
          let div = document.createElement("div");
          div.className = "article-suggestion";
          let title = document.createElement("b");
          title.textContent = a.title || "Article";
          let button = document.createElement("button");
          button.textContent = "This helped";
          div.append(title, document.createTextNode(" "), button);
          button.onclick = async () => {
            await fetch(`/api/tickets/${ticketId}/feedback`, {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({helped:true,article_id:aid})});
            phase = "resolved";
            addMsg({sender:"bot",body:"Glad I could help! Your ticket is resolved.",author_name:"Assistant"});
          };
          document.getElementById("chat-messages").appendChild(div);
        }
        feedbackButtons(resp.article_ids[0]);
      }
      startSSE();
    } else {
      await fetch(`/api/tickets/${ticketId}/messages`, {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({sender:"user",body:text})});
    }
  };
});