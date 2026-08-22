# Support Chat Application

Support Chat is a customer-support web application with automated knowledge-base
answers, confidence-based escalation, human-associate handoff, ticket history,
resolution publishing, feedback, and operational metrics.

The automated answer path uses LangGraph for orchestration, LangChain BM25 for local
lexical retrieval, Pinecone for semantic vector retrieval, and OpenAI for embeddings
and grounded response generation. If semantic retrieval is unavailable, the system
continues with BM25 instead of disabling the complete knowledge base.

## Features

- Customer web chat and persistent ticket history
- Greeting and explicit-human-request detection
- LangGraph retrieval, answer, and escalation workflow
- Hybrid BM25 and Pinecone semantic retrieval
- OpenAI grounded answer generation with article citations
- BM25 fallback during OpenAI or Pinecone failures
- Confidence-based routing to support associates
- Specialty-based assignment and basic load balancing
- Associate login, queue, claim, reply, and resolution workflow
- Optional publication of resolutions into the knowledge base
- Article feedback and support metrics
- Server-sent updates for customer and associate interfaces

## Architecture

```text
Customer browser                     Associate browser
       |                                    |
       +------------- HTTP/SSE -------------+
                            |
                         app.py
                    request routing/session
                       /             \
                    db.py            nlp.py
                      |                 |
              support_chat.db      LangGraph
                                        |
                          +-------------+-------------+
                          |                           |
                    BM25 retrieval              Pinecone search
                    SQLite articles          OpenAI embeddings
                          \                           /
                           +---- grounded answer ----+
                                      |
                              answer or escalation
```

### Main files

| File or directory | Responsibility |
|---|---|
| `app.py` | HTTP server, routes, sessions, chat workflow, handoff and API responses |
| `db.py` | SQLite schema, ingestion, queries, tickets, messages and metrics |
| `nlp.py` | LangGraph workflow, chunking, BM25, Pinecone and answer generation |
| `eval.py` | Retrieval and escalation evaluation cases |
| `data/` | Maintained JSON knowledge-base documents |
| `static/chat.js` | Customer chat behavior, requests and message rendering |
| `static/associate.js` | Associate workspace behavior |
| `templates/` | Customer, associate, article, ticket and metrics pages |
| `support_chat.db` | Runtime SQLite database |
| `SUPPORT_CHAT_CONTROL_FLOW.md` | Detailed 16-step request and RAG control flow |

## Requirements

- Python 3.11 or later
- An OpenAI API key with API billing/quota
- A Pinecone API key
- Network access to OpenAI and Pinecone

Python dependencies are listed in `requirements.txt`.

## Installation

