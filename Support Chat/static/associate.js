let associate = null;
let tickets = [];
let currentTicket = null;
let queueSSE = null;
let msgSSE = null;
let lastMsgId = 0;


// ------------------------------------------------------------
// Utility
// ------------------------------------------------------------

async function apiFetch(url, options = {}) {
  const response = await fetch(url, {
    ...options,
    cache: "no-store"
  });

  if (!response.ok) {
    let message = `HTTP ${response.status}`;

    try {
      const data = await response.json();
      if (data.error) {
        message += `: ${data.error}`;
      }
    } catch (_) {
      // Ignore JSON parsing error
    }

    throw new Error(message);
  }

  return response;
}


// ------------------------------------------------------------
// Associates
// ------------------------------------------------------------

async function pickAssociate() {
  const response = await apiFetch("/api/associate/me");
  const data = await response.json();
  associate = data.associate;
}

async function loadAssociateKnowledgeBase() {
  const response = await apiFetch("/api/associate/articles");
  const data = await response.json();
  const list = document.getElementById("assoc-kb-list");

  if (!list) {
    return;
  }

  list.replaceChildren();

  if (!data.articles || data.articles.length === 0) {
    const empty = document.createElement("div");
    empty.textContent = "No knowledge-base articles yet.";
    list.appendChild(empty);
    return;
  }

  for (const article of data.articles) {
    const item = document.createElement("article");
    item.className = "assoc-kb-article";

    const title = document.createElement("h4");
    title.textContent = article.title || "Untitled article";

    const body = document.createElement("p");
    body.textContent = article.body || "";

    const tags = document.createElement("small");
    tags.textContent = article.tags || "";

    item.append(title, body, tags);
    list.appendChild(item);
  }
}

async function loadAssociateTicket(tid) {
  try {
    return await apiFetch(`/api/associate/tickets/${tid}/messages`);
  } catch (error) {
    if (!error.message.includes("HTTP 404")) {
      throw error;
    }
    return await apiFetch(`/api/tickets/${tid}/messages`);
  }
}


// ------------------------------------------------------------
// Queue
// ------------------------------------------------------------

function renderQueue() {
  const q = document.getElementById("assoc-queue");

  q.innerHTML = "";

  const heading = document.createElement("h3");
  heading.textContent = "Escalation Queue";
  q.appendChild(heading);

  if (!tickets || tickets.length === 0) {
    const empty = document.createElement("div");

    empty.className = "queue-empty";
    empty.textContent = "No escalations waiting.";

    q.appendChild(empty);
    return;
  }

  for (const t of tickets) {
    const div = document.createElement("div");

    div.className = "queue-ticket";

    if (Number(t.id) === Number(currentTicket)) {
      div.classList.add("active");
    }

    const summary = document.createElement("div");
    summary.className = "queue-ticket-summary";
    summary.textContent = t.summary || `Ticket #${t.id}`;

    const status = document.createElement("div");
    status.className = "queue-ticket-status";

    status.textContent =
      t.assigned_to
        ? `Assigned: ${t.assigned_to}`
        : "Waiting for associate";

    div.appendChild(summary);
    div.appendChild(status);

    div.onclick = () => openTicket(t.id);

    q.appendChild(div);
  }
}


// ------------------------------------------------------------
// Open / claim ticket
// ------------------------------------------------------------

async function openTicket(tid) {
  try {
    console.log("Opening ticket:", tid);

    currentTicket = Number(tid);
    lastMsgId = 0;

    renderQueue();
    renderWorkspace();

    // Stop previous message stream.
    if (msgSSE) {
      msgSSE.close();
      msgSSE = null;
    }

    // Clear transcript.
    document.getElementById("assoc-transcript").innerHTML = "";

    // --------------------------------------------------------
    // First load existing messages
    // --------------------------------------------------------

    const response = await loadAssociateTicket(currentTicket);

    const data = await response.json();

    console.log("Ticket data:", data);

    if (data.messages) {
      for (const msg of data.messages) {
        addTranscript(msg);

        if (msg.id != null) {
          lastMsgId = Math.max(
            lastMsgId,
            Number(msg.id)
          );
        }
      }
    }

    // --------------------------------------------------------
    // Claim ticket
    // --------------------------------------------------------

    console.log(
      "Claiming ticket:",
      currentTicket,
      "as associate:",
      associate
    );

    const claimResponse = await apiFetch(
      `/api/associate/tickets/${currentTicket}/claim`,
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json"
        },
        body: JSON.stringify({})
      }
    );

    const claimResult = await claimResponse.json();

    console.log("Claim result:", claimResult);

    // --------------------------------------------------------
    // Add associate joined message if backend doesn't do it
    // --------------------------------------------------------

    // Backend already creates the welcome message when claiming,
    // so we don't add another one here.

    // --------------------------------------------------------
    // Start real-time conversation stream
    // --------------------------------------------------------

    startMessageStream();

  } catch (error) {
    console.error("Unable to open ticket:", error);

    if (error.message.includes("HTTP 401")) {
      window.location.href = "/associate/login";
      return;
    }

    alert(
      `Unable to open ticket #${tid}.\n\n${error.message}`
    );

    currentTicket = null;
    renderWorkspace();
  }
}


