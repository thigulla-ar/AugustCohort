# Support Chat Application: Flow of Control

This document explains the complete control flow from a customer submitting a chat
message through SQLite persistence, LangGraph orchestration, hybrid retrieval,
answer generation, escalation, and the final browser response.

## High-level flow

```text
Browser
  -> static/chat.js
  -> HTTP POST
  -> app.py
  -> db.py / support_chat.db
  -> nlp.py / LangGraph
       -> BM25 from SQLite
       -> Pinecone with OpenAI embeddings
       -> generate answer or escalate
  -> app.py saves the result
  -> HTTP response
  -> static/chat.js displays messages
```

## 1. Browser submits the message

File: `static/chat.js`

Handler:

```javascript
document.getElementById("chat-form").onsubmit
```

For the first message in a conversation, the browser sends:

```javascript
fetch("/api/tickets", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({summary: text})
})
```

For subsequent messages, the browser sends:

```javascript
fetch(`/api/tickets/${ticketId}/messages`, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({sender: "user", body: text})
})
```

After `Hi` creates a ticket, `I need to reset my password` is sent to:

```text
POST /api/tickets/{ticket_id}/messages
```

## 2. `app.py` receives the request

File: `app.py`

Function:

```python
RequestHandler.do_POST()
```

The follow-up-message branch is selected by:

```python
if path.startswith("/api/tickets/") and path.endswith("/messages"):
```

The application validates the ticket and user, saves the message, checks the ticket
status, handles greetings and human requests, and sends other messages to RAG.

## 3. `db.py` saves the customer message

File: `db.py`

Function:

```python
DB.add_message()
```

Called from `app.py` as:

```python
message_id = db.add_message(tid, "user", author_name, body)
```

The corresponding database operation is:

```sql
INSERT INTO messages (
    ticket_id,
    sender,
    author_name,
    body,
    created_at
)
VALUES (?, ?, ?, ?, ?);
```

The customer message is now stored in `support_chat.db`.

## 4. Preliminary message routing

Back in `app.py`, the message passes through these checks:

```python
if nlp.is_greeting(body):
    ...
elif nlp.wants_human(body):
    ...
else:
    rag_result = nlp.answer_from_knowledge_base(body)
```

| Example input | Function | Outcome |
|---|---|---|
| `Hi` | `nlp.is_greeting()` | Fixed greeting response |
| `Connect me to an agent` | `nlp.wants_human()` | Immediate escalation |
| `Reset my password` | `answer_from_knowledge_base()` | LangGraph RAG |
| `How to mint money` | `answer_from_knowledge_base()` | RAG, then likely escalation |

## 5. RAG entry point

File: `nlp.py`

Function:

```python
answer_from_knowledge_base(query)
```

It invokes the compiled LangGraph:

```python
state = rag_graph.invoke({"query": query})
```

The graph is constructed by `_build_graph()`:

```text
START
  -> retrieve
  -> conditional routing
       -> sufficient evidence -> generate -> END
       -> insufficient evidence -> escalate -> END
```

## 6. LangGraph retrieval node

File: `nlp.py`

Function:

```python
_retrieve_node(state)
```

It calls:

```python
_hybrid_search_details(state["query"], TOP_K)
```

The node records:

```python
{
    "scored": scored,
    "top_score": top_score,
    "errors": errors,
    "retrieval_mode": mode
}
```

Possible retrieval modes are:

- `hybrid`
- `bm25_fallback`
- `failed`

## 7. BM25 retrieval

File: `nlp.py`

Function:

```python
_build_bm25()
```

It loads articles from SQLite:

```python
articles = _get_database().get_articles()
```

File: `db.py`

Function:

```python
DB.get_articles()
```

SQL query:

```sql
SELECT *
FROM articles
ORDER BY helpful_count DESC, created_at DESC;
```

The articles are converted into LangChain `Document` objects by
`_article_documents()`. They are then split into chunks:

```python
RecursiveCharacterTextSplitter(
    chunk_size=512,
    chunk_overlap=64
)
```

The in-memory BM25 index is created with:

```python
BM25Retriever.from_documents(chunks)
```

It is cached in `_rag_cache` and rebuilt when the SQLite article signature changes.

## 8. Semantic retrieval

File: `nlp.py`

Function:

```python
_build_vector_store(signature)
```

OpenAI embeddings are configured as:

```python
OpenAIEmbeddings(
    model="text-embedding-3-small",
    dimensions=256
)
```

The Pinecone vector store is configured as:

```python
PineconeVectorStore(
    index=index,
    embedding=embeddings,
    namespace=namespace
)
```

Semantic search runs with:

```python
vector_store.similarity_search_with_relevance_scores(
    query,
    k=RETRIEVER_K
)
```

Pinecone stores the 256-dimensional vectors and chunk metadata. SQLite remains the
runtime source of article content.

