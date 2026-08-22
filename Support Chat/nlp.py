import hashlib
import os
import re
import threading
from typing import Any, Literal, TypedDict

try:
    from langchain_community.retrievers import BM25Retriever
    from langchain_core.documents import Document
    from langchain_core.prompts import ChatPromptTemplate
    from langchain_openai import ChatOpenAI, OpenAIEmbeddings
    from langchain_pinecone import PineconeVectorStore
    from langchain_text_splitters import RecursiveCharacterTextSplitter
    from langgraph.graph import END, START, StateGraph
    from pinecone import Pinecone, ServerlessSpec
    _RAG_IMPORT_ERROR = None
except ImportError as exc:
    _RAG_IMPORT_ERROR = exc

_database = None
_rag_lock = threading.Lock()
_rag_cache: dict[str, Any] = {}

CHUNK_SIZE = 512
CHUNK_OVERLAP = 64
EMBEDDING_DIMENSION = 256
EMBEDDING_MODEL = os.environ.get("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
PINECONE_INDEX = os.environ.get("PINECONE_INDEX", "support-chat")
PINECONE_CLOUD = os.environ.get("PINECONE_CLOUD", "aws")
PINECONE_REGION = os.environ.get("PINECONE_REGION", "us-east-1")
TOP_K = int(os.environ.get("RAG_TOP_K", "4"))
RETRIEVER_K = max(TOP_K * 2, 6)
HIGH_CONFIDENCE = float(os.environ.get("RAG_HIGH_CONFIDENCE", "0.50"))
LOW_CONFIDENCE = float(os.environ.get("RAG_LOW_CONFIDENCE", "0.25"))


class RAGState(TypedDict, total=False):
    query: str
    scored: list[dict[str, Any]]
    top_score: float
    decision: Literal["answer", "escalate"]
    answer: str | None
    generated: bool
    errors: list[str]
    retrieval_mode: str


def configure_database(database):
    global _database, _rag_cache
    _database = database
    _rag_cache = {}


def _get_database():
    if _database is None:
        raise RuntimeError("NLP database has not been configured")
    return _database


def _require_rag_dependencies():
    if _RAG_IMPORT_ERROR is not None:
        raise RuntimeError("RAG dependencies are missing; run `pip install -r requirements.txt`") from _RAG_IMPORT_ERROR


def _require_semantic_configuration():
    if not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY is required")
    if not os.environ.get("PINECONE_API_KEY"):
        raise RuntimeError("PINECONE_API_KEY is required")


def _article_signature(articles):
    value = "\n".join(
        f"{a['id']}|{a.get('document_id')}|{a['created_at']}|{a['title']}|{a['body']}|{a['tags']}"
        for a in articles
    )
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _article_documents(articles):
    documents = []
    for article in articles:
        article_id = article.get("document_id") or str(article["id"])
        documents.append(Document(
            page_content=f"{article['title']}\n\n{article['body']}",
            metadata={"article_id": article_id, "title": article["title"],
                      "tags": article.get("tags") or "", "source": article.get("source") or "knowledge_base",
                      "body": article["body"]},
        ))
    return RecursiveCharacterTextSplitter(
        chunk_size=CHUNK_SIZE, chunk_overlap=CHUNK_OVERLAP, length_function=len
    ).split_documents(documents)


def _ensure_index(pc):
    existing = {item["name"] for item in pc.list_indexes()}
    if PINECONE_INDEX not in existing:
        pc.create_index(name=PINECONE_INDEX, dimension=EMBEDDING_DIMENSION, metric="cosine",
                        spec=ServerlessSpec(cloud=PINECONE_CLOUD, region=PINECONE_REGION))
    description = pc.describe_index(PINECONE_INDEX)
    if description.dimension != EMBEDDING_DIMENSION:
        raise RuntimeError(f"Pinecone index {PINECONE_INDEX!r} has dimension {description.dimension}; expected {EMBEDDING_DIMENSION}")
    return pc.Index(PINECONE_INDEX)


def _build_bm25():
    _require_rag_dependencies()
    articles = _get_database().get_articles()
    signature = _article_signature(articles)
    if _rag_cache.get("bm25_signature") == signature:
        return _rag_cache["bm25"], signature
    with _rag_lock:
        if _rag_cache.get("bm25_signature") == signature:
            return _rag_cache["bm25"], signature
        chunks = _article_documents(articles)
        if not chunks:
            raise RuntimeError("The knowledge base is empty")
        bm25 = BM25Retriever.from_documents(chunks)
        bm25.k = RETRIEVER_K
        _rag_cache.update(bm25_signature=signature, bm25=bm25, chunks=chunks)
        return bm25, signature


def _build_vector_store(signature):
    _require_semantic_configuration()
    if _rag_cache.get("vector_signature") == signature:
        return _rag_cache["vector_store"]
    with _rag_lock:
        if _rag_cache.get("vector_signature") == signature:
            return _rag_cache["vector_store"]
        chunks = _rag_cache["chunks"]
        embeddings = OpenAIEmbeddings(model=EMBEDDING_MODEL, dimensions=EMBEDDING_DIMENSION)
        pc = Pinecone(api_key=os.environ["PINECONE_API_KEY"])
        namespace = f"kb-{signature[:16]}"
        vector_store = PineconeVectorStore(index=_ensure_index(pc), embedding=embeddings, namespace=namespace)
        ids = [hashlib.sha256(f"{doc.metadata['article_id']}:{n}:{doc.page_content}".encode()).hexdigest()
               for n, doc in enumerate(chunks)]
        vector_store.add_documents(chunks, ids=ids)
        _rag_cache.update(vector_signature=signature, vector_store=vector_store)
        return vector_store


_STOP_WORDS = {
    "a", "an", "and", "are", "can", "do", "for", "how", "i", "in", "is",
    "it", "me", "my", "need", "of", "on", "the", "to", "what", "where",
    "with", "you", "your",
}


def _lexical_confidence(query, doc):
    query_tokens = {token for token in re.findall(r"[a-z0-9]+", query.lower())
                    if token not in _STOP_WORDS}
    if not query_tokens:
        return 0.0
    searchable = f"{doc.metadata['title']} {doc.metadata['tags']} {doc.page_content}".lower()
    document_tokens = set(re.findall(r"[a-z0-9]+", searchable))
    return len(query_tokens & document_tokens) / len(query_tokens)


def _hybrid_search_details(query, limit=TOP_K):
    errors = []
    bm25, signature = _build_bm25()
    sparse_docs = bm25.invoke(query)
    try:
        vector_store = _build_vector_store(signature)
        dense_results = vector_store.similarity_search_with_relevance_scores(query, k=RETRIEVER_K)
    except Exception as exc:
        dense_results = []
        errors.append(f"semantic retrieval failed: {exc}")
    fused: dict[str, dict[str, Any]] = {}
    for rank, doc in enumerate(sparse_docs, 1):
        key = doc.metadata["article_id"]
        item = fused.setdefault(key, {"document": doc, "score": 0.0, "bm25_rank": None,
                                      "lexical_score": 0.0, "semantic_score": 0.0})
        lexical = _lexical_confidence(query, doc)
        # For equally strong generic matches, prefer FAQs over specialized process,
        # manual, or historical-ticket articles. Specific query-token overlap still
        # dominates this small source-type tie breaker.
        source_bonus = {
            "faq": 0.04,
            "process": 0.02,
            "manual": 0.01,
        }.get(doc.metadata.get("source"), 0.0) if lexical else 0.0
        item["score"] += 0.4 * lexical + source_bonus
        item["bm25_rank"] = rank
        item["lexical_score"] = max(item["lexical_score"], lexical)
    for rank, (doc, relevance) in enumerate(dense_results, 1):
        key = doc.metadata["article_id"]
        item = fused.setdefault(key, {"document": doc, "score": 0.0, "bm25_rank": None,
                                      "lexical_score": 0.0, "semantic_score": 0.0})
        semantic = max(0.0, min(1.0, float(relevance)))
        item["score"] += 0.6 * semantic / rank
        item["semantic_score"] = max(item["semantic_score"], semantic)
    ranked = sorted(fused.values(), key=lambda item: item["score"], reverse=True)[:limit]
    mode = "hybrid" if dense_results else "bm25_fallback"
    return ranked, errors, mode


def _hybrid_search(query, limit=TOP_K):
    scored, _, _ = _hybrid_search_details(query, limit)
    return scored


def search_articles_hybrid(query, limit=TOP_K):
    return _hybrid_search(query, limit)


def _retrieve_node(state: RAGState):
    try:
        scored, errors, mode = _hybrid_search_details(state["query"], TOP_K)
        return {"scored": scored, "top_score": scored[0]["score"] if scored else 0.0,
                "errors": errors, "retrieval_mode": mode}
    except Exception as exc:
        return {"scored": [], "top_score": 0.0,
                "errors": [f"BM25 retrieval failed: {exc}"], "retrieval_mode": "failed"}


def _route_after_retrieval(state: RAGState):
    if not state.get("scored"):
        return "escalate"
    top = state["scored"][0]
    has_evidence = (top.get("lexical_score", 0.0) >= 0.5 or
                    top.get("semantic_score", 0.0) >= LOW_CONFIDENCE)
    return "generate" if has_evidence else "escalate"


def _generate_node(state: RAGState):
    context = "\n\n---\n\n".join(
        f"[{i['document'].metadata['article_id']}] {i['document'].metadata['title']}\n{i['document'].metadata['body']}"
        for i in state["scored"])
    prompt = ChatPromptTemplate.from_messages([
        ("system", "You are a grounded customer-support agent. Answer only from the supplied context. Be concise, preserve numbered steps, cite sources as [article-id], and reply INSUFFICIENT_CONTEXT when the context does not directly answer the question."),
        ("human", "QUESTION:\n{query}\n\nCONTEXT:\n{context}"),
    ])
    # A strong lexical match remains usable even when Pinecone or generation is down.
    top = state["scored"][0]
    direct_fallback = top.get("lexical_score", 0.0) >= 0.5
    try:
        response = (prompt | ChatOpenAI(model=OPENAI_MODEL, temperature=0)).invoke({"query": state["query"], "context": context})
        answer = str(response.content).strip()
        if "INSUFFICIENT_CONTEXT" not in answer.upper():
            return {"decision": "answer", "answer": answer, "generated": True}
        generation_error = "The model reported insufficient context"
    except Exception as exc:
        generation_error = str(exc)
    generation_errors = list(state.get("errors", []))
    generation_errors.append(f"generation failed: {generation_error}")
    if direct_fallback or state["top_score"] >= HIGH_CONFIDENCE:
        doc = state["scored"][0]["document"]
        return {"decision": "answer", "answer": f"According to [{doc.metadata['article_id']}] {doc.metadata['title']}:\n\n{doc.metadata['body']}",
                "generated": False, "errors": generation_errors}
    return {"decision": "escalate", "answer": None, "errors": generation_errors}


def _escalate_node(state: RAGState):
    return {"decision": "escalate", "answer": None, "generated": False}


def _build_graph():
    builder = StateGraph(RAGState)
    builder.add_node("retrieve", _retrieve_node)
    builder.add_node("generate", _generate_node)
    builder.add_node("escalate", _escalate_node)
    builder.add_edge(START, "retrieve")
    builder.add_conditional_edges("retrieve", _route_after_retrieval,
                                  {"generate": "generate", "escalate": "escalate"})
    builder.add_edge("generate", END)
    builder.add_edge("escalate", END)
    return builder.compile()


rag_graph = _build_graph() if _RAG_IMPORT_ERROR is None else None


def answer_from_knowledge_base(query):
    if rag_graph is None:
        state: RAGState = _retrieve_node({"query": query})
        state.update(_escalate_node(state))
    else:
        state = rag_graph.invoke({"query": query})
    trace = {"top_score": round(state.get("top_score", 0.0), 4),
             "sources": [{"id": i["document"].metadata["article_id"], "title": i["document"].metadata["title"],
                          "score": round(i["score"], 4)} for i in state.get("scored", [])],
             "retrieval": state.get("retrieval_mode", "unknown")}
    if "generated" in state:
        trace["generated"] = state["generated"]
    if state.get("errors"):
        trace["errors"] = state["errors"]
    return {"decision": state.get("decision", "escalate"), "answer": state.get("answer"), "trace": trace}


def search_articles_semantic(query, limit=TOP_K):
    return [item["document"].metadata for item in _hybrid_search(query, limit)]


def search_articles_semantic_scored(query, limit=TOP_K):
    return [(item["document"].metadata, item["score"]) for item in _hybrid_search(query, limit)]


def print_model_status():
    if _RAG_IMPORT_ERROR:
        print(f"[nlp] LangChain RAG unavailable: {_RAG_IMPORT_ERROR}")
    else:
        print(f"[nlp] LangGraph hybrid RAG: BM25 + Pinecone; chunk={CHUNK_SIZE}, dimensions={EMBEDDING_DIMENSION}")


def route_specialty(text):
    specialties = {"Billing & Payments": ("billing", "bill", "payment", "pay", "invoice", "card"),
                   "Security & 2FA": ("2fa", "two-factor", "security", "authenticator"),
                   "Accounts & Login": ("login", "account", "password")}
    lowered = text.lower()
    return next((name for name, terms in specialties.items() if any(term in lowered for term in terms)), "General Support")


def wants_human(text):
    lowered = text.lower()
    return any(keyword in lowered for keyword in ("talk to an agent", "connect me to a human", "human", "real person", "support agent", "live agent"))


def is_greeting(text):
    text_l = text.lower().strip()
    return text_l in {"hi", "hello", "hey", "good morning", "good afternoon", "good evening"} or re.match(r"^(hi|hello|hey)[\s!.,]*$", text_l) is not None