// ------------------------------------------------------------
// Message SSE
// ------------------------------------------------------------

function startMessageStream() {
  if (!currentTicket) {
    console.error(
      "Cannot start message stream: no ticket selected"
    );
    return;
  }

  if (msgSSE) {
    msgSSE.close();
  }

  const after = Number.isFinite(Number(lastMsgId))
    ? Number(lastMsgId)
    : 0;

  console.log(
    `Starting message SSE for ticket ${currentTicket}, after=${after}`
  );

  msgSSE = new EventSource(
    `/api/tickets/${currentTicket}/stream?after=${after}`
  );

  msgSSE.addEventListener("message", event => {
    try {
      const msg = JSON.parse(event.data);

      console.log("Incoming message:", msg);

      // Prevent duplicate messages.
      if (
        msg.id != null &&
        Number(msg.id) <= Number(lastMsgId)
      ) {
        return;
      }

      addTranscript(msg);

      if (msg.id != null) {
        lastMsgId = Math.max(
          Number(lastMsgId),
          Number(msg.id)
        );
      }

    } catch (error) {
      console.error(
        "Failed to process incoming message:",
        error
      );
    }
  });

  msgSSE.addEventListener("status", event => {
    try {
      const data = JSON.parse(event.data);

      console.log(
        "Ticket status:",
        data.status
      );

      if (data.status === "resolved") {
        setWorkspaceStatus("Resolved");
      } else if (data.status === "associate_active") {
        setWorkspaceStatus("Active");
      } else if (data.status === "escalated") {
        setWorkspaceStatus("Waiting");
      }

    } catch (error) {
      console.error(
        "Failed to process status:",
        error
      );
    }
  });

  msgSSE.addEventListener("done", () => {
    console.log("Ticket resolved.");

    setWorkspaceStatus("Resolved");

    if (msgSSE) {
      msgSSE.close();
      msgSSE = null;
    }
  });

  msgSSE.onerror = error => {
    console.error(
      "Message SSE error:",
      error
    );
  };
}


// ------------------------------------------------------------
// Transcript
// ------------------------------------------------------------

function addTranscript(msg) {
  const transcript =
    document.getElementById("assoc-transcript");

  if (!transcript) {
    return;
  }

  const div = document.createElement("div");

  const sender =
    msg.sender || "bot";

  div.className =
    "chat-bubble " +
    (
      sender === "user"
        ? "user"
        : sender === "associate"
          ? "associate"
          : "bot"
    );

  let name = "Assistant";

  if (sender === "user") {
    name = msg.author_name || "User";
  } else if (sender === "associate") {
    name = msg.author_name || associate?.name || "Associate";
  }

  const author = document.createElement("span");
  author.className = "bubble-author";
  author.textContent = name;

  const body = document.createElement("span");
  body.className = "bubble-body";
  body.textContent = msg.body || "";

  div.appendChild(author);
  div.appendChild(body);

  transcript.appendChild(div);

  div.scrollIntoView({
    behavior: "smooth",
    block: "end"
  });
}


// ------------------------------------------------------------
// Workspace
// ------------------------------------------------------------

function renderWorkspace() {
  const workspace =
    document.getElementById("assoc-workspace");

  if (!workspace) {
    return;
  }

  workspace.style.display =
    currentTicket ? "block" : "none";

  if (!currentTicket) {
    document.getElementById(
      "assoc-transcript"
    ).innerHTML = "";
  }
}


function setWorkspaceStatus(status) {
  const workspace =
    document.getElementById("assoc-workspace");

  if (!workspace) {
    return;
  }

  let statusElement =
    document.getElementById("assoc-ticket-status");

  if (!statusElement) {
    statusElement =
      document.createElement("div");

    statusElement.id =
      "assoc-ticket-status";

    statusElement.className =
      "assoc-ticket-status";

    workspace.prepend(statusElement);
  }

  statusElement.textContent =
    `Ticket #${currentTicket}: ${status}`;
}


// ------------------------------------------------------------
// Associate queue SSE
// ------------------------------------------------------------

function startQueueStream() {
  if (queueSSE) {
    queueSSE.close();
  }

  console.log(
    "Starting associate queue SSE..."
  );

  queueSSE = new EventSource(
    "/api/associate/tickets/stream"
  );

  queueSSE.addEventListener(
    "tickets",
    event => {
      try {
        tickets = JSON.parse(event.data);

        console.log(
          "Escalation queue:",
          tickets
        );

        renderQueue();

      } catch (error) {
        console.error(
          "Failed to parse queue:",
          error
        );
      }
    }
  );

  queueSSE.addEventListener(
    "ping",
    () => {
      console.log("Associate queue heartbeat");
    }
  );

  queueSSE.onerror = error => {
    console.error(
      "Associate queue SSE error:",
      error
    );
  };
}