## 9. OpenAI quota-error path

Suppose OpenAI returns:

```text
HTTP 429
code: insufficient_quota
```

The semantic exception is caught inside `_hybrid_search_details()`:

```python
except Exception as exc:
    dense_results = []
    errors.append(f"semantic retrieval failed: {exc}")
```

Control then follows this fallback path:

```text
OpenAI embedding request
  -> 429 insufficient_quota
  -> Pinecone semantic retrieval unavailable
  -> local BM25 remains available
  -> retrieval_mode = "bm25_fallback"
```

The provider error is logged in `app.py`:

```python
print(f"[rag] query={body!r} errors={rag_result['trace']['errors']}")
```

## 10. Hybrid result scoring

File: `nlp.py`

Function:

```python
_hybrid_search_details()
```

When both retrieval systems work, BM25 supplies the lexical component and Pinecone
supplies the semantic component:

```text
BM25 lexical component: 40%
Pinecone semantic component: 60%
```

When Pinecone or OpenAI fails, the system continues with BM25 only.

For `I need to reset my password`:

```text
BM25 -> faq-001
lexical evidence -> strong
decision -> generate or direct article fallback
```

For `How to mint money` during an OpenAI quota failure:

```text
BM25 -> no meaningful knowledge-base article
semantic retrieval -> unavailable
decision -> escalate
```

## 11. Conditional LangGraph routing

File: `nlp.py`

Function:

```python
_route_after_retrieval(state)
```

The graph proceeds to `generate` when the top result has either:

```python
lexical_score >= 0.5
```

or:

```python
semantic_score >= LOW_CONFIDENCE
```

Without sufficient lexical or semantic evidence, it returns `escalate`.

## 12. Generation branch

File: `nlp.py`

Function:

```python
_generate_node(state)
```

It builds grounded context from the retrieved articles and calls:

```python
ChatOpenAI(
    model=OPENAI_MODEL,
    temperature=0
)
```

### Generated answer

```python
{
    "decision": "answer",
    "answer": "...",
    "generated": True
}
```

### Generation failure with a strong BM25 match

The application returns the retrieved article directly:

```python
{
    "decision": "answer",
    "answer": "According to [faq-001] ...",
    "generated": False,
    "errors": ["generation failed: ..."]
}
```

### Generation failure without strong evidence

```python
{
    "decision": "escalate",
    "answer": None,
    "errors": ["generation failed: ..."]
}
```

## 13. Escalation branch

File: `nlp.py`

Function:

```python
_escalate_node(state)
```

Graph update:

```python
{
    "decision": "escalate",
    "answer": None,
    "generated": False
}
```

`answer_from_knowledge_base()` converts the final state into:

```python
{
    "decision": "escalate",
    "answer": None,
    "trace": {
        "top_score": 0.0,
        "sources": [],
        "retrieval": "bm25_fallback",
        "errors": ["semantic retrieval failed: ..."]
    }
}
```

## 14. `app.py` handles the RAG decision

For an answer:

```python
if rag_result["decision"] == "answer":
    db.add_message(
        tid,
        "bot",
        "Assistant",
        rag_result["answer"]
    )
```

For escalation:

```python
db.escalate_ticket(tid)
associate = db.pick_associate(nlp.route_specialty(body))
db.assign_ticket(tid, associate["id"])
```

The generic customer-facing message is saved:

```text
I'm not confident I can answer that accurately, so I've escalated this to a support associate.
```

Provider details remain in the server log rather than being shown to the customer.

## 15. HTTP response

For a follow-up message, `app.py` returns:

```json
{
  "ok": true,
  "message_id": 123
}
```

with:

```text
HTTP 200 OK
```

This means the Support Chat application successfully received, stored, and handled
the message. It does not mean every internal provider request succeeded. For example,
the OpenAI request may return `429`, while the application catches that error, runs
BM25 fallback, saves an escalation, and successfully returns HTTP `200` to the browser.

## 16. Browser displays the result

File: `static/chat.js`

The browser continues loading ticket messages through either:

```text
GET /api/tickets/{ticket_id}/messages
```

or:

```text
GET /api/tickets/{ticket_id}/stream
```

The messages previously saved in `support_chat.db` are rendered as customer,
assistant, or associate chat bubbles. The visible chat response is therefore based on
persisted application messages, while detailed retrieval and provider errors remain in
the server console and RAG trace.

## Response summary

| Layer | Example response | Meaning |
|---|---|---|
| OpenAI embeddings | `429 insufficient_quota` | Semantic retrieval cannot run |
| RAG retrieval | `bm25_fallback` | Local BM25 remains active |
| LangGraph | `answer` or `escalate` | Final RAG decision |
| Support Chat API | `200 OK` | Message handling completed successfully |
| Browser | Chat bubble | Persisted assistant or associate message displayed |