From PowerShell in the project directory:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
py -3 -m pip install -r requirements.txt
```

If PowerShell blocks virtual-environment activation, either adjust the user execution
policy or run the environment's Python executable directly.

## Configuration

The application reads configuration from environment variables. `.env.example` is a
template only; the application does not automatically load `.env` files.

Set the required variables in the same PowerShell session used to start the server:

```powershell
$env:OPENAI_API_KEY = "your-openai-api-key"
$env:PINECONE_API_KEY = "your-pinecone-api-key"
```

Optional settings:

| Variable | Default | Description |
|---|---|---|
| `OPENAI_MODEL` | `gpt-4o-mini` | Grounded answer-generation model |
| `OPENAI_EMBEDDING_MODEL` | `text-embedding-3-small` | Embedding model |
| `PINECONE_INDEX` | `support-chat` | Pinecone index name |
| `PINECONE_CLOUD` | `aws` | Serverless index cloud |
| `PINECONE_REGION` | `us-east-1` | Serverless index region |
| `RAG_TOP_K` | `4` | Final number of article results |
| `RAG_LOW_CONFIDENCE` | `0.25` | Minimum semantic evidence used for generation routing |
| `RAG_HIGH_CONFIDENCE` | `0.50` | Direct-answer fallback threshold |
| `ASSOCIATE_PASSWORD` | unset | Initial/default associate password configuration |
| `ASSOCIATE_RESET_CODE` | unset | Associate credential-reset code |

Never commit real credentials, add them to browser JavaScript, or expose provider
errors containing sensitive information to customers.

## Running the application

```powershell
py app.py
```

Open the customer interface at:

```text
http://127.0.0.1:5000/
```

Related pages:

| Page | URL |
|---|---|
| Customer chat | `/` |
| Associate workspace | `/associate` |
| Associate login | `/associate/login` |
| Knowledge base | `/articles` |
| Customer tickets | `/my-tickets` |
| Operational metrics | `/metrics` |

Stop the server with `Ctrl+C`.

## Knowledge-base corpus

The maintained source files are:

| File | Content type |
|---|---|
| `data/faqs.json` | Customer-facing frequently asked questions |
| `data/process_questions.json` | Internal support processes |
| `data/tickets.json` | Reusable historical-ticket solutions |
| `data/manuals.json` | Associate manuals and escalation procedures |

Each record contains:

```json
{
  "id": "faq-001",
  "title": "How do I reset my password?",
  "content": "Article content...",
  "tags": ["password", "login", "reset"],
  "source": "faq"
}
```

At application startup, `db.ensure_schema()` reads these files and upserts records
into the SQLite `articles` table by `document_id`. SQLite is the runtime retrieval
source. JSON is the maintained source for file-based articles. Published associate
resolutions are stored directly in SQLite.

## RAG workflow

The compiled `rag_graph` in `nlp.py` contains three nodes:

```text
START -> retrieve -> generate -> END
                  \-> escalate -> END