// ------------------------------------------------------------
// Send associate reply
// ------------------------------------------------------------

async function sendReply(text) {
  if (!currentTicket) {
    return;
  }

  if (!associate) {
    alert("No associate is selected.");
    return;
  }

  try {
    await apiFetch(
      `/api/tickets/${currentTicket}/messages`,
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json"
        },
        body: JSON.stringify({
          sender: "associate",
          body: text,
          author_name: associate.name
        })
      }
    );

    console.log(
      "Associate message sent."
    );

  } catch (error) {
    console.error(
      "Failed to send associate message:",
      error
    );

    alert(
      `Unable to send message.\n\n${error.message}`
    );
  }
}


// ------------------------------------------------------------
// Resolve ticket
// ------------------------------------------------------------

async function resolveTicket({
  title,
  steps,
  tags,
  publish
}) {
  if (!currentTicket) {
    return;
  }

  try {
    const response = await apiFetch(
      `/api/associate/tickets/${currentTicket}/resolve`,
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json"
        },
        body: JSON.stringify({
          title,
          steps,
          tags,
          publish
        })
      }
    );

    const result = await response.json();

    console.log(
      "Resolve result:",
      result
    );

    alert("Ticket resolved!");

    if (msgSSE) {
      msgSSE.close();
      msgSSE = null;
    }

    currentTicket = null;
    lastMsgId = 0;

    renderWorkspace();

    // Refresh queue immediately.
    try {
      const queueResponse =
        await apiFetch(
          "/api/associate/tickets"
        );

      tickets =
        await queueResponse.json();

      renderQueue();

    } catch (error) {
      console.error(
        "Failed to refresh queue:",
        error
      );
    }

  } catch (error) {
    console.error(
      "Failed to resolve ticket:",
      error
    );

    alert(
      `Unable to resolve ticket.\n\n${error.message}`
    );
  }
}


// ------------------------------------------------------------
// Page initialization
// ------------------------------------------------------------

window.addEventListener("DOMContentLoaded", async function() {
  const logout = document.querySelector('.logout-link');

  try {

    // ------------------------------------------
    // Authenticate / select associate
    // ------------------------------------------

    await pickAssociate();
    await loadAssociateKnowledgeBase();

    console.log(
      "Associate:",
      associate
    );

    // ------------------------------------------
    // Load queue immediately
    // ------------------------------------------

    try {
      const response =
        await apiFetch(
          "/api/associate/tickets"
        );

      tickets =
        await response.json();

      console.log(
        "Initial escalations:",
        tickets
      );

      renderQueue();

    } catch (error) {
      console.error(
        "Unable to load escalation queue:",
        error
      );

      alert(
        `Unable to load escalation queue.\n\n${error.message}`
      );
    }

    // ------------------------------------------
    // Start live queue updates
    // ------------------------------------------

    startQueueStream();

    // ------------------------------------------
    // Reply form
    // ------------------------------------------

    document.getElementById(
      "assoc-reply-form"
    ).onsubmit = async function(e) {

      e.preventDefault();

      const input =
        document.getElementById(
          "assoc-reply-input"
        );

      const text =
        input.value.trim();

      if (!text) {
        return;
      }

      await sendReply(text);

      input.value = "";
      input.focus();
    };

    // ------------------------------------------
    // Resolve form
    // ------------------------------------------

    document.getElementById(
      "assoc-resolve-form"
    ).onsubmit = async function(e) {

      e.preventDefault();

      if (!currentTicket) {
        alert(
          "Please select a ticket first."
        );
        return;
      }

      const title =
        document.getElementById(
          "resolve-title"
        ).value.trim();

      const steps =
        document.getElementById(
          "resolve-steps"
        ).value.trim();

      const tags =
        document.getElementById(
          "resolve-tags"
        ).value.trim();

      const publish =
        document.getElementById(
          "resolve-publish"
        ).checked;

      await resolveTicket({
        title,
        steps,
        tags,
        publish
      });
    };

    document.getElementById(
      "assoc-close-ticket"
    ).onclick = async function() {
      if (!currentTicket) {
        alert("Please select a ticket first.");
        return;
      }

      await resolveTicket({
        title: "",
        steps: "",
        tags: "",
        publish: false
      });
    };

  } catch (error) {

    console.error(
      "Associate console initialization failed:",
      error
    );

    if (error.message.includes("HTTP 401")) {
      window.location.href = "/associate/login";
      return;
    }

    alert(
      `Associate console failed to initialize.\n\n${error.message}`
    );
  }
});