```

### Chunking

- Splitter: `RecursiveCharacterTextSplitter`
- Chunk size: 512 characters
- Chunk overlap: 64 characters
- Input: article title and body
- Metadata: article ID, title, tags, source and full body

The current articles are shorter than 512 characters, so each normally remains a
single retrieval unit.

### BM25 retrieval

`_build_bm25()` loads articles from SQLite, creates LangChain documents, chunks them,
and builds an in-memory `BM25Retriever`. The retriever cache is invalidated when the
article signature changes.

Lexical confidence measures meaningful query-token overlap. A small source-type tie
breaker prefers a general FAQ over a process, manual, or historical ticket only when
the documents have otherwise equivalent lexical evidence.

### Semantic retrieval

`_build_vector_store()` uses:

- Embedding model: `text-embedding-3-small`
- Dimensions: 256
- Pinecone metric: cosine similarity
- Namespace: content-addressed from the current corpus signature

If the configured Pinecone index does not exist, the application creates it. If an
existing index has a dimension other than 256, initialization fails with an explicit
dimension-mismatch error.

Changing the corpus creates a new active namespace. This avoids mixing stale chunks
into current retrieval, although old namespaces require a separate retention/cleanup
policy in long-running production environments.

### Hybrid scoring and fallback

The current scoring uses a 40% lexical component and a 60% semantic component, plus a
small source-type tie breaker for equivalent lexical results.

If OpenAI embeddings or Pinecone fails:

1. The semantic exception is recorded in the RAG trace.
2. Retrieval mode becomes `bm25_fallback`.
3. A strong BM25 match may still answer directly.
4. Unsupported or weakly matched questions are escalated.

This prevents an external-provider outage from disabling local knowledge retrieval.

### Answer generation

Retrieved sources are supplied to `ChatOpenAI` with instructions to answer only from
the provided support context and cite article IDs. If generation fails but retrieval
has strong evidence, the best article is returned directly. Otherwise, the graph
escalates the ticket.

## Database

`support_chat.db` contains:

| Table | Purpose |
|---|---|
| `articles` | Runtime knowledge-base content and helpfulness counts |
| `tickets` | Ticket summary, status, user and assignment |
| `messages` | Customer, assistant and associate messages |
| `associates` | Associate profile, specialty and credentials |
| `users` | Customer identity and session data |
| `resolutions` | Documented resolution steps and publication status |

Pinecone vectors and the in-memory BM25 index are not stored in SQLite.

## Important API routes

### Customer routes

| Method | Route | Purpose |
|---|---|---|
| `GET` | `/api/me` | Return the current customer identity |
| `POST` | `/api/me` | Update the customer profile |
| `POST` | `/api/tickets` | Create a ticket and process its first message |
| `GET` | `/api/my/tickets` | List the customer's tickets |
| `GET` | `/api/tickets/{id}/messages` | Load ticket messages |
| `POST` | `/api/tickets/{id}/messages` | Add and process a message |
| `GET` | `/api/tickets/{id}/stream` | Stream ticket updates |
| `POST` | `/api/tickets/{id}/feedback` | Record answer feedback |

### Knowledge-base and operations routes

| Method | Route | Purpose |
|---|---|---|
| `GET` | `/api/articles` | List knowledge-base articles |
| `GET` | `/api/articles/{id}` | Return one article |
| `GET` | `/api/metrics` | Return ticket and retrieval-related metrics |

### Associate routes

| Method | Route | Purpose |
|---|---|---|
| `POST` | `/api/associate/login` | Authenticate an associate |
| `GET/POST` | `/api/associate/me` | Read or update associate identity |
| `POST` | `/api/associate/reset` | Reset associate credentials |
| `GET` | `/api/associate/tickets` | List escalated/active tickets |
| `POST` | `/api/associate/tickets/{id}/claim` | Claim a ticket |
| `POST` | `/api/associate/tickets/{id}/resolve` | Resolve and optionally publish |
| `GET` | `/api/associate/articles` | Load knowledge-base content |

## Ticket states

Common states include:

```text
open -> escalated -> associate_active -> resolved
```

- `open`: the automated assistant may process customer messages.
- `escalated`: waiting for human support assignment or action.
- `associate_active`: an associate owns the conversation.
- `resolved`: work is complete.

Once a ticket is no longer `open`, follow-up messages are delivered to the associate
workflow rather than being answered again by RAG.

## Evaluation

Run:

```powershell
py eval.py
```

The evaluation checks expected answer/escalation decisions and top source IDs. It also
contains focused checks that verify:

- `faq-001` answers password-reset questions during semantic-provider failure.
- Irrelevant questions still escalate during BM25 fallback.

Provider-backed evaluation requires valid OpenAI and Pinecone credentials and may
incur API usage. The focused fallback tests mock provider failure.

## Troubleshooting

### `429 insufficient_quota`

OpenAI API billing or project quota is unavailable. Semantic retrieval fails, but
BM25 fallback remains active. Add API credits or use a funded project/key.

### Pinecone dimension mismatch

The existing Pinecone index dimension must match `EMBEDDING_DIMENSION` exactly. Create
a correctly dimensioned index or restore the code configuration expected by the
existing index.

### Generic low-confidence escalation

Check the server console for a line beginning with `[rag]`. It distinguishes provider
failure from genuine insufficient retrieval evidence.

### Import warnings in the editor

Install `requirements.txt` into the interpreter selected by the editor:

```powershell
py -3 -m pip install -r requirements.txt
```

### `ConnectionAbortedError: WinError 10053`

This usually means a browser closed or cancelled a local connection. It occurs before
application/RAG processing and is generally harmless.

## Security and production considerations

The project is suitable for local development and demonstration. Before production:

- Run behind a production-grade HTTP server and TLS reverse proxy.
- Store secrets in a managed secret store.
- Replace in-memory associate sessions with durable, expiring sessions.
- Set secure cookie flags when using HTTPS.
- Add CSRF protection and request-rate limiting.
- Enforce stronger authorization around metrics and knowledge-base routes.
- Add database backups, migrations and retention policies.
- Add Pinecone namespace cleanup and ingestion observability.
- Avoid logging sensitive customer text or raw provider errors in production.
- Add structured logs, request IDs, health checks and provider timeouts.
- Expand evaluation before adjusting confidence thresholds or retrieval weights.

## Additional documentation

See `SUPPORT_CHAT_CONTROL_FLOW.md` for the detailed 16-step request lifecycle,
including file names, function names, graph state, provider failures and HTTP response
behavior.
